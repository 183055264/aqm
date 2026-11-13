"""Pipeline CRUD API router."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CreatePipelineRequest(BaseModel):
    name: str
    content: str


class UpdatePipelineRequest(BaseModel):
    content: str


class DuplicateRequest(BaseModel):
    new_name: str


class SetDefaultRequest(BaseModel):
    name: str


class AgentRequest(BaseModel):
    id: str
    type: str = "agent"  # agent | session
    name: str = ""
    runtime: str = "claude"
    system_prompt: str = ""
    model: str | None = None
    handoffs: list[dict] = Field(default_factory=list)
    gate: dict | None = None
    mcp: list[dict] = Field(default_factory=list)
    context_strategy: str = "both"
    human_input: dict | None = None
    participants: list[str] = Field(default_factory=list)
    max_rounds: int | None = None
    summary_agent: str | None = None
    consensus: dict | None = None
    chunks: dict | None = None


def create_pipelines_router(project_root: Path) -> APIRouter:
    from aqm.core.project import (
        delete_pipeline,
        get_default_pipeline,
        get_pipeline_path,
        list_pipelines,
        save_pipeline,
        set_default_pipeline,
    )

    router = APIRouter()

    @router.get("/api/pipelines")
    async def api_list_pipelines():
        pipelines = list_pipelines(project_root)
        default = get_default_pipeline(project_root) or "default"
        result = []
        for name in pipelines:
            try:
                path = get_pipeline_path(project_root, name)
                agent_count = 0
                content = path.read_text(encoding="utf-8")
                agent_count = content.count("- id:")
                result.append({
                    "name": name,
                    "agent_count": agent_count,
                    "is_default": name == default,
                })
            except FileNotFoundError:
                result.append({"name": name, "agent_count": 0, "is_default": name == default})
        return {"pipelines": result, "default": default}

    @router.get("/api/pipelines/{name}")
    async def api_get_pipeline(name: str):
        try:
            path = get_pipeline_path(project_root, name)
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{name}' not found")
        content = path.read_text(encoding="utf-8")
        default = get_default_pipeline(project_root) or "default"
        return {
            "name": name,
            "content": content,
            "is_default": name == default,
        }

    @router.post("/api/pipelines")
    async def api_create_pipeline(req: CreatePipelineRequest):
        pipelines = list_pipelines(project_root)
        if req.name in pipelines:
            raise HTTPException(409, f"Pipeline '{req.name}' already exists")
        if not req.name.strip():
            raise HTTPException(400, "Pipeline name cannot be empty")
        save_pipeline(project_root, req.name, req.content)
        return {"name": req.name, "status": "created"}

    @router.put("/api/pipelines/{name}")
    async def api_update_pipeline(name: str, req: UpdatePipelineRequest):
        try:
            get_pipeline_path(project_root, name)
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{name}' not found")
        save_pipeline(project_root, name, req.content)
        return {"name": name, "status": "updated"}

    @router.delete("/api/pipelines/{name}")
    async def api_delete_pipeline(name: str):
        try:
            delete_pipeline(project_root, name)
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{name}' not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": name, "status": "deleted"}

    @router.post("/api/pipelines/{name}/duplicate")
    async def api_duplicate_pipeline(name: str, req: DuplicateRequest):
        try:
            path = get_pipeline_path(project_root, name)
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{name}' not found")
        pipelines = list_pipelines(project_root)
        if req.new_name in pipelines:
            raise HTTPException(409, f"Pipeline '{req.new_name}' already exists")
        content = path.read_text(encoding="utf-8")
        save_pipeline(project_root, req.new_name, content)
        return {"name": req.new_name, "status": "created"}

    @router.get("/api/pipelines/{name}/yaml")
    async def api_download_yaml(name: str):
        try:
            path = get_pipeline_path(project_root, name)
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{name}' not found")
        content = path.read_text(encoding="utf-8")
        return PlainTextResponse(
            content,
            media_type="text/yaml",
            headers={"Content-Disposition": f"attachment; filename={name}.yaml"},
        )

    @router.post("/api/pipelines/default")
    async def api_set_default(req: SetDefaultRequest):
        pipelines = list_pipelines(project_root)
        if req.name not in pipelines:
            raise HTTPException(404, f"Pipeline '{req.name}' not found")
        set_default_pipeline(project_root, req.name)
        return {"default": req.name}

    # ── Agent CRUD within a pipeline ─────────────────────────────────

    def _load_pipeline_yaml(name: str) -> tuple[dict, str]:
        """Load and parse pipeline YAML. Returns (data, raw_content)."""
        import yaml as _yaml
        try:
            path = get_pipeline_path(project_root, name)
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{name}' not found")
        content = path.read_text(encoding="utf-8")
        data = _yaml.safe_load(content) or {}
        return data, content

    def _save_pipeline_yaml(name: str, data: dict) -> None:
        """Serialize data back to YAML and save."""
        import yaml as _yaml
        content = _yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        save_pipeline(project_root, name, content)

    @router.get("/api/pipelines/{name}/agents")
    async def api_list_agents(name: str):
        data, _ = _load_pipeline_yaml(name)
        agents = data.get("agents", [])
        return [
            {
                "id": a.get("id", ""),
                "type": a.get("type", "agent"),
                "name": a.get("name", ""),
                "runtime": a.get("runtime", ""),
                "model": a.get("model", ""),
                "system_prompt": a.get("system_prompt", ""),
                "handoffs": a.get("handoffs", []),
                "gate": a.get("gate"),
                "mcp": a.get("mcp", []),
                "context_strategy": a.get("context_strategy", "both"),
                "human_input": a.get("human_input"),
                "participants": a.get("participants", []),
                "max_rounds": a.get("max_rounds"),
                "summary_agent": a.get("summary_agent"),
                "consensus": a.get("consensus"),
                "chunks": a.get("chunks"),
            }
            for a in agents
        ]

    @router.post("/api/pipelines/{name}/agents")
    async def api_add_agent(name: str, req: AgentRequest):
        data, _ = _load_pipeline_yaml(name)
        agents = data.get("agents", [])
        # Check for duplicate ID
        if any(a.get("id") == req.id for a in agents):
            raise HTTPException(409, f"Agent '{req.id}' already exists")
        agent_dict = req.model_dump(exclude_none=True)
        if not agent_dict.get("model"):
            agent_dict.pop("model", None)
        if agent_dict.get("type", "agent") == "agent":
            for key in ("type", "participants", "max_rounds", "summary_agent", "consensus"):
                agent_dict.pop(key, None)
        if not req.gate:
            agent_dict.pop("gate", None)
        if not req.mcp:
            agent_dict.pop("mcp", None)
        if req.context_strategy == "both":
            agent_dict.pop("context_strategy", None)
        if not req.human_input:
            agent_dict.pop("human_input", None)
        if not req.chunks:
            agent_dict.pop("chunks", None)
        elif req.chunks.get("enabled") is True and len(req.chunks) == 1:
            agent_dict.pop("chunks", None)
        agents.append(agent_dict)
        data["agents"] = agents
        _save_pipeline_yaml(name, data)
        return {"status": "added", "agent_id": req.id}

    @router.put("/api/pipelines/{name}/agents/{agent_id}")
    async def api_update_agent(name: str, agent_id: str, req: AgentRequest):
        data, _ = _load_pipeline_yaml(name)
        agents = data.get("agents", [])
        idx = next((i for i, a in enumerate(agents) if a.get("id") == agent_id), None)
        if idx is None:
            raise HTTPException(404, f"Agent '{agent_id}' not found")
        if req.id != agent_id:
            raise HTTPException(400, "Agent ID cannot be changed")
        agent_dict = dict(agents[idx])
        updates = req.model_dump(exclude_unset=True)
        cleared: list[str] = []
        old_runtime = agent_dict.get("runtime")
        if "runtime" in updates and updates.get("runtime") != old_runtime:
            # cli_flags / model are runtime-specific (e.g. Claude's
            # --allowedTools is rejected by opencode/kilo/cline). Drop stale
            # values so switching runtime in the UI cannot break startup.
            for key in ("cli_flags", "model"):
                if key in agent_dict:
                    agent_dict.pop(key, None)
                    if not updates.get(key):
                        cleared.append(key)
        for field in (
            "type", "name", "runtime", "system_prompt", "model", "handoffs",
            "gate", "mcp", "context_strategy", "human_input", "participants",
            "max_rounds", "summary_agent", "consensus", "chunks",
        ):
            if field not in updates:
                continue
            value = updates[field]
            if field in ("gate", "human_input", "consensus", "chunks") and value is None:
                agent_dict.pop(field, None)
            elif field == "model" and not value:
                agent_dict.pop(field, None)
            else:
                agent_dict[field] = value
        # Switching a session back to a normal agent drops session-only fields.
        if agent_dict.get("type", "agent") == "agent":
            for key in ("type", "participants", "max_rounds", "summary_agent", "consensus"):
                agent_dict.pop(key, None)
        elif agent_dict.get("type") == "session" and not agent_dict.get("participants"):
            agent_dict["participants"] = []
        if not agent_dict.get("chunks"):
            agent_dict.pop("chunks", None)
        agents[idx] = agent_dict
        data["agents"] = agents
        _save_pipeline_yaml(name, data)
        return {"status": "updated", "agent_id": req.id, "cleared": cleared}

    @router.delete("/api/pipelines/{name}/agents/{agent_id}")
    async def api_delete_agent(name: str, agent_id: str):
        data, _ = _load_pipeline_yaml(name)
        agents = data.get("agents", [])
        new_agents = [a for a in agents if a.get("id") != agent_id]
        if len(new_agents) == len(agents):
            raise HTTPException(404, f"Agent '{agent_id}' not found")
        data["agents"] = new_agents
        _save_pipeline_yaml(name, data)
        return {"status": "deleted", "agent_id": agent_id}

    return router
