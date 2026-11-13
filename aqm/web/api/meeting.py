"""Meeting API endpoints — CRUD, start/stop, SSE."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationInfo, field_validator
from starlette.responses import StreamingResponse

from aqm.meeting import (
    Meeting,
    MeetingAgent,
    DEFAULT_AGENTS,
    broadcast_meeting_event,
    continue_meeting,
    create_meeting,
    delete_meeting,
    get_meeting,
    get_shared_file_content,
    get_shared_file_path,
    list_meetings,
    load_meetings_on_startup,
    start_meeting,
    stop_meeting,
    submit_human_speech,
    subscribe_meeting,
    unsubscribe_meeting,
)


class MeetingAgentConfig(BaseModel):
    id: str
    name: str
    runtime: str
    role: str = ""
    system_prompt: str = ""
    model: str = ""
    directory_access: str = ""

    @field_validator("directory_access")
    @classmethod
    def validate_directory_access(cls, value: str, info: ValidationInfo) -> str:
        if value not in ("", "project_read_write"):
            raise ValueError("directory_access is fixed to project_read_write")
        if value and info.data.get("runtime") != "claude":
            raise ValueError("directory_access is only supported for Claude agents")
        return value

class CreateMeetingRequest(BaseModel):
    topic: str
    description: str = ""
    agents: list[MeetingAgentConfig] = []
    shared_file: str = "meeting.md"
    rounds: int = 1
    timeout_per_agent: int = 300


class ContinueMeetingRequest(BaseModel):
    additional_rounds: int = 0


class HumanSpeechRequest(BaseModel):
    content: str = ""
    agent_id: str = ""
    name: str = ""
    skip: bool = False


class RequirementsSaveRequest(BaseModel):
    content: str


class HandoffRequest(BaseModel):
    pipeline: Optional[str] = None
    agent_id: Optional[str] = None
    # Create another task even when this meeting already produced one.
    force: bool = False


def create_meeting_router(project_root: Path) -> APIRouter:
    # Load persisted meetings from disk on startup
    load_meetings_on_startup(project_root)

    router = APIRouter()

    def _root_for(meeting: Meeting, fallback: Path) -> Path:
        """Resolve the project root a meeting lives in.

        A meeting carries its own ``project_dir`` so it can be started/continued
        even when the server is currently serving a different project. Fall back
        to the server root when unknown or missing.
        """
        if meeting.project_dir:
            p = Path(meeting.project_dir)
            if p.is_dir():
                return p
        return fallback

    @router.get("/api/meetings")
    async def api_list_meetings():
        meetings = list_meetings(project_root)
        return [m.to_dict() for m in meetings]

    @router.get("/api/meetings/default-agents")
    async def api_default_agents():
        return DEFAULT_AGENTS

    @router.get("/api/meetings/models")
    async def api_meeting_models(runtime: str = ""):
        """Return selectable model identifiers for a meeting runtime."""
        from aqm.runtime.model_catalog import list_models

        return list_models(runtime)

    @router.post("/api/meetings")
    async def api_create_meeting(req: CreateMeetingRequest):
        if not req.agents:
            req.agents = [MeetingAgentConfig(**a) for a in DEFAULT_AGENTS]
        if not req.agents:
            raise HTTPException(400, "至少需要一个Agent")

        meeting = create_meeting(
            topic=req.topic,
            description=req.description,
            agents=[a.model_dump() for a in req.agents],
            shared_file=req.shared_file,
            rounds=req.rounds,
            timeout_per_agent=req.timeout_per_agent,
            project_root=project_root,
        )
        return meeting.to_dict()

    @router.get("/api/meetings/{meeting_id}")
    async def api_get_meeting(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        return meeting.to_dict()

    @router.patch("/api/meetings/{meeting_id}")
    async def api_update_meeting(meeting_id: str, req: CreateMeetingRequest):
        from aqm.meeting import update_meeting, get_meeting as get_m
        m = get_m(meeting_id, project_root)
        if not m:
            raise HTTPException(404, "会议未找到")
        if m.status == "running":
            raise HTTPException(400, "会议正在运行中，请先停止")
        root = _root_for(m, project_root)
        update_meeting(
            meeting_id,
            topic=req.topic,
            description=req.description,
            agents=[a.model_dump() for a in req.agents],
            shared_file=req.shared_file,
            rounds=req.rounds,
            timeout_per_agent=req.timeout_per_agent,
            project_root=root,
        )
        updated = get_m(meeting_id, project_root)
        return updated.to_dict()

    @router.delete("/api/meetings/{meeting_id}")
    async def api_delete_meeting(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        if meeting.status == "running":
            raise HTTPException(400, "会议正在运行中，请先停止")
        root = _root_for(meeting, project_root)
        delete_meeting(meeting_id, root)
        return {"deleted": True}

    @router.post("/api/meetings/{meeting_id}/start")
    async def api_start_meeting(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        if meeting.status == "running":
            raise HTTPException(400, "会议已在运行中")
        root = _root_for(meeting, project_root)
        start_meeting(meeting_id, root)
        return {"meeting_id": meeting_id, "status": "started"}

    @router.post("/api/meetings/{meeting_id}/stop")
    async def api_stop_meeting(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        root = _root_for(meeting, project_root)
        ok = stop_meeting(meeting_id, root)
        if not ok:
            raise HTTPException(400, "会议未在运行中")
        return {"meeting_id": meeting_id, "status": "stopping"}

    @router.post("/api/meetings/{meeting_id}/speak")
    async def api_meeting_speak(meeting_id: str, req: HumanSpeechRequest):
        """Submit a human message at any time during a running meeting."""
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        root = _root_for(meeting, project_root)
        try:
            return submit_human_speech(
                meeting_id,
                root,
                content=req.content,
                agent_id=req.agent_id,
                name=req.name,
                skip=req.skip,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.post("/api/meetings/{meeting_id}/continue")
    async def api_continue_meeting(meeting_id: str, req: ContinueMeetingRequest | None = None):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        if meeting.status == "running":
            raise HTTPException(400, "会议正在运行中")
        root = _root_for(meeting, project_root)
        continued = continue_meeting(
            meeting_id,
            root,
            additional_rounds=req.additional_rounds if req else 0,
        )
        if not continued:
            raise HTTPException(400, "会议正在运行中")
        return continued.to_dict()

    @router.get("/api/meetings/{meeting_id}/file")
    async def api_get_shared_file(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        root = _root_for(meeting, project_root)
        content = get_shared_file_content(meeting, root)
        return {
            "content": content,
            "path": str(get_shared_file_path(meeting, root)),
        }

    # ── Requirements handoff ───────────────────────────────────────────

    @router.get("/api/meetings/{meeting_id}/requirements")
    async def api_get_requirements(meeting_id: str):
        from aqm.meeting import (
            get_requirements_content,
            get_requirements_path,
        )

        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        root = _root_for(meeting, project_root)
        path = get_requirements_path(meeting, root)
        return {
            "exists": path.exists(),
            "path": str(path),
            "content": get_requirements_content(meeting, root),
            "status": meeting._requirements_status,
            "error": meeting._requirements_error,
        }

    @router.post("/api/meetings/{meeting_id}/requirements")
    async def api_generate_requirements(meeting_id: str):
        import threading

        from aqm.meeting import generate_requirements_bg

        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        if meeting.status == "running":
            raise HTTPException(400, "会议正在运行中，请结束后再生成需求文档")
        if meeting._requirements_status == "running":
            return {"meeting_id": meeting_id, "status": "running"}
        root = _root_for(meeting, project_root)
        meeting._requirements_status = "running"
        meeting._requirements_error = ""
        threading.Thread(
            target=generate_requirements_bg,
            args=(meeting_id, root),
            daemon=True,
        ).start()
        return {"meeting_id": meeting_id, "status": "started"}

    @router.put("/api/meetings/{meeting_id}/requirements")
    async def api_save_requirements(meeting_id: str, req: RequirementsSaveRequest):
        from aqm.meeting import get_requirements_path

        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        root = _root_for(meeting, project_root)
        path = get_requirements_path(meeting, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(req.content, encoding="utf-8")
        meeting._requirements_status = "ready"
        meeting._requirements_error = ""
        return {"meeting_id": meeting_id, "path": str(path), "saved": True}

    @router.get("/api/meetings/{meeting_id}/tasks")
    async def api_meeting_tasks(meeting_id: str):
        """Tasks that were created from this meeting's requirements."""
        from aqm.web.task_projects import list_project_tasks

        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        root = _root_for(meeting, project_root)
        tasks = [
            task
            for task in list_project_tasks(root)
            if (task.metadata or {}).get("source_meeting_id") == meeting.id
        ]
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return [
            {
                "id": task.id,
                "status": task.status.value,
                "pipeline": (task.metadata or {}).get("pipeline"),
                "stage_count": len(task.stages),
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
            }
            for task in tasks
        ]

    @router.post("/api/meetings/{meeting_id}/handoff")
    async def api_handoff_to_task(meeting_id: str, req: HandoffRequest | None = None):
        from aqm.meeting import get_requirements_content, get_requirements_path
        from aqm.web.task_projects import list_project_tasks

        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        if meeting.status == "running":
            raise HTTPException(400, "会议正在运行中，请先停止或结束")

        root = _root_for(meeting, project_root)
        content = get_requirements_content(meeting, root).strip()
        if not content:
            raise HTTPException(
                400,
                "需求文档尚未生成，请先生成或手工填写 REQUIREMENTS.md",
            )

        # Prevent accidental duplicates: one meeting normally maps to one task.
        existing = [
            task
            for task in list_project_tasks(root)
            if (task.metadata or {}).get("source_meeting_id") == meeting.id
        ]
        active_statuses = {
            "pending",
            "in_progress",
            "awaiting_gate",
            "awaiting_human_input",
        }
        active = [task for task in existing if task.status.value in active_statuses]
        if active:
            latest = max(active, key=lambda task: task.created_at)
            raise HTTPException(
                409,
                detail={
                    "code": "task_active",
                    "task_id": latest.id,
                    "status": latest.status.value,
                    "message": (
                        f"该会议已有正在运行的任务 {latest.id}"
                        f"（{latest.status.value}），请先处理或取消后再创建"
                    ),
                },
            )
        if existing and not (req and req.force):
            latest = max(existing, key=lambda task: task.created_at)
            raise HTTPException(
                409,
                detail={
                    "code": "task_exists",
                    "task_id": latest.id,
                    "status": latest.status.value,
                    "count": len(existing),
                    "message": (
                        f"该会议已创建过 {len(existing)} 个任务，最新为 {latest.id}"
                        f"（{latest.status.value}）；如确需重新创建，请确认后重试"
                    ),
                },
            )

        from aqm.web.api.tasks import start_pipeline_run

        try:
            from aqm.core.config import add_recent_project

            add_recent_project(root)
        except Exception:
            pass

        description = (
            f"根据会议《{meeting.topic}》产出的需求文档开发项目。\n"
            f"需求文档路径：{get_requirements_path(meeting, root)}\n\n"
            f"{content}"
        )
        try:
            task = start_pipeline_run(
                root,
                description,
                pipeline=req.pipeline if req else None,
                metadata={
                    "source_meeting_id": meeting.id,
                    "source_meeting_topic": meeting.topic,
                    "requirements_path": str(get_requirements_path(meeting, root)),
                },
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {
            "meeting_id": meeting_id,
            "task_id": task.id,
            "status": "started",
        }

    @router.get("/api/meetings/{meeting_id}/events")
    async def meeting_events(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")

        import asyncio

        async def event_generator():
            q = subscribe_meeting(meeting_id)
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=30)
                        yield f"event: {event['event']}\ndata: {event['data']}\n\n"
                        if event["event"] in ("meeting_complete",):
                            return
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                unsubscribe_meeting(meeting_id, q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return router
