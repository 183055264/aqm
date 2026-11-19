"""Rolling context compaction for meeting transcripts.

A meeting keeps its whole discussion in one ``meeting.md``.  Handing a member
"everything since my last turn" stops scaling after a few rounds: the slice is
tens of thousands of characters, and keeping only its tail silently drops the
beginning — including human instructions written earlier in the round.

This module folds everything older than ``keep_recent_chars`` into a rolling
summary, the same idea as an agent compacting its own context.  Human messages
are never summarised away: they are extracted before folding and injected
verbatim on every turn.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_COMPACT_SETTINGS: dict = {
    "enabled": True,
    # "auto" uses the model when a summariser is supplied and plain extraction
    # otherwise; "extract" never calls a model; "llm" always does.
    "mode": "auto",
    # Compact only when the transcript is longer than this.
    "trigger_chars": 24000,
    # Always keep this much of the transcript verbatim at the tail.
    "keep_recent_chars": 8000,
    # Upper bound for the rolling summary.
    "max_summary_chars": 4000,
    # Summarise in slices of this size so one call stays inside the window.
    "chunk_chars": 8000,
    # Hard cap on how much is folded per compaction (a few model calls at most).
    "max_fold_chars": 48000,
    # Budget for verbatim human instructions kept on top of the summary.
    "max_human_chars": 12000,
    # Empty runtime/model = reuse the meeting host's runtime.
    "runtime": "",
    "model": "",
    "timeout": 180,
}

_ENTRY_RE = re.compile(r"^### .+$", re.M)
_HUMAN_HEAD_RE = re.compile(r"^### (.+?) \(human\) - (.+)$")


def resolve_compact_settings(raw: dict | None) -> dict:
    """Merge a project's ``meeting.compact`` block with the defaults."""
    settings = dict(DEFAULT_COMPACT_SETTINGS)
    for key, value in (raw or {}).items():
        if key in settings and value is not None:
            settings[key] = value
    for key, floor in (
        ("trigger_chars", 4000),
        ("keep_recent_chars", 1000),
        ("max_summary_chars", 500),
        ("chunk_chars", 4000),
        ("max_fold_chars", 4000),
        ("max_human_chars", 0),
        ("timeout", 10),
    ):
        try:
            settings[key] = max(floor, int(settings.get(key, floor)))
        except (TypeError, ValueError):
            settings[key] = floor
    settings["enabled"] = bool(settings.get("enabled", True))
    mode = str(settings.get("mode") or "auto").strip().lower()
    settings["mode"] = mode if mode in ("auto", "extract", "llm") else "auto"
    settings["runtime"] = str(settings.get("runtime") or "")
    settings["model"] = str(settings.get("model") or "")
    # Compacting a slice smaller than what we keep verbatim is pointless.
    if settings["keep_recent_chars"] >= settings["trigger_chars"]:
        settings["trigger_chars"] = settings["keep_recent_chars"] * 2
    return settings


@dataclass
class ContextResult:
    """What a member should be shown, plus what to persist back."""

    feed: str = ""
    summary: str = ""
    summary_upto: int = 0
    pinned_human: list[str] = field(default_factory=list)
    compacted: bool = False
    summarized_chars: int = 0
    dropped_chars: int = 0
    error: str = ""


def extract_human_messages(text: str) -> list[str]:
    """Return ``（第N轮）全文`` for every human entry inside *text*.

    The body ends at the next ``###`` heading of *any* kind, otherwise a
    member's speech that follows the human turn would be pinned as if the
    human had said it.
    """
    out: list[str] = []
    entries = list(_ENTRY_RE.finditer(text or ""))
    for idx, entry in enumerate(entries):
        head = _HUMAN_HEAD_RE.match(entry.group(0))
        if not head:
            continue
        when = head.group(2).strip()
        body_start = entry.end()
        body_end = entries[idx + 1].start() if idx + 1 < len(entries) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            out.append(f"（{when}）{body}")
    return out


