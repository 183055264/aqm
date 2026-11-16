"""Global team templates, independent of any project directory.

Templates live in ``<aqm install root>/templates/<name>.yaml`` and only store the
member skeleton (id / name / runtime / role / system_prompt).  A meeting or a
task that uses a template freezes its own copy when it is created, so editing a
template later never rewrites existing records.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# File-name safe: no path separators, no leading dash.
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def templates_root() -> Path:
    """Directory holding the global templates.

    Defaults to ``<installation root>/templates`` so it follows the software,
    not the current working directory or a project.  ``AQM_TEMPLATES_DIR``
    overrides it (used by tests and by users who want a private library).
    When the installation directory is read-only (e.g. a system-wide install)
    we fall back to ``~/.aqm/templates`` so saving still works.
    """
    override = os.environ.get("AQM_TEMPLATES_DIR")
    if override:
        return Path(override).expanduser()
    # aqm/core/team_templates.py -> aqm/ -> installation root
    install = Path(__file__).resolve().parents[2] / "templates"
    if install.is_dir():
        return install
    try:
        install.mkdir(parents=True, exist_ok=True)
        if os.access(install, os.W_OK):
            return install
    except OSError:
        pass
    fallback = Path.home() / ".aqm" / "templates"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _TEMPLATE_NAME_RE.match(name):
        raise ValueError("模板名只能包含字母、数字、点、下划线和短横线（1-64 字符）")
    return name


def template_path(name: str, root: Path | None = None) -> Path:
    return (root or templates_root()) / f"{validate_name(name)}.yaml"


def list_templates(root: Path | None = None) -> list[dict[str, Any]]:
    """Return every readable template, sorted by name."""
    base = root or templates_root()
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Skipping unreadable team template %s: %s", path, e)
            continue
        agents = data.get("agents") or []
        items.append({
            "name": path.stem,
            "description": data.get("description") or "",
            "agent_count": len(agents),
            "agents": agents,
        })
    return items


def load_template(name: str, root: Path | None = None) -> dict[str, Any] | None:
    """Load one template, or ``None`` when it does not exist."""
    path = template_path(name, root)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as e:
        raise ValueError(f"模板 {name} 无法解析：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"模板 {name} 格式错误：顶层必须是映射")
    data.setdefault("name", path.stem)
    data.setdefault("agents", [])
    return data


def save_template(
    name: str,
    data: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Create or replace a template. Raises ``ValueError`` on bad input."""
    name = validate_name(name)
    agents: list[dict[str, str]] = []
    seen: set[str] = set()
    for agent in data.get("agents") or []:
        agent_id = str(agent.get("id") or "").strip()
        runtime = str(agent.get("runtime") or "").strip()
        if not agent_id or not runtime:
            raise ValueError("每个成员都必须有 id 和 runtime")
        if agent_id in seen:
            raise ValueError(f"成员 id 重复：{agent_id}")
        seen.add(agent_id)
        agents.append({
            "id": agent_id,
            "name": str(agent.get("name") or agent_id),
            "runtime": runtime,
            "role": str(agent.get("role") or ""),
            "system_prompt": str(agent.get("system_prompt") or ""),
        })
    if not agents:
        raise ValueError("模板至少需要一个成员")

    payload = {
        "name": name,
        "description": str(data.get("description") or ""),
        "agents": agents,
    }
    path = template_path(name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(payload, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return payload


def delete_template(name: str, root: Path | None = None) -> bool:
    path = template_path(name, root)
    if not path.exists():
        return False
    path.unlink()
    return True


def resolve_agents(template: dict[str, Any]) -> list[dict[str, str]]:
    """Expand a template into concrete members.

    Name / role / model fall back to the built-in defaults for the same id (or
    runtime) so a skeleton stays usable.  ``system_prompt`` is returned exactly
    as stored: an empty prompt means "let the runtime use its own default", and
    the UI must show that emptiness rather than silently filling it in.
    """
    from aqm.meeting import DEFAULT_AGENTS

    by_id = {a["id"]: a for a in DEFAULT_AGENTS}
    by_runtime = {a["runtime"]: a for a in DEFAULT_AGENTS}
    resolved: list[dict[str, str]] = []
    for agent in template.get("agents") or []:
        base = by_id.get(agent.get("id")) or by_runtime.get(agent.get("runtime")) or {}
        resolved.append({
            "id": str(agent.get("id") or ""),
            "name": str(agent.get("name") or base.get("name") or agent.get("id") or ""),
            "runtime": str(agent.get("runtime") or ""),
            "role": str(agent.get("role") or base.get("role") or ""),
            "system_prompt": str(agent.get("system_prompt") or ""),
            "model": str(agent.get("model") or base.get("model") or ""),
        })
    return resolved


def write_task_snapshot(task_dir: Path, agents: dict) -> Path | None:
    """Persist the members a task was created with.

    ``agents`` is the pipeline's ``{id: AgentDefinition}`` mapping.  The file is
    a frozen copy, so re-reading the task later never depends on the current
    pipeline YAML or on a team template that may have changed.  Empty fields
    fall back to the built-in defaults for the same id/runtime.
    """
    from aqm.meeting import DEFAULT_AGENTS

    by_id = {a["id"]: a for a in DEFAULT_AGENTS}
    by_runtime = {a["runtime"]: a for a in DEFAULT_AGENTS}
    members = []
    for agent in agents.values():
        if getattr(agent, "type", "agent") != "agent":
            continue
        runtime = agent.runtime or ""
        base = by_id.get(agent.id) or by_runtime.get(runtime) or {}
        members.append({
            "id": agent.id,
            "name": agent.name or base.get("name") or agent.id,
            "runtime": runtime,
            "role": base.get("role", ""),
            "system_prompt": agent.system_prompt or base.get("system_prompt", ""),
            "model": agent.model or base.get("model", "") or "",
        })
    if not members:
        return None
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "team_snapshot.yaml"
    path.write_text(
        yaml.dump(
            {"agents": members},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path
