"""Global team-template CRUD API (project independent)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TemplateAgent(BaseModel):
    id: str
    name: str = ""
    runtime: str = "claude"
    role: str = ""
    system_prompt: str = ""


class TemplatePayload(BaseModel):
    name: str = ""
    description: str = ""
    agents: list[TemplateAgent] = Field(default_factory=list)


class SetProjectTemplateRequest(BaseModel):
    name: str = ""


def create_team_templates_router(project_root: Path) -> APIRouter:
    from aqm.core import team_templates as store
    from aqm.core.config import load_project_config, save_project_config

    router = APIRouter()

    def _current_default() -> str:
        return load_project_config(project_root).team_template or ""

    @router.get("/api/team-templates")
    async def api_list_templates():
        items = [
            {
                "name": item["name"],
                "description": item["description"],
                "agent_count": item["agent_count"],
            }
            for item in store.list_templates()
        ]
        return {"items": items, "default": _current_default(), "root": str(store.templates_root())}

    @router.get("/api/team-templates/{name}")
    async def api_get_template(name: str):
        try:
            data = store.load_template(name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if data is None:
            raise HTTPException(404, f"Team template '{name}' not found")
        return data

    @router.get("/api/team-templates/{name}/agents")
    async def api_template_agents(name: str):
        try:
            data = store.load_template(name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if data is None:
            raise HTTPException(404, f"Team template '{name}' not found")
        return {"agents": store.resolve_agents(data)}

    @router.post("/api/team-templates")
    async def api_create_template(req: TemplatePayload):
        try:
            store.validate_name(req.name)
            if store.load_template(req.name) is not None:
                raise HTTPException(409, f"Team template '{req.name}' already exists")
            payload = store.save_template(
                req.name,
                {"description": req.description, "agents": [a.model_dump() for a in req.agents]},
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"name": payload["name"], "status": "created"}

    @router.put("/api/team-templates/{name}")
    async def api_update_template(name: str, req: TemplatePayload):
        try:
            if store.load_template(name) is None:
                raise HTTPException(404, f"Team template '{name}' not found")
            payload = store.save_template(
                name,
                {"description": req.description, "agents": [a.model_dump() for a in req.agents]},
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"name": payload["name"], "status": "updated"}

    @router.delete("/api/team-templates/{name}")
    async def api_delete_template(name: str):
        try:
            deleted = store.delete_template(name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not deleted:
            raise HTTPException(404, f"Team template '{name}' not found")
        # Do not leave the current project pointing at a template that is gone.
        if _current_default() == name:
            config = load_project_config(project_root)
            config.team_template = None
            save_project_config(project_root, config)
        return {"name": name, "status": "deleted"}

    @router.post("/api/team-templates/default")
    async def api_set_project_default(req: SetProjectTemplateRequest):
        """Remember which global template this project starts from."""
        if req.name:
            try:
                if store.load_template(req.name) is None:
                    raise HTTPException(404, f"Team template '{req.name}' not found")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        config = load_project_config(project_root)
        config.team_template = req.name or None
        save_project_config(project_root, config)
        return {"default": req.name}

    return router