def merge_pinned_human(
    existing: list[str] | None,
    incoming: list[str] | None,
    max_chars: int,
) -> list[str]:
    """Merge human instructions, dropping the oldest when the budget runs out."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(incoming or []):
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    if max_chars <= 0:
        return merged
    kept: list[str] = []
    total = 0
    for text in reversed(merged):
        if kept and total + len(text) > max_chars:
            break
        total += len(text)
        kept.append(text)
    kept.reverse()
    return kept


def build_compact_prompt(existing: str, chunk: str, max_chars: int) -> str:
    return (
        "你是会议记录压缩器。把「新增讨论」合并进「已有摘要」，"
        "输出更新后的摘要。\n\n"
        "要求：\n"
        "1. 保留所有已确认的结论、决定、数字、约束和验收标准。\n"
        "2. 保留每位成员的核心立场与分歧点，并注明是谁提出的。\n"
        "3. 人类（human）的指令必须逐字保留。\n"
        "4. 删掉寒暄、重复表述、工具调用过程与代码片段细节。\n"
        f"5. 使用简体中文，Markdown 小标题 + 项目符号，总长不超过 {max_chars} 字。\n"
        "6. 只输出摘要正文，不要解释你在做什么。\n\n"
        f"【已有摘要】\n{existing or '（无，这是第一次压缩）'}\n\n"
        f"【新增讨论】\n{chunk}\n"
    )


def _tail(text: str, keep: int) -> str:
    if len(text) <= keep:
        return text
    return "...[前文省略]...\n" + text[-keep:]


def _compose(summary: str, pinned: list[str], recent: str, note: str = "") -> str:
    parts: list[str] = []
    if note:
        parts.append(f"（{note}）")
    if summary.strip():
        parts.append(
            "【前情摘要】（早前讨论已压缩；人类指令另列在下一节）\n"
            + summary.strip()
        )
    if pinned:
        parts.append(
            "【人类指令 · 全文保留，必须遵守】\n"
            + "\n".join(f"- {item}" for item in pinned)
        )
    if recent.strip():
        parts.append("【最新讨论原文】\n" + recent.strip())
    return "\n\n".join(parts)


def _cap(text: str, limit: int) -> tuple[str, int]:
    """Keep at most *limit* trailing characters; return (text, dropped)."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    return text[-limit:], len(text) - limit


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


# Lines that usually carry a decision, a constraint or an acceptance criterion.
_DECISION_RE = re.compile(
    r"决定|结论|裁决|约束|必须|禁止|不允许|默认|最终|验收|优先|风险|口径|冻结|确认|改用|改为"
)
_MEMBER_HEAD_RE = re.compile(r"^### (.+?) \(([A-Za-z0-9_./-]+)\) - (.+)$")


def extract_summary(text: str, max_chars: int = 4000) -> str:
    """Digest *text* without calling a model.

    Each speech keeps its heading plus the lines most likely to matter (its
    opening claim and up to two decision/constraint lines).  Deterministic,
    instant, and unable to invent anything — which matters when the host model
    needs minutes to answer a single chunk.
    """
    text = text or ""
    entries = list(_ENTRY_RE.finditer(text))
    if not entries:
        return _tail(text, max_chars).strip()
    per_entry = max(80, max_chars // max(1, len(entries)))
    blocks: list[str] = []
    owner = ""
    for index, match in enumerate(entries):
        end = entries[index + 1].start() if index + 1 < len(entries) else len(text)
        heading = match.group(0).strip().lstrip("#").strip()
        member = _MEMBER_HEAD_RE.match(match.group(0))
        if member:
            owner = f"{member.group(1)} - {member.group(3)}"
            label = heading
        else:
            # A subsection carries no speaker; keep the one it belongs to.
            label = f"{heading}（{owner}）" if owner else heading
        lines = [
            line.strip()
            for line in text[match.end() : end].splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "|", "```"))
        ]
        picks: list[str] = []
        first = next((line for line in lines if len(line) > 12), "")
        if first:
            picks.append(first)
        for line in lines:
            if len(picks) >= 3:
                break
            if line in picks or len(line) < 8:
                continue
            if _DECISION_RE.search(line):
                picks.append(line)
        if not picks:
            continue
        detail = " / ".join(_clip(line, 90) for line in picks)
        blocks.append(_clip(f"- **{label}**：{detail}", per_entry))
    digest = "\n".join(blocks).strip()
    return digest or _tail(text, max_chars).strip()


