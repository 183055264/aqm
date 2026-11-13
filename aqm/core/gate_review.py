"""Describe what a pending human gate is actually approving.

A human gate used to render only a free-text "reason" box: the reviewer had to
hunt through the timeline for the member's output and dig through the artifact
list to work out what was being signed off.  This module derives a concrete
checklist from ``agent.gate.prompt`` when the pipeline configures one, or from
the stage output itself (checkboxes / section headings / conclusion line),
and adds the work-block and handoff context needed to decide.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aqm.core.agent import AgentDefinition
from aqm.core.chunks import ChunkManager, ChunkStatus
from aqm.core.project import get_tasks_dir
from aqm.core.task import Task

# Keep the panel readable: a gate is a decision, not another review document.
MAX_ITEMS = 12
MAX_ITEM_CHARS = 240

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$", re.M)
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、])\s+")
_CHECKBOX_MARK_RE = re.compile(r"^\[[ xX]\]\s*")
_CONCLUSION_WORDS = ("审核结论", "结论", "判定", "总结", "结果", "审核意见", "评审意见")
# Prose agents often write "复核结论：返工。" instead of a markdown heading.
_INLINE_CONCLUSION_RE = re.compile(r"^[^\n]{0,24}?结论\s*[:：]\s*(.+)$", re.M)

_STATUS_ORDER = {
    ChunkStatus.blocked: 0,
    ChunkStatus.in_progress: 1,
    ChunkStatus.pending: 2,
    ChunkStatus.done: 3,
}


def _clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _extract_conclusion(output: str) -> str:
    """Return the member's own verdict line, if it stated one."""
    text = output or ""
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING_RE.match(line)
        title = heading.group(2) if heading else ""
        if title and any(word in title for word in _CONCLUSION_WORDS):
            for nxt in lines[idx + 1 :]:
                nxt = nxt.strip()
                if not nxt:
                    continue
                if _HEADING_RE.match(nxt):
                    break
                nxt = _CHECKBOX_MARK_RE.sub("", _LIST_MARK_RE.sub("", nxt))
                if nxt:
                    return f"成员自述{title}：{_clip(nxt, 160)}"
            break
    match = _INLINE_CONCLUSION_RE.search(text)
    if match:
        verdict = _clip(match.group(1), 160)
        # Keep the verdict sentence only: "返工。 本轮独立复核…" -> "返工。"
        verdict = verdict.split("。")[0].strip() or verdict
        return f"成员自述结论：{verdict}"
    return ""


