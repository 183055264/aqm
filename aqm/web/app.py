"""Web UI dashboard for aqm — FastAPI app factory.

All HTML rendering is in pages/, all API endpoints in api/.
This module just wires everything together.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from aqm.core.agent import load_agents
from aqm.core.context_file import ContextFile
from aqm.core.project import (
    get_agents_yaml_path,
    get_db_path,
    get_default_pipeline,
    get_pipeline_path,
    get_tasks_dir,
    list_pipelines,
)
from aqm.queue.sqlite import SQLiteQueue
from aqm.meeting import list_meetings
from aqm.web.task_projects import find_task, list_project_tasks

# Restart coordination: only one restart script may run at a time, otherwise
# overlapping scripts kill each other's freshly started server and the port is
# left dead (the UI then reports "Failed to fetch").
_restart_lock = threading.Lock()
_restart_busy = False


class _ProbeAccessFilter(logging.Filter):
    """Hide access-log noise from other local services probing this port.

    The opencode CLI service scans every listening port and asks ``/v1/models``
    whether it happens to be an OpenAI-compatible server.  A 404 is the correct
    answer — we must *not* pretend to be a model server — but logging the same
    404 every few seconds is just noise.
    """

    _PATHS = frozenset({"/v1/models"})

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and path.split("?", 1)[0] in self._PATHS:
                return False
        return True


def _install_probe_access_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _ProbeAccessFilter) for f in access.filters):
        access.addFilter(_ProbeAccessFilter())


def create_app(project_root: Path, host: str = "127.0.0.1", port: int = 8000) -> FastAPI:
    """Create and return the FastAPI application for the aqm dashboard."""
    _install_probe_access_filter()
    app = FastAPI(title="aqm 控制台", docs_url="/docs")

    project_root = Path(project_root).resolve()
    db_path = get_db_path(project_root)
    
    # Store server config for restart
    app.state.host = host
    app.state.port = port
    app.state.project_root = project_root

    def _get_queue() -> SQLiteQueue:
        return SQLiteQueue(db_path)

    def _get_agents(pipeline: str | None = None) -> tuple[dict, str | None]:
        """Load agents, returning (agents_dict, error_message)."""
        try:
            path = get_agents_yaml_path(project_root, pipeline)
            if path.exists():
                return load_agents(path), None
            return {}, None
        except (ValueError, FileNotFoundError) as exc:
            return {}, str(exc)

    # ── Health probe ──────────────────────────────────────────────────

    _health_seen: set[str] = set()

    @app.get("/health")
    async def health(request: Request):
        """Liveness probe for local monitors / browser extensions.

        The opencode CLI service probes ``/health`` on every port it finds in
        its config, so answering here keeps the console log free of 404s.
        """
        log = logging.getLogger("aqm.web")
        user_agent = (request.headers.get("user-agent") or "").strip() or "unknown"
        if user_agent not in _health_seen:
            _health_seen.add(user_agent)
            log.warning(
                "Health probe from %s: %s",
                request.client.host if request.client else "?",
                user_agent,
            )
        return {"status": "ok", "service": "aqm"}

    # ── Startup: recover stale tasks ──────────────────────────────────

    @app.on_event("startup")
    async def _recover_stale():
        log = logging.getLogger("aqm.web")
        queue = _get_queue()
        stalled = []
        try:
            stalled = queue.recover_stale_tasks()
            if stalled:
                log.warning(
                    "Recovered %d stale task(s): %s",
                    len(stalled),
                    ", ".join(t.id for t in stalled),
                )
        finally:
            queue.close()
        # Members die with the process, so any block left "in progress" is
        # stale.  Release them before deciding what to resume, otherwise the
        # work-block panel shows several blocks running at once.
        try:
            from aqm.web.api.tasks import cleanup_stale_chunks

            cleaned = cleanup_stale_chunks(project_root)
            if cleaned:
                log.warning("Released %d stale in-progress work block(s)", cleaned)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Work-block cleanup failed: %s", exc)
        if not stalled:
            return
        # A paused task blocks at the next boundary in memory; restore that
        # state after a server restart so it does not silently start running.
        from aqm.core.pipeline import pause_task

        for task in stalled:
            if (task.metadata or {}).get("paused"):
                pause_task(task.id)
                log.warning("Task %s stays paused after restart", task.id)
        # Resume tasks that were running when the server stopped, so a
        # deployment does not silently leave work half-finished.
        if os.environ.get("AQM_DISABLE_AUTO_RESUME") == "1":
            log.warning("Auto-resume disabled by AQM_DISABLE_AUTO_RESUME")
            return
        try:
            from aqm.core.config import load_project_config

            server_cfg = load_project_config(project_root).server or {}
        except Exception:  # pragma: no cover - defensive
            server_cfg = {}
        if server_cfg.get("auto_resume", True) is False:
            log.warning("Auto-resume disabled by project config (server.auto_resume)")
            return
        from aqm.web.api.tasks import auto_resume_stalled

        auto_resume_stalled(project_root, [t.id for t in stalled])

    # ── Favicon ───────────────────────────────────────────────────────

    from fastapi.responses import Response

    _favicon_path = Path(__file__).parent / "static" / "favicon.svg"

    @app.get("/favicon.svg", response_class=Response, include_in_schema=False)
    async def favicon():
        if _favicon_path.exists():
            return Response(
                content=_favicon_path.read_bytes(),
                media_type="image/svg+xml",
            )
        return Response(status_code=404)

    # ── HTML Pages ────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(pipeline: str | None = None):
        from aqm.web.pages.dashboard import render_dashboard
        queue = _get_queue()
        try:
            tasks = queue.list_tasks()
            meetings = list_meetings(project_root)
            return render_dashboard(tasks, meetings=meetings)
        finally:
            queue.close()

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(pipeline: str | None = None):
        from aqm.web.pages.tasks import render_tasks
        tasks = list_project_tasks(project_root)
        pipelines = list_pipelines(project_root)
        current = pipeline or get_default_pipeline(project_root) or "default"
        agents, agent_error = _get_agents(current)
        return render_tasks(
            tasks, agents,
            pipelines=pipelines, current_pipeline=current,
            agent_error=agent_error,
            current_project_dir=str(project_root),
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(pipeline: str | None = None):
        from aqm.web.pages.agents import render_agents
        pipelines = list_pipelines(project_root)
        current = pipeline or get_default_pipeline(project_root) or "default"
        agents, agent_error = _get_agents(current)
        queue = _get_queue()
        try:
            recent_tasks = queue.list_tasks()[:20]
        finally:
            queue.close()
        return render_agents(
            agents, pipelines=pipelines, current_pipeline=current,
            agent_error=agent_error, recent_tasks=recent_tasks,
        )

    @app.get("/pipelines", response_class=HTMLResponse)
    async def pipelines_page(edit: str | None = None):
        from aqm.web.pages.pipelines import render_pipelines
        from aqm.web.api.pipelines import CreatePipelineRequest
        pipelines_list = list_pipelines(project_root)
        default = get_default_pipeline(project_root) or "default"
        pip_data = []
        for name in pipelines_list:
            try:
                path = get_pipeline_path(project_root, name)
                content = path.read_text(encoding="utf-8")
                agent_count = content.count("- id:")
                pip_data.append({"name": name, "agent_count": agent_count, "is_default": name == default})
            except FileNotFoundError:
                pip_data.append({"name": name, "agent_count": 0, "is_default": name == default})

        edit_content = None
        if edit:
            try:
                path = get_pipeline_path(project_root, edit)
                edit_content = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                pass

        return render_pipelines(pip_data, default, edit_name=edit, edit_content=edit_content)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(task_id: str):
        from aqm.web.pages.task_detail import render_task_detail
        found = find_task(project_root, task_id)
        if found is None:
            raise HTTPException(status_code=404, detail="任务未找到")
        task, task_root = found
        pipeline_name = task.metadata.get("pipeline")
        try:
            agents_path = get_agents_yaml_path(task_root, pipeline_name)
            agents = load_agents(
                agents_path,
                cli_params=task.metadata.get("_pipeline_params"),
            ) if agents_path.exists() else {}
        except (ValueError, FileNotFoundError):
            agents = {}
        tasks_dir = get_tasks_dir(task_root)
        ctx_file = ContextFile(tasks_dir / task_id)
        context_md = ctx_file.read()
        return render_task_detail(
            task,
            agents,
            context_md,
            task_project_dir=str(task_root),
            current_project_dir=str(project_root),
        )

    @app.get("/registry", response_class=HTMLResponse)
    async def registry_page():
        from aqm.web.pages.registry import render_registry
        return render_registry()

    @app.get("/validate", response_class=HTMLResponse)
    async def validate_page():
        from aqm.web.pages.validate import render_validate
        return render_validate()

    @app.get("/meeting", response_class=HTMLResponse)
    async def meeting_page():
        from aqm.web.pages.meeting import render_meeting
        return render_meeting()

    # ── Agents JSON API ───────────────────────────────────────────────

    @app.get("/api/agents")
    async def api_agents(pipeline: str | None = None):
        agents, error = _get_agents(pipeline)
        if error:
            raise HTTPException(400, error)
        return [
            {
                "id": a.id,
                "name": a.name,
                "runtime": a.runtime,
                "type": a.type,
                "model": a.model,
                "gate": {"type": a.gate.type, "prompt": a.gate.prompt, "max_retries": a.gate.max_retries} if a.gate else None,
                "mcp": [{"server": m.server} for m in a.mcp],
                "handoffs": [{"to": h.to, "condition": h.condition, "task": h.task} for h in a.handoffs],
                "context_strategy": a.context_strategy,
                "human_input": {"enabled": a.human_input.enabled, "mode": a.human_input.mode} if a.human_input else None,
            }
            for a in agents.values()
        ]

    # ── Context API ───────────────────────────────────────────────────

    @app.get("/api/tasks/{task_id}/context")
    async def api_get_context(task_id: str):
        found = find_task(project_root, task_id)
        if found is None:
            raise HTTPException(404, "任务未找到")
        _, task_root = found
        tasks_dir = get_tasks_dir(task_root)
        ctx_file = ContextFile(tasks_dir / task_id)
        content = ctx_file.read()
        if not content:
            raise HTTPException(404, "该任务没有上下文文件")
        return PlainTextResponse(content, media_type="text/plain")

    # ── Global SSE (dashboard real-time counters) ───────────────────

    @app.get("/api/events")
    async def global_events():
        from starlette.responses import StreamingResponse
        from aqm.web.api.sse import subscribe_global
        return StreamingResponse(
            subscribe_global(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Project Config API ──────────────────────────────────────────────

    @app.get("/api/project")
    async def api_get_project():
        """Get current project info."""
        from aqm.core.config import get_recent_projects
        return {
            "current": str(project_root),
            "recent": [str(p) for p in get_recent_projects()],
            "pid": os.getpid(),
        }

    @app.post("/api/project/switch")
    async def api_switch_project(request: Request):
        """Switch to a different project directory."""
        from aqm.core.config import set_default_project, add_recent_project
        body = await request.json()
        path_str = body.get("path", "").strip()
        if not path_str:
            raise HTTPException(400, "路径不能为空")
        new_root = Path(path_str).expanduser().resolve()
        if not new_root.is_dir():
            raise HTTPException(400, f"{new_root} 目录不存在")
        if not (new_root / ".aqm").is_dir():
            # Return 404 to indicate "not a valid project" - frontend will prompt to create
            raise HTTPException(404, f"{new_root} 不是有效的 aqm 项目 (没有 .aqm/ 目录)")
        # Save as default project
        set_default_project(new_root)
        add_recent_project(new_root)
        return {
            "status": "ok",
            "message": f"已切换到 {new_root}。请重启服务器以应用更改。",
            "new_project": str(new_root),
        }

    @app.post("/api/project/init")
    async def api_init_project(request: Request):
        """Initialize a new aqm project in the given directory."""
        from aqm.core.project import init_project
        body = await request.json()
        path_str = body.get("path", "").strip()
        if not path_str:
            raise HTTPException(400, "路径不能为空")
        new_root = Path(path_str).expanduser().resolve()
        if not new_root.is_dir():
            raise HTTPException(400, f"{new_root} 目录不存在")
        if (new_root / ".aqm").is_dir():
            raise HTTPException(400, f"{new_root} 已经是 aqm 项目")
        try:
            init_project(new_root)
        except Exception as e:
            raise HTTPException(500, f"初始化失败: {str(e)}")
        return {
            "status": "ok",
            "message": f"已在 {new_root} 创建 aqm 项目",
            "new_project": str(new_root),
        }

    # ── Restart Status File ───────────────────────────────────────────
    
    _restart_status_file = os.path.join(
        tempfile.gettempdir(), "aqm_restart_status.json"
    )
    
    def _write_restart_status(status: str, detail: str = "", suggestion: str = ""):
        """Write restart status to file for frontend to poll."""
        import json
        try:
            with open(_restart_status_file, "w", encoding="utf-8") as f:
                json.dump({"status": status, "detail": detail, "suggestion": suggestion}, f)
        except Exception:
            pass
    
    def _clear_restart_status():
        """Clear restart status file."""
        try:
            if os.path.exists(_restart_status_file):
                os.remove(_restart_status_file)
        except Exception:
            pass
    
    @app.get("/api/project/restart-status")
    async def api_restart_status():
        """Get current restart status."""
        import json
        try:
            if os.path.exists(_restart_status_file):
                with open(_restart_status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["pid"] = os.getpid()
                return data
            return {"status": "idle"}
        except Exception:
            return {"status": "unknown"}
    
    @app.post("/api/project/restart")
    async def api_restart_server(request: Request):
        """Restart server with new project configuration.
        
        Writes detailed status to a file that the frontend can poll.
        """
        import json
        import subprocess
        import sys
        import platform

        # Target project: explicit body path when switching projects, otherwise
        # the project this server is already serving. Using the globally
        # configured default here used to restart into an unrelated project.
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        requested = str(body.get("path") or "").strip()
        if requested:
            new_project = Path(requested).expanduser().resolve()
        else:
            new_project = Path(project_root).expanduser().resolve()

        if not new_project.is_dir():
            return {
                "status": "error",
                "detail": f"项目目录不存在: {new_project}",
                "suggestion": "请确认路径正确",
            }

        # Single-flight: overlapping restart requests used to spawn several
        # batch scripts that killed whichever server owned the port last,
        # leaving orphan processes and a dead port ("Failed to fetch").
        global _restart_busy
        with _restart_lock:
            if _restart_busy:
                return {
                    "status": "restarting",
                    "message": "服务器已在重启中...",
                    "poll_url": "/api/project/restart-status",
                }
            try:
                if os.path.exists(_restart_status_file):
                    age = time.time() - os.path.getmtime(_restart_status_file)
                    with open(_restart_status_file, "r", encoding="utf-8") as f:
                        current = json.load(f)
                    if (
                        age < 30
                        and current.get("status")
                        in ("restarting", "creating_script", "starting_process")
                    ):
                        return {
                            "status": "restarting",
                            "message": current.get("detail") or "服务器正在重启，请稍候...",
                            "poll_url": "/api/project/restart-status",
                        }
            except Exception:
                pass
            _restart_busy = True

        # Get current server config
        host = app.state.host
        port = app.state.port
        old_pid = os.getpid()
        
        # Write "restarting" status immediately
        _write_restart_status("restarting", "正在准备重启...", "")
        
        def _restart_in_background():
            """Stop old server, then start new server."""
            global _restart_busy
            try:
                # Find aqm executable in the venv
                venv_scripts = os.path.dirname(sys.executable)
                is_windows = platform.system() == "Windows"
                
                # Try to find aqm executable
                if is_windows:
                    aqm_exe = os.path.join(venv_scripts, "aqm.exe")
                else:
                    aqm_exe = os.path.join(venv_scripts, "aqm")
                
                # Build command
                if os.path.exists(aqm_exe):
                    exe_path = aqm_exe
                    cmd = [exe_path, "--project", str(new_project), "serve", "--port", str(port), "--host", host]
                else:
                    # Fallback: python -m aqm.cli
                    python_path = sys.executable
                    if not os.path.exists(python_path):
                        raise FileNotFoundError(f"Python 可执行文件未找到: {python_path}")
                    cmd = [python_path, "-m", "aqm.cli", "serve", "--project", str(new_project), "--port", str(port), "--host", host]
                    exe_path = python_path
                cmd_line = subprocess.list2cmdline(cmd)
                
                # Write "creating script" status
                _write_restart_status("creating_script", f"命令: {' '.join(cmd)}", "")
                
                if is_windows:
                    # Windows: create .bat script
                    # A per-run log avoids fighting with the previous server,
                    # which keeps the old log handle open for its whole life
                    # (a shared file made every restart after the first one
                    # fail silently).
                    log_file = os.path.join(
                        tempfile.gettempdir(),
                        f"aqm_restart_{int(time.time() * 1000)}.log",
                    )
                    restart_script = f"""@echo off