def _merge_digest(existing: str, digest: str, limit: int) -> str:
    """Append *digest* to *existing*, dropping the oldest lines to fit."""
    parts = [
        part.strip()
        for part in ((existing or "").strip(), (digest or "").strip())
        if part.strip()
    ]
    combined = "\n".join(parts)
    if limit <= 0 or len(combined) <= limit:
        return combined
    lines = combined.splitlines()
    while len(lines) > 1 and len("\n".join(lines)) > limit:
        lines.pop(0)
    return "\n".join(lines)[-limit:]


def _fold(
    summary: str,
    text: str,
    settings: dict,
    invoke: Callable[[str], str] | None,
) -> tuple[str, int]:
    """Fold *text* into *summary*.

    Returns ``(summary, folded_chars)``.  *folded_chars* is how much of *text*
    the new summary covers, so a partial fold can advance the anchor without
    pretending the rest was summarised.
    """
    max_chars = int(settings.get("max_summary_chars") or 4000)
    mode = str(settings.get("mode") or "auto").lower()
    if mode == "extract" or (mode == "auto" and invoke is None):
        digest = extract_summary(text, max_chars)
        if not digest:
            return "", 0
        return _merge_digest(summary, digest, max_chars), len(text)
    if invoke is None:
        return "", 0
    chunk_size = int(settings.get("chunk_chars") or 8000)
    current = (summary or "").strip()
    for start in range(0, len(text), chunk_size):
        chunk = text[start : start + chunk_size]
        if not chunk.strip():
            continue
        try:
            answer = str(invoke(build_compact_prompt(current, chunk, max_chars)) or "")
        except Exception as exc:  # noqa: BLE001 - compaction must never block a meeting
            logger.warning("[MeetingCompaction] summariser failed: %s", exc)
            return "", 0
        answer = answer.strip()
        if not answer:
            return "", 0
        current = answer[:max_chars]
    return current, len(text)


