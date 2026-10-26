"""Project configuration storage for aqm.

Stores default project path and other user preferences.
Configuration is stored in ~/.aqm/config.json (global) or .aqm/config.yaml (project-local).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Global config directory
GLOBAL_CONFIG_DIR = Path.home() / ".aqm"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"


@dataclass
class ProjectConfig:
    """Project-level configuration stored in .aqm/config.yaml."""
    default_pipeline: str | None = None
    pipeline: dict = field(default_factory=lambda: {"max_stages": 20})
    gate: dict = field(default_factory=dict)


def _ensure_config_dir() -> None:
    """Ensure global config directory exists."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load configuration from global config file."""
    if not GLOBAL_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(GLOBAL_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load config: %s", e)
        return {}


def save_config(config: dict) -> None:
    """Save configuration to global config file."""
    _ensure_config_dir()
    GLOBAL_CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_default_project() -> Optional[Path]:
    """Get the default project path from config."""
    config = load_config()
    path_str = config.get("default_project")
    if path_str:
        path = Path(path_str).resolve()
        if (path / ".aqm").is_dir():
            return path
    return None


def set_default_project(project_path: Path) -> None:
    """Set the default project path in config."""
    config = load_config()
    config["default_project"] = str(project_path.resolve())
    save_config(config)


def clear_default_project() -> None:
    """Clear the default project path from config."""
    config = load_config()
    config.pop("default_project", None)
    save_config(config)


def get_recent_projects() -> list[Path]:
    """Get list of recent projects from config."""
    config = load_config()
    projects = config.get("recent_projects", [])
    result = []
    for p in projects:
        path = Path(p).resolve()
        if (path / ".aqm").is_dir():
            result.append(path)
    return result


def add_recent_project(project_path: Path) -> None:
    """Add a project to recent projects list (max 10)."""
    config = load_config()
    projects = config.get("recent_projects", [])
    path_str = str(project_path.resolve())
    
    # Remove if already exists
    projects = [p for p in projects if p != path_str]
    
    # Add to front
    projects.insert(0, path_str)
    
    # Keep max 10
    config["recent_projects"] = projects[:10]
    save_config(config)


def load_project_config(root: Path) -> ProjectConfig:
    """Load project configuration from .aqm/config.yaml."""
    config_file = root / ".aqm" / "config.yaml"
    if not config_file.exists():
        return ProjectConfig()
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        return ProjectConfig(
            default_pipeline=data.get("default_pipeline"),
            pipeline=data.get("pipeline", {"max_stages": 20}),
            gate=data.get("gate", {}),
        )
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Failed to load project config: %s", e)
        return ProjectConfig()


def save_project_config(root: Path, config: ProjectConfig) -> None:
    """Save project configuration to .aqm/config.yaml."""
    config_file = root / ".aqm" / "config.yaml"
    data = {
        "default_pipeline": config.default_pipeline,
        "pipeline": config.pipeline,
        "gate": config.gate,
    }
    config_file.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
