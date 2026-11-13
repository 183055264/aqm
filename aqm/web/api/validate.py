"""Validate API endpoint — JSON Schema validation for any project pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqm.core.project import (
    get_agents_yaml_path,
    get_default_pipeline,
    get_pipeline_path,
    list_pipelines,
)


class ValidateRequest(BaseModel):
    yaml_content: Optional[str] = None
    strict: bool = False
    pipeline: Optional[str] = None


def _resolve_pipeline(project_root: Path, pipeline: Optional[str]) -> tuple[Path, str]:
    """Resolve a pipeline name (or the current default) to its YAML path.

    Resolved per request so that switching the default pipeline takes effect
    immediately instead of being frozen at server startup.
    """
    if pipeline:
        try:
            return get_pipeline_path(project_root, pipeline), pipeline
        except FileNotFoundError:
            raise HTTPException(404, f"Pipeline '{pipeline}' not found")
    name = get_default_pipeline(project_root) or "default"
    return get_agents_yaml_path(project_root), name


def create_validate_router(project_root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/validate/pipelines")
    async def api_validate_pipelines():
        default = get_default_pipeline(project_root) or "default"
        items = []
        for name in list_pipelines(project_root):
            try:
                path = get_pipeline_path(project_root, name)
                items.append({
                    "name": name,
                    "is_default": name == default,
                    "path": str(path),
                    "exists": path.exists(),
                })
            except FileNotFoundError:
                items.append({
                    "name": name,
                    "is_default": name == default,
                    "path": "",
                    "exists": False,
                })
        return {"pipelines": items, "default": default}

    @router.post("/api/validate")
    async def api_validate(req: ValidateRequest = None):
        import yaml as _yaml

        # Use provided content or read the selected pipeline from disk
        if req and req.yaml_content:
            content = req.yaml_content
            pipeline_name = (req.pipeline if req else None) or "(inline)"
            path = None
        else:
            path, pipeline_name = _resolve_pipeline(
                project_root, req.pipeline if req else None
            )
            if not path.exists():
                raise HTTPException(
                    400,
                    f"No YAML found for pipeline '{pipeline_name}' "
                    f"(expected {path}); no content provided",
                )
            content = path.read_text(encoding="utf-8")

        # Read YAML
        try:
            data = _yaml.safe_load(content)
        except Exception as e:
            return {
                "valid": False,
                "errors": [{"path": "(root)", "message": f"Failed to parse YAML: {e}", "fix": "Check YAML syntax."}],
                "summary": {},
                "pipeline": pipeline_name,
                "source_path": str(path) if path else "",
                "yaml_content": content,
            }

        if not isinstance(data, dict):
            return {
                "valid": False,
                "errors": [{"path": "(root)", "message": "agents.yaml must be a YAML mapping", "fix": "Ensure the file starts with a mapping structure."}],
                "summary": {},
                "pipeline": pipeline_name,
                "source_path": str(path) if path else "",
                "yaml_content": content,
            }

        # Load JSON Schema — check package-internal path first, then project root
        schema_path = Path(__file__).resolve().parent.parent.parent / "schema" / "agents-schema.json"
        if not schema_path.exists():
            schema_path = Path(__file__).resolve().parent.parent.parent.parent / "schema" / "agents-schema.json"
        if not schema_path.exists():
            raise HTTPException(500, "JSON Schema file not found")

        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)

        # Validate
        try:
            from jsonschema import Draft7Validator
        except ImportError:
            raise HTTPException(500, "jsonschema package not installed")

        validator = Draft7Validator(schema)
        raw_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))

        errors = []
        for err in raw_errors:
            path_str = " → ".join(str(p) for p in err.absolute_path) or "(root)"
            fix = ""
            if "is a required property" in err.message:
                prop = err.message.split("'")[1]
                fix = f"Add the '{prop}' field."
            elif "is not one of" in err.message:
                fix = "Use one of the allowed values."
            elif "Additional properties are not allowed" in err.message:
                fix = "Remove unrecognized fields."
            errors.append({"path": path_str, "message": err.message, "fix": fix})

        # Summary
        agents_list = data.get("agents", [])
        features = []
        if any(a.get("gate") for a in agents_list):
            features.append("gates")
        if any(a.get("mcp") for a in agents_list):
            features.append("MCP servers")
        if any(a.get("handoffs") for a in agents_list):
            features.append("handoffs")
        if data.get("params"):
            features.append(f"{len(data['params'])} param(s)")
        if data.get("imports"):
            features.append(f"{len(data['imports'])} import(s)")

        # Resource & permission warnings (only if schema is valid)
        warnings = []
        if not errors:
            from aqm.core.validate import run_all_checks

            raw_warnings = run_all_checks(data)
            warnings = [
                {"path": w.path, "message": w.message, "fix": w.fix}
                for w in raw_warnings
            ]

        strict = req.strict if req else False
        is_valid = len(errors) == 0 and (not strict or len(warnings) == 0)

        return {
            "valid": is_valid,
            "errors": errors,
            "warnings": warnings,
            "summary": {
                "agent_count": len(agents_list),
                "features": features,
            },
            "pipeline": pipeline_name,
            "source_path": str(path) if path else "",
            "yaml_content": content,
        }

    return router
