"""Shared JSON event stream parser for opencode/kilo CLI runtimes.

Both `opencode run --format json` and `kilo run --format json` emit one JSON
event per line on stdout. Events are grouped by step and carry a `type`
discriminator plus a `part` payload. This module turns those raw events into
the aqm callback protocol (on_output / on_thinking / on_tool) so the Web UI's
Live Output panel shows real-time progress instead of going silent until the
agent finishes.

Observed event types (as of opencode 1.18.x / kilocode 7.8.x):
  step_start    {"type":"step_start", "part":{"type":"step-start"}}
  text          {"type":"text",       "part":{"type":"text", "text":"..."}}
  thinking      {"type":"thinking",   "part":{"type":"thinking", "thinking":"..."}}
  tool_use      {"type":"tool_use",   "part":{"type":"tool", "tool":"write", "state":{...}}}
  step_finish   {"type":"step_finish","part":{"reason":"stop"|"tool-calls", "tokens":{...}}}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Callback signatures (duck-typed; matches aqm.runtime.base protocol)
OutputCallback = Callable[[str], None]
ThinkingCallback = Callable[[str], None]
ToolCallback = Callable[[str, dict], None]


def _short(text: str, max_len: int = 200) -> str:
    """Truncate long strings for log/summary use."""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... (+{len(text) - max_len} chars)"


class JsonStreamParser:
    """Line-oriented parser for opencode/kilo JSON event streams.

    Usage::

        parser = JsonStreamParser(on_output=cb, on_thinking=cb, on_tool=cb)
        for line in iter(proc.stdout.readline, ""):
            parser.feed_line(line)
        final_text = parser.collected_text
    """

    def __init__(
        self,
        on_output: OutputCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
        on_tool: ToolCallback | None = None,
    ) -> None:
        self._on_output = on_output
        self._on_thinking = on_thinking
        self._on_tool = on_tool
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._step_count = 0
        self._tool_count = 0

    @property
    def collected_text(self) -> str:
        """Concatenated assistant text (the agent's final answer)."""
        return "".join(self._text_parts).strip()

    @property
    def collected_thinking(self) -> str:
        """Concatenated reasoning/thinking blocks, if any."""
        return "".join(self._thinking_parts)

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def tool_count(self) -> int:
        return self._tool_count

    def feed_line(self, line: str) -> None:
        """Process a single line of JSON output. Malformed lines are ignored."""
        line = line.rstrip("\r\n")
        if not line:
            return

        # Non-JSON preamble (some CLI versions print a banner line) — skip
        if not line.startswith("{"):
            return

        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return

        if not isinstance(event, dict):
            return

        etype = event.get("type")
        part = event.get("part") or {}
        if not isinstance(part, dict):
            part = {}

        try:
            if etype == "text" and isinstance(part, dict):
                self._handle_text(part)
            elif etype == "thinking" and isinstance(part, dict):
                self._handle_thinking(part)
            elif etype == "tool_use" and isinstance(part, dict):
                self._handle_tool_use(part)
            elif etype == "step_start":
                self._step_count += 1
            elif etype == "step_finish":
                # No callback; token metrics could be surfaced but the UI
                # doesn't render them. Kept here so future UIs can hook in.
                pass
            # Unknown types are silently ignored for forward compatibility.
        except Exception:  # noqa: BLE001 - never let a bad event kill the stream
            logger.debug("JsonStreamParser: failed to dispatch event type=%s", etype)

    def _handle_text(self, part: dict[str, Any]) -> None:
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        # Strip trailing whitespace-only fragments to avoid blank lines piling up
        # in the Web UI's Live Output panel (kilo frequently emits "\n\n" text parts).
        if text.strip() == "":
            return
        self._text_parts.append(text)
        if self._on_output is None:
            return
        try:
            self._on_output(text)
        except Exception:  # noqa: BLE001
            logger.debug("on_output callback raised", exc_info=True)

    def _handle_thinking(self, part: dict[str, Any]) -> None:
        # Some CLI versions use "thinking", others "text" for the content field.
        body = part.get("thinking") or part.get("text") or ""
        if not isinstance(body, str) or not body:
            return
        self._thinking_parts.append(body)
        if self._on_thinking is None:
            return
        try:
            self._on_thinking(body)
        except Exception:  # noqa: BLE001
            logger.debug("on_thinking callback raised", exc_info=True)

    def _handle_tool_use(self, part: dict[str, Any]) -> None:
        tool_name = part.get("tool") or "unknown"
        state = part.get("state") or {}
        if not isinstance(state, dict):
            state = {}

        payload: dict[str, Any] = {
            "tool": tool_name,
            "status": state.get("status", "unknown"),
            "title": state.get("title", ""),
            "input": state.get("input", {}),
            "output": _short(str(state.get("output", "") or "")),
            "callID": part.get("callID", ""),
        }

        self._tool_count += 1

        if self._on_tool is None:
            return
        try:
            # "call" is the event_type; aqm's web/api/tasks.py forwards it as
            # `tool_call` over SSE so the frontend can hook it in.
            self._on_tool("call", payload)
        except Exception:  # noqa: BLE001
            logger.debug("on_tool callback raised", exc_info=True)
