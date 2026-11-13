"""Read non-secret Cline CLI settings needed to select its configured provider."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def cline_provider_settings_path() -> Path:
    return Path.home() / ".cline" / "data" / "settings" / "providers.json"


def resolve_cline_provider_id() -> str:
    path = cline_provider_settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Unable to read Cline provider settings from %s", path)
        return ""

    if not isinstance(settings, dict):
        logger.warning("Cline provider settings have an unexpected format: %s", path)
        return ""

    provider_id = settings.get("lastUsedProvider")
    providers = settings.get("providers")
    if not isinstance(provider_id, str) or not provider_id.strip():
        return ""
    if not isinstance(providers, dict) or provider_id not in providers:
        logger.warning("Cline's selected provider is not present in %s", path)
        return ""

    return provider_id