chcp 65001 >nul
echo Restarting aqm server... > "{log_file}"
echo Command: {cmd_line} >> "{log_file}"
echo Timestamp: %date% %time% >> "{log_file}"
set OLD_PID={old_pid}
rem Wait for the old server to release the port (it exits right after spawning us).
for /l %%i in (1,1,30) do (
    netstat -an | findstr /C:"127.0.0.1:{port} " | findstr LISTENING >nul
    if errorlevel 1 goto port_free
    timeout /t 1 /nobreak >nul
)
set OWNER=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:"127.0.0.1:{port} " ^| findstr LISTENING') do set OWNER=%%a
if not defined OWNER goto port_free
if not "%OWNER%"=="%OLD_PID%" (
    echo Port is held by PID %OWNER%, not the previous server %OLD_PID%; aborting restart >> "{log_file}"
    goto abort_restart
)
echo Killing unresponsive old server %OLD_PID% >> "{log_file}"
taskkill /F /PID %OLD_PID% >nul 2>&1
timeout /t 2 /nobreak >nul
:port_free
echo Starting new server on port {port}... >> "{log_file}"
{cmd_line} >> "{log_file}" 2>&1
echo Server exited with code %errorlevel% >> "{log_file}"
goto :eof
:abort_restart
echo Restart aborted; the previous server was left running >> "{log_file}"
"""
                    temp_dir = tempfile.gettempdir()
                    restart_script_path = os.path.join(temp_dir, "aqm_restart.bat")
                    with open(restart_script_path, "w", encoding="utf-8") as f:
                        f.write(restart_script)
                    
                    # Write "starting process" status
                    _write_restart_status("starting_process", f"脚本: {restart_script_path}，日志: {log_file}", "")
                    
                    # Check if script can be executed
                    if not os.path.exists(restart_script_path):
                        raise FileNotFoundError(f"重启脚本创建失败: {restart_script_path}")
                    
                    subprocess.Popen(
                        restart_script_path,
                        creationflags=subprocess.CREATE_NEW_CONSOLE,
                        shell=True
                    )
                else:
                    # Unix: create .sh script
                    log_file = os.path.join(
                        tempfile.gettempdir(),
                        f"aqm_restart_{int(time.time() * 1000)}.log",
                    )
                    restart_script = f"""#!/bin/bash
