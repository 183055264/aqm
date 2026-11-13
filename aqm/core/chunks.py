"""Chunk decomposition — break tasks into trackable work units.

Chunks are small, atomic pieces of work that make up a larger task.  They can
be created in four ways:

* automatically from a requirements document (``aqm.core.chunk_planner``);
* seeded from pipeline YAML (``chunks.initial``);
* by an agent through output directives (``CHUNK_ADD:`` …);
* by a human through the web UI or ``aqm chunks``.

Storage: ``chunks.json`` in the task directory alongside ``context.md`` and
``transcript.md``.  All mutations are serialised with a process-wide lock and
persisted atomically, because the web UI and a running agent may update the
same list concurrently.

Directive protocol (case insensitive)::

    CHUNK_START: C-003
    CHUNK_DONE: C-003 C-004
    CHUNK_BLOCK: C-005 waiting for the API contract
    CHUNK_REMOVE: C-006
    CHUNK_ADD: Build the export page [owner=writer] [phase=delivery]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ChunkStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"


_STATUS_ICON = {
    "pending": "⬜",
    "in_progress": "🔄",
    "done": "✅",
    "blocked": "⛔",
}


# ---------------------------------------------------------------------------
# Concurrency: one re-entrant lock per chunks.json path.
# ---------------------------------------------------------------------------

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_chunk_id(existing_ids: set[str]) -> str:
    """Return the next chunk ID (max numeric suffix + 1).

    Using max+1 instead of filling gaps avoids resurrecting an ID that is still
    referenced by an old transcript or a completed-chunk note.
    """
    largest = 0
    for cid in existing_ids:
        match = re.fullmatch(r"C-(\d+)", cid or "", re.IGNORECASE)
        if match:
            largest = max(largest, int(match.group(1)))
    return f"C-{largest + 1:03d}"


class Chunk(BaseModel):
    """A single work unit within a task."""

    id: str
    description: str
    status: ChunkStatus = ChunkStatus.pending
    owner: str = ""
    phase: str = ""
    note: str = ""
    source: str = "manual"  # manual | config | agent | auto
    created_by: str = ""
    completed_by: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ChunkList(BaseModel):
    """Serializable collection of chunks plus generation metadata."""

    version: int = 1
    generated_from: str = ""
    source_hash: str = ""
    splitter_model: str = ""
    auto_split_at: str = ""
    chunks: list[Chunk] = Field(default_factory=list)


class ChunkManager:
    """Manage ``chunks.json`` for a single task directory."""

    def __init__(self, task_dir: Path) -> None:
        self.task_dir = Path(task_dir)
        self.chunks_path = self.task_dir / "chunks.json"
        self._lock = _lock_for(self.chunks_path)

    # ── low level persistence ────────────────────────────────────────

    def _ensure_dir(self) -> None:
        """Create the task directory (kept for backwards compatibility)."""
        self.task_dir.mkdir(parents=True, exist_ok=True)

    def _read_unlocked(self) -> ChunkList:
        if not self.chunks_path.exists():
            return ChunkList()
        try:
            raw = self.chunks_path.read_text(encoding="utf-8")
            return ChunkList.model_validate_json(raw)
        except Exception as exc:
            logger.warning("Failed to load %s, using empty: %s", self.chunks_path, exc)
            return ChunkList()

    def _write_unlocked(self, chunk_list: ChunkList) -> None:
        self.task_dir.mkdir(parents=True, exist_ok=True)
        payload = chunk_list.model_dump_json(indent=2)
        tmp = self.chunks_path.with_name(self.chunks_path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.chunks_path)

    # ── public API ───────────────────────────────────────────────────

    def load(self) -> ChunkList:
        """Load chunks from disk. Returns an empty list on missing/corrupt data."""
        with self._lock:
            return self._read_unlocked()

    def save(self, chunk_list: ChunkList) -> None:
        """Persist chunks to disk atomically."""
        with self._lock:
            self._write_unlocked(chunk_list)

    def add(
        self,
        description: str,
        created_by: str = "user",
        *,
        owner: str = "",
        phase: str = "",
        source: str = "manual",
        note: str = "",
    ) -> Chunk:
        """Add a new chunk and return it."""
        with self._lock:
            cl = self._read_unlocked()
            chunk = Chunk(
                id=_generate_chunk_id({c.id for c in cl.chunks}),
                description=description.strip(),
                owner=owner,
                phase=phase,
                note=note,
                source=source,
                created_by=created_by,
            )
            cl.chunks.append(chunk)
            self._write_unlocked(cl)
        logger.info(
            "[ChunkManager] Added %s: %s (by %s, owner=%s)",
            chunk.id, chunk.description, created_by, owner or "-",
        )
        return chunk

    def update(self, chunk_id: str, **fields: Any) -> Optional[Chunk]:
        """Update arbitrary chunk fields. Returns the updated chunk or None."""
        allowed = {"description", "status", "owner", "phase", "note",
                   "completed_by", "source"}
        with self._lock:
            cl = self._read_unlocked()
            for chunk in cl.chunks:
                if chunk.id != chunk_id:
                    continue
                for key, value in fields.items():
                    if key not in allowed or value is None:
                        continue
                    if key == "status":
                        value = ChunkStatus(value)
                    setattr(chunk, key, value)
                chunk.updated_at = _now()
                self._write_unlocked(cl)
                logger.info("[ChunkManager] Updated %s: %s", chunk_id, fields)
                return chunk
        return None

    def remove(self, chunk_id: str) -> bool:
        """Remove a chunk by ID. Returns True if found and removed."""
        with self._lock:
            cl = self._read_unlocked()
            before = len(cl.chunks)
            cl.chunks = [c for c in cl.chunks if c.id != chunk_id]
            if len(cl.chunks) < before:
                self._write_unlocked(cl)
                logger.info("[ChunkManager] Removed %s", chunk_id)
                return True
            return False

    def mark_done(self, chunk_id: str, completed_by: str = "") -> bool:
        return self.update(
            chunk_id, status=ChunkStatus.done, completed_by=completed_by,
        ) is not None

    def mark_in_progress(self, chunk_id: str) -> bool:
        return self.update(chunk_id, status=ChunkStatus.in_progress) is not None

    def start(self, chunk_id: str, owner: str = "") -> bool:
        fields: dict[str, Any] = {"status": ChunkStatus.in_progress}
        if owner:
            fields["owner"] = owner
        return self.update(chunk_id, **fields) is not None

    def block(self, chunk_id: str, reason: str = "") -> bool:
        return self.update(
            chunk_id, status=ChunkStatus.blocked, note=reason,
        ) is not None

    def claim_next(self, owner: str) -> Optional[Chunk]:
        """Mark the first pending chunk owned by *owner* as in progress."""
        with self._lock:
            cl = self._read_unlocked()
            for chunk in cl.chunks:
                if chunk.owner == owner and chunk.status == ChunkStatus.pending:
                    chunk.status = ChunkStatus.in_progress
                    chunk.updated_at = _now()
                    self._write_unlocked(cl)
                    logger.info("[ChunkManager] %s claimed %s", owner, chunk.id)
                    return chunk
        return None

    def release_in_progress(self, keep: Iterable[str] = ()) -> list[Chunk]:
        """Return abandoned in-progress chunks to pending.

        Only one stage runs per task, so a block still marked ``in_progress``
        when the next stage starts cannot be actively worked on: the previous
        turn was interrupted, the server restarted, or the member never
        emitted ``CHUNK_DONE``.  Leaving them alone made the progress panel
        show several "in progress" blocks at once.
        """
        keep_set = {str(item) for item in keep}
        released: list[Chunk] = []
        with self._lock:
            cl = self._read_unlocked()
            for chunk in cl.chunks:
                if (
                    chunk.status == ChunkStatus.in_progress
                    and chunk.id not in keep_set
                ):
                    chunk.status = ChunkStatus.pending
                    chunk.updated_at = _now()
                    released.append(chunk.model_copy(deep=True))
            if released:
                self._write_unlocked(cl)
                logger.info(
                    "[ChunkManager] released stale in-progress chunks: %s",
                    ", ".join(c.id for c in released),
                )
        return released

    def reorder(self, ordered_ids: Iterable[str]) -> bool:
        """Reorder chunks according to *ordered_ids*; unknown ids are ignored."""
        ids = [str(i) for i in ordered_ids]
        with self._lock:
            cl = self._read_unlocked()
            by_id = {c.id: c for c in cl.chunks}
            ordered = [by_id[i] for i in ids if i in by_id]
            ordered.extend(c for c in cl.chunks if c.id not in set(ids))
            cl.chunks = ordered
            self._write_unlocked(cl)
            return bool(ids)

    def reset(self, keep_done: bool = False) -> int:
        """Reset all chunks to pending. Returns the number changed."""
        changed = 0
        with self._lock:
            cl = self._read_unlocked()
            for chunk in cl.chunks:
                if keep_done and chunk.status == ChunkStatus.done:
                    continue
                if chunk.status != ChunkStatus.pending:
                    changed += 1
                chunk.status = ChunkStatus.pending
                chunk.completed_by = ""
                chunk.note = ""
                chunk.updated_at = _now()
            self._write_unlocked(cl)
        return changed

    def clear(self, keep_done: bool = False) -> int:
        """Remove all chunks (optionally keeping completed ones)."""
        with self._lock:
            cl = self._read_unlocked()
            before = len(cl.chunks)
            if keep_done:
                cl.chunks = [c for c in cl.chunks if c.status == ChunkStatus.done]
            else:
                cl.chunks = []
            self._write_unlocked(cl)
            return before - len(cl.chunks)

    def merge_auto(self, planned: Iterable[Any], keep_done: bool = True) -> list[Chunk]:
        """Merge an automatically generated plan into the existing list.

        Manual chunks and (optionally) chunks that are already done / in
        progress are preserved.  Previously generated *pending* chunks are
        replaced so a re-split does not duplicate work.
        """
        with self._lock:
            cl = self._read_unlocked()
            kept: list[Chunk] = []
            for chunk in cl.chunks:
                protected = chunk.source != "auto"
                if keep_done and chunk.status in (ChunkStatus.done, ChunkStatus.in_progress):
                    protected = True
                if protected:
                    kept.append(chunk)

            existing_ids = {c.id for c in kept}
            created: list[Chunk] = []
            for item in planned:
                data = item if isinstance(item, dict) else item.model_dump()
                description = str(
                    data.get("description") or data.get("title") or ""
                ).strip()
                if not description:
                    continue
                chunk = Chunk(
                    id=_generate_chunk_id(existing_ids),
                    description=description,
                    owner=str(data.get("owner") or ""),
                    phase=str(data.get("phase") or ""),
                    note=str(data.get("detail") or ""),
                    source="auto",
                    created_by="chunk_planner",
                )
                existing_ids.add(chunk.id)
                created.append(chunk)

            cl.chunks = kept + created
            self._write_unlocked(cl)
        logger.info(
            "[ChunkManager] merge_auto kept=%d created=%d", len(kept), len(created)
        )
        return created

    def set_generation_meta(
        self,
        *,
        generated_from: str = "",
        source_hash: str = "",
        splitter_model: str = "",
    ) -> None:
        with self._lock:
            cl = self._read_unlocked()
            cl.generated_from = generated_from or cl.generated_from
            cl.source_hash = source_hash or cl.source_hash
            cl.splitter_model = splitter_model or cl.splitter_model
            cl.auto_split_at = _now().isoformat()
            self._write_unlocked(cl)

    def log_directives(self, agent_id: str, stage_number: int, text: str) -> None:
        """Keep the raw output that contained chunk directives for auditing."""
        try:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            with (self.task_dir / "chunks.directives.log").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    f"\n===== {_now().isoformat()} agent={agent_id} "
                    f"stage={stage_number} =====\n{text}\n"
                )
        except OSError:
            logger.debug("Could not append chunk directives log", exc_info=True)

    # ── queries ─────────────────────────────────────────────────────

    def all_done(self, owner: Optional[str] = None) -> bool:
        """True when every chunk (optionally only *owner*'s) is done."""
        cl = self.load()
        chunks = self._select(cl.chunks, owner)
        if not chunks:
            return True
        return all(c.status == ChunkStatus.done for c in chunks)

    def counts(self, owner: Optional[str] = None) -> tuple[int, int, int]:
        """Return ``(total, done, remaining)``."""
        cl = self.load()
        chunks = self._select(cl.chunks, owner)
        total = len(chunks)
        done = sum(1 for c in chunks if c.status == ChunkStatus.done)
        return total, done, total - done

    def progress(self, owner: Optional[str] = None) -> dict[str, Any]:
        cl = self.load()
        chunks = self._select(cl.chunks, owner)
        total = len(chunks)
        done = sum(1 for c in chunks if c.status == ChunkStatus.done)
        in_progress = sum(1 for c in chunks if c.status == ChunkStatus.in_progress)
        blocked = sum(1 for c in chunks if c.status == ChunkStatus.blocked)
        pending = sum(1 for c in chunks if c.status == ChunkStatus.pending)
        return {
            "total": total,
            "done": done,
            "pending": pending,
            "in_progress": in_progress,
            "blocked": blocked,
            "remaining": total - done,
            "percent": round(done * 100 / total) if total else 100,
            "by_owner": self._by_owner(cl.chunks),
            "by_phase": self._by_phase(cl.chunks),
            "source": cl.generated_from,
            "splitter_model": cl.splitter_model,
            "auto_split_at": cl.auto_split_at,
        }

    def remaining(self, owner: Optional[str] = None) -> list[Chunk]:
        cl = self.load()
        return [c for c in self._select(cl.chunks, owner) if c.status != ChunkStatus.done]

    def get(self, chunk_id: str) -> Optional[Chunk]:
        cl = self.load()
        for chunk in cl.chunks:
            if chunk.id == chunk_id:
                return chunk
        return None

    def summary(self, owner: Optional[str] = None, compact: bool = True) -> str:
        """Render chunks for prompt injection via ``{{ chunks }}``.

        In compact mode only open chunks are listed; completed chunks are
        represented by the progress line so large lists do not flood the prompt.
        """
        cl = self.load()
        chunks = self._select(cl.chunks, owner)
        if not chunks:
            return "(no chunks defined)"

        rows = chunks
        if compact:
            open_rows = [c for c in chunks if c.status != ChunkStatus.done]
            done_rows = [c for c in chunks if c.status == ChunkStatus.done]
            # Show a few recently completed rows for context without flooding
            # the prompt when a large project is nearly finished.
            done_rows.sort(key=lambda c: c.updated_at, reverse=True)
            rows = open_rows + done_rows[:5]
        lines = ["| ID | Status | Owner | Description |", "|---|---|---|---|"]
        for chunk in rows:
            icon = _STATUS_ICON.get(chunk.status.value, chunk.status.value)
            description = chunk.description.replace("|", "\\|")
            if len(description) > 160:
                description = description[:157] + "..."
            owner_text = chunk.owner or "-"
            lines.append(
                f"| {chunk.id} | {icon} {chunk.status.value} | {owner_text} | {description} |"
            )
        if compact and not rows:
            lines.append("| - | ✅ done | - | 全部工作块已完成 |")
        total, done, remaining = self.counts(owner)
        lines.append(
            f"\n**Progress: {done}/{total} done, {remaining} remaining**"
        )
        return "\n".join(lines)

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _select(chunks: list[Chunk], owner: Optional[str]) -> list[Chunk]:
        if not owner:
            return list(chunks)
        # Own chunks plus unassigned ones (a shared backlog).
        return [c for c in chunks if c.owner in ("", owner)]

    @staticmethod
    def _by_owner(chunks: list[Chunk]) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for chunk in chunks:
            key = chunk.owner or "_unassigned"
            bucket = result.setdefault(key, {"total": 0, "done": 0})
            bucket["total"] += 1
            if chunk.status == ChunkStatus.done:
                bucket["done"] += 1
        return result

    @staticmethod
    def _by_phase(chunks: list[Chunk]) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for chunk in chunks:
            key = chunk.phase or "_none"
            bucket = result.setdefault(key, {"total": 0, "done": 0})
            bucket["total"] += 1
            if chunk.status == ChunkStatus.done:
                bucket["done"] += 1
        return result

    def init_from_config(
        self, initial: list[Any], created_by: str = "config",
        owner: str = "",
    ) -> None:
        """Seed chunks from YAML config. Only runs when no chunks exist yet."""
        with self._lock:
            cl = self._read_unlocked()
            if cl.chunks:
                return
            existing_ids: set[str] = set()
            for item in initial:
                if isinstance(item, dict):
                    description = str(item.get("description") or item.get("title") or "")
                    item_owner = str(item.get("owner") or owner)
                    phase = str(item.get("phase") or "")
                else:
                    description = str(item)
                    item_owner = owner
                    phase = ""
                description = description.strip()
                if not description:
                    continue
                chunk = Chunk(
                    id=_generate_chunk_id(existing_ids),
                    description=description,
                    owner=item_owner,
                    phase=phase,
                    source="config",
                    created_by=created_by,
                )
                existing_ids.add(chunk.id)
                cl.chunks.append(chunk)
            self._write_unlocked(cl)
            logger.info("[ChunkManager] Initialised %d chunks from config", len(cl.chunks))


# ---------------------------------------------------------------------------
# Directive parsing (used by pipeline)
# ---------------------------------------------------------------------------

_CHUNK_ADD_RE = re.compile(r"CHUNK_ADD:\s*(.+)", re.IGNORECASE)
_CHUNK_DONE_RE = re.compile(r"CHUNK_DONE:\s*(.+)", re.IGNORECASE)
_CHUNK_START_RE = re.compile(r"CHUNK_START:\s*(\S+)", re.IGNORECASE)
_CHUNK_BLOCK_RE = re.compile(r"CHUNK_BLOCK:\s*(\S+)\s*(.*)", re.IGNORECASE)
_CHUNK_REMOVE_RE = re.compile(r"CHUNK_REMOVE:\s*(\S+)", re.IGNORECASE)
_CHUNK_ID_RE = re.compile(r"C-\d+", re.IGNORECASE)
_DIRECTIVE_RE = re.compile(
    r"\s*CHUNK_(?:ADD|DONE|START|BLOCK|REMOVE):[^\n]*", re.IGNORECASE
)
_CHUNK_TAG_RE = re.compile(r"\[(owner|phase)\s*=\s*([^\]]+)\]", re.IGNORECASE)


def sanitize_chunk_directives(text: str) -> str:
    """Remove chunk directive expressions from a user-visible message."""
    if not text:
        return text
    cleaned = _DIRECTIVE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def parse_chunk_directives(
    message: str,
    chunk_mgr: ChunkManager,
    agent_id: str,
) -> list[dict]:
    """Parse CHUNK_* directives from agent output.

    Returns a list of action dicts for SSE broadcasting::

        [{"action": "done", "chunk_id": "C-003", "chunk": {...},
          "progress": {...}, "agent_id": "writer"}]
    """
    actions: list[dict] = []

    def push(action: str, chunk: Any = None, **extra: Any) -> None:
        payload = {
            "action": action,
            "agent_id": agent_id,
            **extra,
        }
        if chunk is not None:
            payload["chunk_id"] = chunk.id
            payload["chunk"] = chunk.model_dump(mode="json")
        payload["progress"] = chunk_mgr.progress()
        actions.append(payload)

    for match in _CHUNK_ADD_RE.finditer(message):
        raw = match.group(1).strip()
        tags = {k.lower(): v.strip() for k, v in _CHUNK_TAG_RE.findall(raw)}
        description = _CHUNK_TAG_RE.sub("", raw).strip()
        if not description:
            continue
        chunk = chunk_mgr.add(
            description,
            created_by=agent_id,
            owner=tags.get("owner", agent_id),
            phase=tags.get("phase", ""),
            source="agent",
        )
        push("add", chunk, description=description)

    for match in _CHUNK_START_RE.finditer(message):
        chunk_id = match.group(1).strip().rstrip(".,;:")
        if chunk_mgr.start(chunk_id, owner=agent_id):
            push("start", chunk_mgr.get(chunk_id), chunk_id=chunk_id)
        else:
            logger.warning("[ChunkDirective] CHUNK_START: %s not found", chunk_id)

    for match in _CHUNK_DONE_RE.finditer(message):
        ids = _CHUNK_ID_RE.findall(match.group(1))
        if not ids:
            continue
        for chunk_id in ids:
            chunk_id = chunk_id.upper()
            if chunk_mgr.mark_done(chunk_id, completed_by=agent_id):
                push("done", chunk_mgr.get(chunk_id), chunk_id=chunk_id)
            else:
                logger.warning("[ChunkDirective] CHUNK_DONE: %s not found", chunk_id)

    for match in _CHUNK_BLOCK_RE.finditer(message):
        chunk_id = match.group(1).strip().rstrip(".,;:").upper()
        reason = match.group(2).strip()
        if chunk_mgr.block(chunk_id, reason):
            push("block", chunk_mgr.get(chunk_id), chunk_id=chunk_id, reason=reason)
        else:
            logger.warning("[ChunkDirective] CHUNK_BLOCK: %s not found", chunk_id)

    for match in _CHUNK_REMOVE_RE.finditer(message):
        chunk_id = match.group(1).strip().rstrip(".,;:").upper()
        if chunk_mgr.remove(chunk_id):
            push("remove", None, chunk_id=chunk_id)
        else:
            logger.warning("[ChunkDirective] CHUNK_REMOVE: %s not found", chunk_id)

    return actions


def chunk_instructions() -> str:
    """Fixed protocol block appended to stage prompts when chunks are active."""
    return (
        "\n\n════════ 工作块协议（必须遵守）════════\n"
        "- 清单中的每个工作块都必须独立完成，不要跳过。\n"
        "- 开始做某块时输出：CHUNK_START: C-001\n"
        "- 完成某块时输出：CHUNK_DONE: C-001（可一次写多个，如 CHUNK_DONE: C-001 C-002）\n"
        "- 遇到阻塞无法继续时输出：CHUNK_BLOCK: C-001 原因\n"
        "- 需要补充新块时输出：CHUNK_ADD: 描述 [owner=成员ID] [phase=阶段]\n"
        "- 上述指令行不会展示给用户，请正常输出正文。\n"
        "════════ 工作块协议结束 ════════"
    )


def remaining_instruction(chunks: list[Chunk]) -> str:
    """Instruction appended when a stage must retry because chunks remain."""
    lines = "\n".join(
        f"- {c.id}（{c.status.value}，{c.owner or '未分配'}）：{c.description}"
        for c in chunks
    )
    return (
        "\n\n════════ 本轮工作块未完成 ════════\n"
        "以下工作块尚未完成，请继续把它们做完；每完成一个就输出 "
        "CHUNK_DONE: <编号>。\n"
        f"{lines}\n"
        "完成后在最终答复中说明每一块的结果。\n"
        "════════ 未完成工作块说明结束 ════════"
    )


def content_hash(text: str) -> str:
    """Stable hash used to avoid re-splitting an unchanged requirements doc."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULT_CHUNK_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "auto_split": True,
    "auto_split_required": False,
    "auto_instructions": True,
    "enforce": "retry",          # off | warn | retry | fail
    "retry_limit": 2,
    "owner_filter": True,
    "auto_claim": True,
    "max_chunks": 30,
    "min_description_chars": 800,
    "requirements_max_chars": 24000,
    "splitter": {
        "runtime": "",
        "model": "",
        "timeout": 180,
    },
}


def resolve_chunk_settings(raw: dict | None) -> dict[str, Any]:
    """Merge project/agent chunk settings with the defaults."""
    settings = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in DEFAULT_CHUNK_SETTINGS.items()
    }
    for key, value in (raw or {}).items():
        if key == "splitter" and isinstance(value, dict):
            settings["splitter"].update(value)
        elif key in settings:
            settings[key] = value
    mode = str(settings.get("enforce") or "retry").lower()
    if mode not in {"off", "warn", "retry", "fail"}:
        mode = "retry"
    settings["enforce"] = mode
    try:
        settings["retry_limit"] = max(0, int(settings.get("retry_limit", 2)))
    except (TypeError, ValueError):
        settings["retry_limit"] = 2
    try:
        settings["max_chunks"] = max(1, int(settings.get("max_chunks", 30)))
    except (TypeError, ValueError):
        settings["max_chunks"] = 30
    return settings