def build_member_context(
    transcript: str,
    *,
    offset: int | None,
    summary: str = "",
    summary_upto: int = 0,
    pinned_human: list[str] | None = None,
    settings: dict | None = None,
    invoke: Callable[[str], str] | None = None,
) -> ContextResult:
    """Build the context block for one member turn.

    ``offset`` is the character position this member last read up to; content
    before it is not repeated while it is still recent.  ``summary_upto`` is
    the position already covered by the meeting-wide rolling ``summary``.
    """
    settings = settings or resolve_compact_settings(None)
    transcript = transcript or ""
    total = len(transcript)
    keep = int(settings.get("keep_recent_chars") or 8000)
    trigger = int(settings.get("trigger_chars") or 24000)
    max_human = int(settings.get("max_human_chars") or 0)
    pinned = list(pinned_human or [])

    result = ContextResult(
        summary=(summary or "").strip(),
        summary_upto=min(max(0, int(summary_upto or 0)), total),
        pinned_human=pinned,
    )
    if not transcript:
        return result

    mode = str(settings.get("mode") or "auto").lower()
    if not (settings.get("enabled", True) and (invoke is not None or mode == "extract")):
        # Compaction off: keep the historical per-member slice, tail-truncated.
        start = 0 if offset is None else min(max(0, int(offset)), total)
        result.summary = ""
        result.summary_upto = 0
        result.pinned_human = []
        result.feed = _tail(transcript[start:], keep)
        return result

    if not result.summary:
        result.summary_upto = 0
    start = 0
    if offset is not None:
        try:
            start = min(max(0, int(offset)), total)
        except (TypeError, ValueError):
            start = 0
    # Everything the member already read is skipped while it is still recent;
    # once the meeting is long, the shared summary stands in for the old part.
    base = max(result.summary_upto, start)

    # Small meeting (or little since the last compaction): no model call.
    if not result.summary and total <= trigger:
        result.feed = transcript[start:]
        return result
    if total - base <= trigger:
        result.feed = _compose(result.summary, pinned, transcript[base:])
        return result

    target = total - keep
    to_fold = transcript[result.summary_upto:target]
    # Pin every human message before any trimming so instructions survive
    # even when the folded slice itself is capped.
    pinned = merge_pinned_human(pinned, extract_human_messages(to_fold), max_human)
    fold_limit = int(settings.get("max_fold_chars") or 48000)
    if mode == "extract":
        # Extraction costs no tokens, so digest the whole slice instead of
        # throwing away its earliest part.
        fold_limit = max(fold_limit, len(to_fold))
    fold_text, dropped = _cap(to_fold, fold_limit)
    folded, folded_chars = _fold(result.summary, fold_text, settings, invoke)

    if not folded or folded_chars <= 0:
        result.error = "压缩失败，回退为尾部截断"
        result.pinned_human = pinned
        result.feed = _compose(
            "",
            pinned,
            _tail(transcript[base:], keep),
            note=f"摘要生成失败，已省略 {len(to_fold)} 字符",
        )
        return result

    note = ""
    if dropped:
        note = f"更早的 {dropped} 字符未纳入摘要"
    result.summary = folded
    # A partial fold covers `dropped + folded_chars` of the slice; the rest is
    # still handed over verbatim instead of being silently skipped.
    result.summary_upto = min(result.summary_upto + dropped + folded_chars, target)
    result.pinned_human = pinned
    result.compacted = True
    result.summarized_chars = folded_chars
    result.dropped_chars = dropped
    result.feed = _compose(
        folded,
        pinned,
        transcript[max(result.summary_upto, base):],
        note=note,
    )
    return result


# ---------------------------------------------------------------------------
# Model invocation
# ---------------------------------------------------------------------------


def _build_runtime(runtime_name: str, project_root: Path, timeout: int):
    name = (runtime_name or "claude").lower()
    root = Path(project_root)
    if name == "claude":
        from aqm.runtime.claude import ClaudeCodeRuntime

        return ClaudeCodeRuntime(root, timeout=timeout)
    if name == "gemini":
        from aqm.runtime.gemini import GeminiRuntime

        return GeminiRuntime(root, timeout=timeout)
    if name == "codex":
        from aqm.runtime.codex import CodexRuntime

        return CodexRuntime(root, timeout=timeout)
    if name == "opencode":
        from aqm.runtime.opencode import OpencodeRuntime

        return OpencodeRuntime(root, timeout=timeout)
    if name == "kilo":
        from aqm.runtime.kilo import KiloRuntime

        return KiloRuntime(root, timeout=timeout)
    if name == "cline":
        from aqm.runtime.cline import ClineRuntime

        return ClineRuntime(root, timeout=timeout)
    raise ValueError(f"Unsupported compaction runtime: {runtime_name}")


def make_invoker(
    project_root: Path,
    runtime_name: str = "",
    model: str = "",
    timeout: int = 120,
    should_stop: Callable[[], bool] | None = None,
) -> Callable[[str], str]:
    """Return a ``prompt -> answer`` callable using a one-shot CLI run."""

    cached: dict = {}

    def invoke(prompt: str) -> str:
        if should_stop is not None and should_stop():
            raise RuntimeError("meeting stopped during compaction")
        from aqm.core.agent import AgentDefinition
        from aqm.core.task import Task

        runtime = cached.get("runtime")
        if runtime is None:
            runtime = _build_runtime(runtime_name, project_root, timeout)
            cached["runtime"] = runtime
        agent = AgentDefinition(
            id="_meeting_compactor",
            name="会议压缩器",
            runtime=(runtime_name or "claude").lower(),
            model=model or None,
            system_prompt="",
        )
        task = Task(description="compact meeting transcript")
        return str(runtime.run(prompt, agent, task) or "")

    return invoke