def extract_review_items(prompt: str, output: str) -> tuple[list[dict[str, str]], str]:
    """Build review items from the gate prompt, else from the stage output.

    Returns ``(items, source)`` where ``source`` is ``gate_prompt`` when the
    pipeline supplied explicit criteria, ``stage_output`` when they were
    derived from what the member wrote, or ``none`` when nothing usable was
    found.
    """
    text = (prompt or "").strip()
    if text:
        items: list[dict[str, str]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = _CHECKBOX_MARK_RE.sub("", _LIST_MARK_RE.sub("", line))
            if not line:
                continue
            items.append({"text": _clip(line), "state": "check"})
            if len(items) >= MAX_ITEMS:
                break
        if items:
            return items, "gate_prompt"

    items = []
    for match in _CHECKBOX_RE.finditer(output or ""):
        items.append(
            {
                "text": _clip(match.group(2)),
                "state": "done" if match.group(1).lower() == "x" else "todo",
            }
        )
        if len(items) >= MAX_ITEMS:
            break

    if not items:
        for match in _HEADING_RE.finditer(output or ""):
            if match.group(1) == "#":
                continue
            items.append(
                {"text": f"核对「{_clip(match.group(2), 80)}」部分", "state": "section"}
            )
            if len(items) >= MAX_ITEMS:
                break

    if not items:
        # Prose reports (no markdown structure): surface their bullet list.
        for raw in (output or "").splitlines():
            line = raw.strip()
            if not line or not _LIST_MARK_RE.match(line):
                continue
            text_item = _CHECKBOX_MARK_RE.sub("", _LIST_MARK_RE.sub("", line))
            if not text_item:
                continue
            items.append({"text": _clip(text_item), "state": "note"})
            if len(items) >= MAX_ITEMS:
                break

    if not items:
        # Last resort: the first few sentences of the report, so the reviewer
        # at least sees what the member focused on.
        first_para = re.split(r"\n\s*\n", (output or "").strip())[0]
        for sentence in re.split(r"(?<=[。！？!?])", first_para):
            sentence = sentence.strip()
            if len(sentence) < 8:
                continue
            items.append({"text": _clip(sentence), "state": "note"})
            if len(items) >= 3:
                break

    conclusion = _extract_conclusion(output)
    if conclusion:
        items.insert(0, {"text": conclusion, "state": "conclusion"})
        items = items[:MAX_ITEMS]

    return items, ("stage_output" if items else "none")


def _handoff_targets(
    agent: AgentDefinition | None, condition: str, include_always: bool = False
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    if agent is None:
        return targets
    allowed = {condition, "always"} if include_always else {condition}
    for handoff in agent.handoffs:
        if handoff.condition not in allowed:
            continue
        for target in str(handoff.to or "").split(","):
            target = target.strip()
            if not target:
                continue
            targets.append({"id": target, "task": handoff.task or ""})
    return targets


def _pending_chunks(task: Task, project_dir: str | Path | None) -> dict[str, Any]:
    chunks: list[Any] = []
    if project_dir:
        try:
            manager = ChunkManager(get_tasks_dir(Path(project_dir)) / task.id)
            chunks = list(manager.load().chunks)
        except Exception:  # pragma: no cover - defensive
            chunks = []
    done = sum(1 for c in chunks if c.status == ChunkStatus.done)
    pending = [c for c in chunks if c.status != ChunkStatus.done]
    pending.sort(key=lambda c: (_STATUS_ORDER.get(c.status, 9), c.id))
    return {
        "total": len(chunks),
        "done": done,
        "pending_count": len(pending),
        "pending": [
            {
                "id": c.id,
                "description": _clip(c.description, 160),
                "status": c.status.value,
                "owner": c.owner or "",
            }
            for c in pending[:8]
        ],
    }


def build_gate_review(
    task: Task,
    agents: dict[str, AgentDefinition],
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a structured description of the pending human gate."""
    stage = task.stages[-1] if task.stages else None
    agent = agents.get(stage.agent_id) if stage is not None else None
    gate = getattr(agent, "gate", None) if agent is not None else None
    prompt = (gate.prompt if gate else "") or ""
    output = (stage.output_text if stage is not None else "") or ""
    items, source = extract_review_items(prompt, output)
    chunk_info = _pending_chunks(task, project_dir)

    def describe(targets: list[dict[str, str]]) -> list[dict[str, str]]:
        described = []
        for target in targets:
            member = agents.get(target["id"])
            described.append(
                {
                    "id": target["id"],
                    "name": (member.name or member.id) if member else target["id"],
                    "task": target["task"],
                }
            )
        return described

    return {
        "stage_number": stage.stage_number if stage is not None else None,
        "agent_id": stage.agent_id if stage is not None else "",
        "agent_name": (agent.name or agent.id) if agent is not None else "",
        "task_name": stage.task_name if stage is not None else "",
        "gate_prompt": prompt.strip(),
        "items": items,
        "items_source": source,
        "output_excerpt": _clip(output, 1200),
        "output_chars": len(output),
        "output_preview": output[:600],
        "chunks": chunk_info,
        "approve_to": describe(_handoff_targets(agent, "on_approve", include_always=True)),
        "reject_to": describe(_handoff_targets(agent, "on_reject")),
    }
