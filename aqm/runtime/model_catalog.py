"""Discover selectable model identifiers for each meeting runtime.

The meeting UI needs concrete ``--model`` values for every runtime.  Each CLI
accepts a different identifier syntax, so the catalog is assembled from the
CLI itself when possible and from the runtime's configuration file otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RUNTIMES = ("claude", "opencode", "kilo", "cline")

_COMMAND_TIMEOUT_SECONDS = 20
_CACHE_TTL_SECONDS = 300.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

_CLAUDE_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

# Models backed by the environment credentials kilo already recognises
# (AGNES_API_KEY / SENSENOVA_API_KEY) come first: ``kilo/...`` premium models
# require ``kilo auth login`` and fail with PAID_MODEL_AUTH_REQUIRED otherwise.
_KILO_FALLBACK_MODELS = [
    "agnes/agnes-3.0-flash",
    "agnes/agnes-2.5-flash",
    "sensenova/sensenova-6.8-flash-lite",
    "sensenova/deepseek-v4.1-flash",
    "kilo/~anthropic/claude-sonnet-latest",
    "kilo/~deepseek/deepseek-v4-flash-latest",
]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Unable to read model configuration from %s", path)
        return None


def _run_command(command: list[str]) -> str:
    """Run a CLI model-listing command, returning its stdout ("" on failure)."""
    display = " ".join(command)
    try:
        if os.name == "nt":
            # opencode/kilo are .cmd shims on Windows, which need a shell.
            completed = subprocess.run(
                subprocess.list2cmdline(command),
                shell=True,
                capture_output=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                encoding="utf-8",
                errors="replace",
            )
        else:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                encoding="utf-8",
                errors="replace",
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Unable to run %r: %s", display, exc)
        return ""

    if completed.returncode != 0:
        logger.warning("%r exited with status %s", display, completed.returncode)
        return ""
    return completed.stdout or ""


def _parse_model_lines(output: str) -> list[str]:
    models: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        token = parts[0]
        if "/" not in token:
            continue
        models.append(token)
    return _dedupe(models)


def _opencode_models() -> tuple[list[str], str, str]:
    cli_models = _parse_model_lines(_run_command(["opencode", "models"]))
    config = _load_json(Path.home() / ".config" / "opencode" / "opencode.json")
    default = ""
    config_models: list[str] = []
    if isinstance(config, dict):
        configured_default = config.get("model")
        if isinstance(configured_default, str):
            default = configured_default.strip()
        providers = config.get("provider")
        if isinstance(providers, dict):
            for provider_id, provider in providers.items():
                if not isinstance(provider_id, str) or not isinstance(provider, dict):
                    continue
                provider_models = provider.get("models")
                if isinstance(provider_models, dict):
                    for model_key in provider_models:
                        if isinstance(model_key, str) and model_key:
                            config_models.append(f"{provider_id}/{model_key}")

    legacy = _load_json(Path.home() / ".opencode" / "config.json")
    if isinstance(legacy, dict):
        entries = legacy.get("models")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                model = entry.get("model")
                provider = entry.get("provider")
                if not isinstance(model, str) or not model.strip():
                    continue
                model = model.strip()
                if "/" in model:
                    config_models.append(model)
                elif isinstance(provider, str) and provider.strip():
                    config_models.append(f"{provider.strip()}/{model}")

    if cli_models:
        return cli_models, default, "cli"
    config_models = _dedupe(config_models)
    if config_models:
        return config_models, default, "config"
    return [], default, "none"


def _kilo_models() -> tuple[list[str], str, str]:
    cli_models = _parse_model_lines(_run_command(["kilo", "models"]))
    if cli_models:
        return cli_models, "", "cli"
    return list(_KILO_FALLBACK_MODELS), "", "fallback"


def _claude_models() -> tuple[list[str], str, str]:
    settings = _load_json(Path.home() / ".claude" / "settings.json")
    default = ""
    values: list[str] = []
    if isinstance(settings, dict):
        configured_default = settings.get("model")
        if isinstance(configured_default, str):
            default = configured_default.strip()
            if default:
                values.append(default)
        env = settings.get("env")
        if isinstance(env, dict):
            for key in _CLAUDE_MODEL_ENV_KEYS:
                value = env.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
    for key in _CLAUDE_MODEL_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            values.append(value.strip())

    values = _dedupe(values)
    if values:
        return values, default or values[0], "config"
    return [], "", "none"


def _cline_models() -> tuple[list[str], str, str]:
    settings = _load_json(Path.home() / ".cline" / "data" / "settings" / "providers.json")
    if not isinstance(settings, dict):
        return [], "", "none"

    providers = settings.get("providers")
    if not isinstance(providers, dict):
        return [], "", "none"

    last_used = settings.get("lastUsedProvider")
    order = list(providers)
    if isinstance(last_used, str) and last_used in providers:
        order.remove(last_used)
        order.insert(0, last_used)

    values: list[str] = []
    default = ""
    for provider_id in order:
        provider = providers.get(provider_id)
        if not isinstance(provider_id, str) or not isinstance(provider, dict):
            continue
        provider_settings = provider.get("settings")
        if not isinstance(provider_settings, dict):
            continue
        model = provider_settings.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        model = model.strip()
        values.append(model)
        if provider_id == last_used and not default:
            default = model

    values = _dedupe(values)
    if values:
        return values, default or values[0], "config"
    return [], "", "none"


def _build_catalog(runtime: str) -> dict[str, Any]:
    if runtime == "opencode":
        values, default, source = _opencode_models()
    elif runtime == "kilo":
        values, default, source = _kilo_models()
    elif runtime == "claude":
        values, default, source = _claude_models()
    elif runtime == "cline":
        values, default, source = _cline_models()
    else:
        values, default, source = [], "", "none"

    return {
        "runtime": runtime,
        "models": [{"value": value, "label": value} for value in values],
        "default": default,
        "source": source,
    }


def list_models(runtime: str) -> dict[str, Any]:
    """Return selectable models for ``runtime``.

    The result shape is ``{"runtime", "models": [{"value", "label"}],
    "default", "source"}``.  Unknown runtimes yield an empty catalog.
    """
    normalized = (runtime or "").strip().lower()
    if normalized not in RUNTIMES:
        return {"runtime": normalized, "models": [], "default": "", "source": "none"}

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(normalized)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    catalog = _build_catalog(normalized)
    with _cache_lock:
        _cache[normalized] = (now, catalog)
    return catalog
