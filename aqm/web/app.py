"""Web UI dashboard for aqm — FastAPI app factory.

All HTML rendering is in pages/, all API endpoints in api/.
This module just wires everything together.
"""

from __future__ import annotations

import os
import tempfile
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


def create_app(project_root: Path, host: str = "127.0.0.1", port: int = 8000) -> FastAPI:
    """Create and return the FastAPI application for the aqm dashboard."""
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

    # ── Startup: recover stale tasks ──────────────────────────────────

    @app.on_event("startup")
    async def _recover_stale():
        import logging
        log = logging.getLogger("aqm.web")
        queue = _get_queue()
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
        queue = _get_queue()
        try:
            tasks = queue.list_tasks()
            pipelines = list_pipelines(project_root)
            current = pipeline or get_default_pipeline(project_root) or "default"
            agents, agent_error = _get_agents(current)
            return render_tasks(
                tasks, agents,
                pipelines=pipelines, current_pipeline=current,
                agent_error=agent_error,
            )
        finally:
            queue.close()

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
        queue = _get_queue()
        try:
            task = queue.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="任务未找到")
            agents, _ = _get_agents()
            tasks_dir = get_tasks_dir(project_root)
            ctx_file = ContextFile(tasks_dir / task_id)
            context_md = ctx_file.read()
            return render_task_detail(task, agents, context_md)
        finally:
            queue.close()

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
        tasks_dir = get_tasks_dir(project_root)
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
                return data
            return {"status": "idle"}
        except Exception:
            return {"status": "unknown"}
    
    @app.post("/api/project/restart")
    async def api_restart_server():
        """Restart server with new project configuration.
        
        Writes detailed status to a file that the frontend can poll.
        """
        import subprocess
        import sys
        import threading
        import time
        import platform
        import shutil
        
        # Get the current project root from config
        from aqm.core.config import get_default_project
        new_project = get_default_project()
        
        if new_project is None:
            return {"status": "error", "detail": "没有保存的项目配置", "suggestion": "请先切换项目"}
        
        # Get current server config
        host = app.state.host
        port = app.state.port
        
        # Write "restarting" status immediately
        _write_restart_status("restarting", "正在准备重启...", "")
        
        def _restart_in_background():
            """Stop old server, then start new server."""
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
                
                # Write "creating script" status
                _write_restart_status("creating_script", f"命令: {' '.join(cmd)}", "")
                
                if is_windows:
                    # Windows: create .bat script
                    # Write error log to temp file
                    log_file = os.path.join(tempfile.gettempdir(), "aqm_restart.log")
                    restart_script = f"""@echo off
chcp 65001 >nul
echo Restarting aqm server... > "{log_file}"
echo Command: {" ".join(cmd)} >> "{log_file}"
echo Timestamp: %date% %time% >> "{log_file}"
timeout /t 3 /nobreak >nul
echo Starting new server on port {port}... >> "{log_file}"
{" ".join(cmd)} >> "{log_file}" 2>&1
echo Server exited with code %errorlevel% >> "{log_file}"
"""
                    temp_dir = tempfile.gettempdir()
                    restart_script_path = os.path.join(temp_dir, "aqm_restart.bat")
                    with open(restart_script_path, "w", encoding="utf-8") as f:
                        f.write(restart_script)
                    
                    # Write "starting process" status
                    _write_restart_status("starting_process", f"脚本: {restart_script_path}", "")
                    
                    # Check if script can be executed
                    if not os.path.exists(restart_script_path):
                        raise FileNotFoundError(f"重启脚本创建失败: {restart_script_path}")
                    
                    subprocess.Popen(
                        [restart_script_path],
                        creationflags=subprocess.CREATE_NEW_CONSOLE,
                        shell=True
                    )
                else:
                    # Unix: create .sh script
                    log_file = os.path.join(tempfile.gettempdir(), "aqm_restart.log")
                    restart_script = f"""#!/bin/bash
echo "Restarting aqm server..." > "{log_file}"
echo "Command: {' '.join(cmd)}" >> "{log_file}"
echo "Timestamp: $(date)" >> "{log_file}"
sleep 3
echo "Starting new server on port {port}..." >> "{log_file}"
{' '.join(cmd)} >> "{log_file}" 2>&1
echo "Server exited with code $?"/>>> "{log_file}"
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
                        [restart_script_path],
                        preexec_fn=os.setsid
                    )
                
                # Write "success" status before exiting
                _write_restart_status("success", "服务器已停止，新服务器正在启动", "")
                
                # Stop old server (this process)
                time.sleep(0.5)
                os._exit(0)
            except FileNotFoundError as e:
                _write_restart_status("failed", str(e), "确保 aqm 已正确安装，可执行文件存在于虚拟环境的 Scripts/bin 目录")
                os._exit(0)
            except PermissionError as e:
                _write_restart_status("failed", str(e), "检查脚本权限或尝试以管理员身份运行")
                os._exit(0)
            except Exception as e:
                import traceback
                error_detail = str(e) + "\n" + traceback.format_exc()
                _write_restart_status("failed", error_detail[:500], "查看日志文件: " + os.path.join(tempfile.gettempdir(), "aqm_restart.log"))
                os._exit(0)
        
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
