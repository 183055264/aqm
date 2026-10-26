"""Web UI dashboard for aqm — FastAPI app factory.

All HTML rendering is in pages/, all API endpoints in api/.
This module just wires everything together.
"""

from __future__ import annotations

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


def create_app(project_root: Path) -> FastAPI:
    """Create and return the FastAPI application for the aqm dashboard."""
    app = FastAPI(title="aqm 控制台", docs_url="/docs")

    project_root = Path(project_root).resolve()
    db_path = get_db_path(project_root)

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

    @app.post("/api/project/restart")
    async def api_restart_server():
        """Request server restart (for project switch)."""
        # Note: Server cannot restart itself cleanly. Config is saved,
        # user must restart the server manually to apply changes.
        return {
            "status": "saved",
            "message": "配置已保存，请手动重启服务器以应用更改。",
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