echo "Restarting aqm server..." > "{log_file}"
echo "Command: {cmd_line}" >> "{log_file}"
echo "Timestamp: $(date)" >> "{log_file}"
# Wait up to 30s for the old server to release the port.
for i in $(seq 1 30); do
  if command -v lsof >/dev/null 2>&1; then
    lsof -i :{port} -sTCP:LISTEN -t >/dev/null 2>&1 || break
  else
    sleep 2
    break
  fi
  sleep 1
done
echo "Starting new server on port {port}..." >> "{log_file}"
{cmd_line} >> "{log_file}" 2>&1
echo "Server exited with code $?" >> "{log_file}"
"""
                    temp_dir = tempfile.gettempdir()
                    restart_script_path = os.path.join(temp_dir, "aqm_restart.sh")
                    with open(restart_script_path, "w", encoding="utf-8") as f:
                        f.write(restart_script)
                    
                    _write_restart_status("starting_process", f"脚本: {restart_script_path}", "")
                    
                    if not os.path.exists(restart_script_path):
                        raise FileNotFoundError(f"重启脚本创建失败: {restart_script_path}")
                    
                    os.chmod(restart_script_path, 0o755)
                    subprocess.Popen(
                        restart_script_path,
                        preexec_fn=os.setsid
                    )
                
                # Write "success" status before exiting
                _write_restart_status("success", "服务器已停止，新服务器正在启动", "")
                
                # Save all running meetings before terminating the process
                try:
                    from aqm.meeting import _meetings, _meetings_lock, _save_meeting
                    with _meetings_lock:
                        running = [m for m in _meetings.values() if m.status == "running"]
                    for m in running:
                        try:
                            _save_meeting(m, project_root)
                        except Exception:
                            pass
                except Exception:
                    pass
                
                # Stop old server (this process)
                time.sleep(0.5)
                os._exit(0)
            except FileNotFoundError as e:
                _write_restart_status("failed", str(e), "确保 aqm 已正确安装，可执行文件存在于虚拟环境的 Scripts/bin 目录")
                _restart_busy = False
            except PermissionError as e:
                _write_restart_status("failed", str(e), "检查脚本权限或尝试以管理员身份运行")
                _restart_busy = False
            except Exception as e:
                import traceback
                error_detail = str(e) + "\n" + traceback.format_exc()
                _write_restart_status("failed", error_detail[:500], "查看临时目录中的 aqm_restart_*.log")
                _restart_busy = False
        
        # Start restart in background thread
        threading.Thread(target=_restart_in_background, daemon=True).start()
        
        return {
            "status": "restarting",
            "message": "服务器正在重启，请稍后刷新页面...",
            "poll_url": "/api/project/restart-status",
        }

    # ── API Routes ────────────────────────────────────────────────────

    from aqm.web.api.tasks import create_tasks_router
    from aqm.web.api.pipelines import create_pipelines_router
    from aqm.web.api.registry import create_registry_router
    from aqm.web.api.validate import create_validate_router
    from aqm.web.api.meeting import create_meeting_router

    app.include_router(create_tasks_router(project_root))
    app.include_router(create_pipelines_router(project_root))
    app.include_router(create_registry_router(project_root))
    app.include_router(create_validate_router(project_root))
    app.include_router(create_meeting_router(project_root))

    return app
