"""Meeting API endpoints — CRUD, start/stop, SSE."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from aqm.meeting import (
    Meeting,
    MeetingAgent,
    DEFAULT_AGENTS,
    broadcast_meeting_event,
    create_meeting,
    delete_meeting,
    get_meeting,
    get_shared_file_content,
    list_meetings,
    load_meetings_on_startup,
    start_meeting,
    stop_meeting,
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


class CreateMeetingRequest(BaseModel):
    topic: str
    description: str = ""
    agents: list[MeetingAgentConfig] = []
    shared_file: str = "meeting.md"
    rounds: int = 1
    timeout_per_agent: int = 300


def create_meeting_router(project_root: Path) -> APIRouter:
    # Load persisted meetings from disk on startup
    load_meetings_on_startup(project_root)

    router = APIRouter()

    @router.get("/api/meetings")
    async def api_list_meetings():
        meetings = list_meetings(project_root)
        return [m.to_dict() for m in meetings]

    @router.get("/api/meetings/default-agents")
    async def api_default_agents():
        return DEFAULT_AGENTS

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

    @router.delete("/api/meetings/{meeting_id}")
    async def api_delete_meeting(meeting_id: str):
        ok = delete_meeting(meeting_id, project_root)
        if not ok:
            meeting = get_meeting(meeting_id, project_root)
            if meeting and meeting.status == "running":
                raise HTTPException(400, "会议正在运行中，请先停止")
            raise HTTPException(404, "会议未找到")
        return {"deleted": True}

    @router.post("/api/meetings/{meeting_id}/start")
    async def api_start_meeting(meeting_id: str):
        ok = start_meeting(meeting_id, project_root)
        if not ok:
            meeting = get_meeting(meeting_id, project_root)
            if not meeting:
                raise HTTPException(404, "会议未找到")
            if meeting.status == "running":
                raise HTTPException(400, "会议已在运行中")
        return {"meeting_id": meeting_id, "status": "started"}

    @router.post("/api/meetings/{meeting_id}/stop")
    async def api_stop_meeting(meeting_id: str):
        ok = stop_meeting(meeting_id, project_root)
        if not ok:
            raise HTTPException(400, "会议未在运行中")
        return {"meeting_id": meeting_id, "status": "stopping"}

    @router.get("/api/meetings/{meeting_id}/file")
    async def api_get_shared_file(meeting_id: str):
        meeting = get_meeting(meeting_id, project_root)
        if not meeting:
            raise HTTPException(404, "会议未找到")
        content = get_shared_file_content(meeting, project_root)
        return {"content": content}

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
