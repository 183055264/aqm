"""Helpers for Claude Code project trust settings."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
_trust_lock = threading.Lock()


def _claude_user_config_path() -> Path:
    return Path.home() / ".claude.json"


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).resolve())))


DEFAULT_THINKING_TOKENS = 512


def claude_thinking_tokens() -> int:
    """Thinking budget passed to Claude Code.

    Claude Code reads ``CLAUDE_CODE_DISABLE_THINKING`` / ``MAX_THINKING_TOKENS``
    from its settings file.  A global settings file can therefore suppress the
    ``thinking_delta`` stream even when the model supports it.  Callers append
    :func:`claude_thinking_args` to the CLI command so a project can opt in
    without editing the user's global config.
    """
    raw = os.environ.get("AQM_CLAUDE_THINKING_TOKENS", str(DEFAULT_THINKING_TOKENS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_THINKING_TOKENS
    return max(0, min(value, 32000))


def claude_thinking_args() -> list[str]:
    """Return ``--settings`` args that control Claude's extended thinking."""
    tokens = claude_thinking_tokens()
    if tokens <= 0:
        env = {"CLAUDE_CODE_DISABLE_THINKING": "1"}
    else:
        env = {
            "CLAUDE_CODE_DISABLE_THINKING": "0",
            "MAX_THINKING_TOKENS": str(tokens),
        }
    return ["--settings", json.dumps({"env": env})]


def should_pass_claude_model(model: str) -> bool:
    if not model:
        return False

    configured_models = {
        value
        for value in (
            os.environ.get("ANTHROPIC_MODEL"),
            os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
            os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
            os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        )
        if value
    }
    settings_path = _claude_settings_path()
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Unable to read Claude Code model settings from %s", settings_path)
        settings = {}

    if isinstance(settings, dict):
        if isinstance(settings.get("model"), str):
            configured_models.add(settings["model"])
        env_settings = settings.get("env")
        if isinstance(env_settings, dict):
            configured_models.update(
                value
                for key, value in env_settings.items()
                if key in (
                    "ANTHROPIC_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                )
                and isinstance(value, str)
                and value
            )

    return model not in configured_models


def trust_claude_project(project_root: Path) -> None:
    root = Path(project_root).resolve()
    if not (root / ".aqm").is_dir():
        raise ValueError(f"Refusing to trust a directory that is not an AQM project: {root}")

    config_path = _claude_user_config_path()
    with _trust_lock:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read Claude Code trust settings: {config_path}") from exc

        if not isinstance(config, dict):
            raise RuntimeError(f"Claude Code trust settings have an unexpected format: {config_path}")
        projects = config.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise RuntimeError(f"Claude Code project settings have an unexpected format: {config_path}")

        matching_key = next(
            (key for key in projects if _normalized_path(key) == _normalized_path(root)),
            str(root),
        )
        project_settings = projects.get(matching_key, {})
        if not isinstance(project_settings, dict):
            raise RuntimeError(f"Claude Code project settings have an unexpected format: {config_path}")
        if project_settings.get("hasTrustDialogAccepted") is True:
            return

        projects[matching_key] = {**project_settings, "hasTrustDialogAccepted": True}
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=config_path.parent,
                prefix=".claude-trust-",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(config, temp_file, ensure_ascii=False, indent=2)
                temp_file.write("\n")
            os.replace(temp_path, config_path)
        except OSError as exc:
            raise RuntimeError(f"Unable to save Claude Code trust settings: {config_path}") from exc
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

        logger.info("Trusted Claude Code project directory %s", root)
