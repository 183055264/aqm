"""Guards against degenerate repeated output from small language models.

Some models (notably lightweight proxy models used through Claude Code) can
enter a short repetition loop while emitting reasoning tokens, e.g. printing
``Today's date is 2026-11-06.`` hundreds of times.  The tokens are useless and
they flood the live output panel, so this module detects a repeating tail and
suppresses the rest of the stream.
"""

from __future__ import annotations

MIN_UNIT = 8
MAX_UNIT = 200
MIN_REPEATS = 3
_TAIL_LIMIT = 1200

_TRIP_NOTE = "\n\n…（检测到思考内容重复循环，已折叠后续思考）\n"


def _repeated_tail_info(text: str, tail_limit: int = _TAIL_LIMIT) -> tuple[int, int]:
    """Return ``(unit_length, repeat_count)`` for a repeated tail, else (0, 0).

    Only the end of ``text`` is inspected, and the unit must repeat at least
    ``MIN_REPEATS`` times consecutively.  The shortest matching unit wins so a
    sentence repeated verbatim is reported at sentence granularity.
    """
    tail = text[-tail_limit:]
    size = len(tail)
    max_unit = min(MAX_UNIT, size // MIN_REPEATS)
    for unit_len in range(MIN_UNIT, max_unit + 1):
        unit = tail[size - unit_len:]
        count = 1
        pos = size - unit_len
        while pos - unit_len >= 0 and tail[pos - unit_len:pos] == unit:
            count += 1
            pos -= unit_len
        if count >= MIN_REPEATS:
            return unit_len, count
    return 0, 0


def collapse_repeats(text: str, tail_limit: int = 8000) -> str:
    """Collapse a repeated tail into a single copy plus a short note."""
    if not text:
        return text
    unit_len, count = _repeated_tail_info(text, tail_limit=tail_limit)
    if not unit_len or count < MIN_REPEATS:
        return text
    unit = text[-unit_len:]
    start = len(text) - unit_len * count
    prefix = text[:start].rstrip()
    note = f"…（原文重复 {count} 次，已折叠）"
    return f"{prefix}\n\n{unit}\n\n{note}".strip()


class ThinkingGuard:
    """Stream filter that stops forwarding once a repetition loop is detected."""

    def __init__(self, check_every: int = 320) -> None:
        self._buffer = ""
        self._pending = 0
        self._tripped = False
        self._check_every = max(32, check_every)

    @property
    def tripped(self) -> bool:
        return self._tripped

    def feed(self, chunk: str) -> str:
        """Return text that should be forwarded (possibly empty)."""
        if not chunk:
            return ""
        self._buffer = (self._buffer + chunk)[-_TAIL_LIMIT:]
        if self._tripped:
            return ""
        self._pending += len(chunk)
        if self._pending < self._check_every:
            return chunk
        self._pending = 0
        unit_len, count = _repeated_tail_info(self._buffer)
        if unit_len and count >= MIN_REPEATS:
            self._tripped = True
            return _TRIP_NOTE
        return chunk
