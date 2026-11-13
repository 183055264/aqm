"""Automatic work-block (chunk) decomposition from a requirements document.

The planner asks a model to turn a requirements document into a list of
independently verifiable work units, each owned by one of the pipeline
members.  It is deliberately tolerant: malformed JSON, a Markdown table, or a
plain numbered list are all accepted, and any failure simply leaves the task
without chunks instead of blocking the run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from aqm.core.chunks import content_hash

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、])\s+(.+)$")
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*$", re.MULTILINE)


@dataclass
class PlannedChunk:
    """A work unit proposed by the planner, before it is persisted."""

    title: str
    detail: str = ""
    owner: str = ""
    phase: str = ""
    acceptance: str = ""

    def model_dump(self) -> dict[str, str]:
        return {
            "title": self.title,
            "description": self.title,
            "detail": self.detail,
            "owner": self.owner,
            "phase": self.phase,
            "acceptance": self.acceptance,
        }


# ---------------------------------------------------------------------------
# Requirements discovery
# ---------------------------------------------------------------------------

def find_requirements(
    task: Any, project_root: Path
) -> tuple[str, str, str]:
    """Return ``(text, source_label, source_hash)`` for the requirements doc.

    Priority:
      1. ``task.metadata["requirements_path"]`` (meeting hand-off);
      2. ``<project>/REQUIREMENTS.md``;
      3. ``<project>/docs/REQUIREMENTS.md``;
      4. the task description itself.
    """
    metadata = getattr(task, "metadata", None) or {}
    candidates: list[Path] = []
    raw_path = metadata.get("requirements_path")
    if raw_path:
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidates.append(candidate)
    candidates.extend([
        project_root / "REQUIREMENTS.md",
        project_root / "docs" / "REQUIREMENTS.md",
    ])

    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.strip()
        if text:
            return text, str(candidate), content_hash(text)

    description = str(getattr(task, "description", "") or "").strip()
    return description, "task.description", content_hash(description)


def truncate_document(text: str, max_chars: int = 24000) -> str:
    """Keep the document structure while fitting it into *max_chars*.

    Headings are always preserved; each section gets a fair share of the
    remaining budget, and acceptance-criteria sections are kept in full.
    """
    if len(text) <= max_chars:
        return text

    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current_heading or current_body:
                sections.append((current_heading, "\n".join(current_body)))
            current_heading = line.strip()
            current_body = []
        else:
            current_body.append(line)
    if current_heading or current_body:
        sections.append((current_heading, "\n".join(current_body)))

    if not sections:
        return text[:max_chars] + "\n\n…（文档过长，已截断）"

    heading_cost = sum(len(h) + 1 for h, _ in sections)
    full_sections = {
        idx for idx, (heading, _) in enumerate(sections)
        if any(key in heading.lower() for key in ("验收", "acceptance", "交付"))
    }
    budget = max(0, max_chars - heading_cost - 200)
    per_section = max(300, budget // max(1, len(sections)))
    parts: list[str] = []
    for idx, (heading, body) in enumerate(sections):
        body = body.strip()
        limit = max(len(body), per_section) if idx in full_sections else per_section
        if len(body) > limit:
            body = body[:limit].rstrip() + "\n…（本节已截断）"
        parts.append(f"{heading}\n{body}".strip())
    result = "\n\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + "\n\n…（文档过长，已截断）"
    return result


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _agent_role_line(agent: Any) -> str:
    name = getattr(agent, "name", "") or getattr(agent, "id", "")
    runtime = getattr(agent, "runtime", "") or ""
    prompt = (getattr(agent, "system_prompt", "") or "").strip().replace("\n", " ")
    if len(prompt) > 220:
        prompt = prompt[:220].rstrip() + "…"
    handoffs = getattr(agent, "handoffs", None) or []
    targets = ",".join(
        str(getattr(h, "to", "") or "") for h in handoffs
    ).strip(",")
    suffix = f"；交接给：{targets}" if targets else ""
    return f"- {agent.id}（{name}，runtime={runtime}）：{prompt}{suffix}"


def build_split_prompt(
    requirements: str,
    agents: dict[str, Any],
    max_chunks: int = 30,
    language: str = "",
) -> str:
    roles = "\n".join(_agent_role_line(a) for a in agents.values())
    language_line = (
        f"全部文字使用{language}。" if language else "全部文字使用简体中文。"
    )
    return f"""你是资深项目计划员。请把下面的需求文档拆成可独立验收的工作块（work chunks）。

## 流水线成员
{roles}

## 拆分规则
1. 拆成 5 到 {max_chunks} 个工作块，覆盖需求的全部交付物；不要漏掉验收标准。
2. 每个工作块必须是单一职责、可独立验收的动作，避免「完成整个模块」这类大块。
3. 按实际执行顺序排列；前后依赖的块要拆开写清楚。
4. owner 必须是上面成员列表中的 id；无法判断时留空字符串。
5. phase 用 2~4 个中文字概括阶段，例如：需求、设计、实现、测试、评审、文档、交付。
6. 不要重复；不要把同一件事拆成多个近义块。
7. {language_line}

## 输出格式
只输出一个 JSON 对象，不要输出解释、不要使用 Markdown 围栏：
{{"chunks": [
  {{"title": "工作块标题", "detail": "要做什么、边界与依赖", "owner": "成员id", "phase": "实现", "acceptance": "如何验收"}}
]}}

## 需求文档
{requirements}
"""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _balanced_json(text: str) -> str | None:
    """Return the first balanced JSON object or array in *text*."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _normalise_items(items: Iterable[Any], valid_owners: set[str]) -> list[PlannedChunk]:
    result: list[PlannedChunk] = []
    for item in items:
        if isinstance(item, str):
            title = item.strip()
            detail = ""
            owner = ""
            phase = ""
            acceptance = ""
        elif isinstance(item, dict):
            title = str(
                item.get("title")
                or item.get("description")
                or item.get("name")
                or item.get("task")
                or ""
            ).strip()
            detail = str(item.get("detail") or item.get("notes") or "").strip()
            owner = str(item.get("owner") or item.get("agent") or "").strip()
            phase = str(item.get("phase") or item.get("stage") or "").strip()
            acceptance = str(
                item.get("acceptance") or item.get("acceptance_criteria") or ""
            ).strip()
        else:
            continue
        if not title:
            continue
        if owner and valid_owners and owner not in valid_owners:
            owner = ""
        title = title[:200]
        result.append(PlannedChunk(
            title=title,
            detail=detail[:2000],
            owner=owner,
            phase=phase[:32],
            acceptance=acceptance[:1000],
        ))
    return result


def _parse_table(text: str, valid_owners: set[str]) -> list[PlannedChunk]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return []

    header = [c.lower() for c in rows[0]]
    index = {
        "title": next(
            (i for i, h in enumerate(header)
             if any(k in h for k in ("title", "description", "任务", "工作块", "描述"))),
            -1,
        ),
        "owner": next(
            (i for i, h in enumerate(header) if "owner" in h or "负责" in h), -1
        ),
        "phase": next(
            (i for i, h in enumerate(header) if "phase" in h or "阶段" in h), -1
        ),
        "acceptance": next(
            (i for i, h in enumerate(header) if "accept" in h or "验收" in h), -1
        ),
    }

    items: list[dict[str, str]] = []
    for row in rows[1:]:
        if index["title"] >= 0 and index["title"] < len(row):
            title = row[index["title"]]
        else:
            title = max(row, key=len) if row else ""
        if not title or re.fullmatch(r"C-\d+", title, re.IGNORECASE):
            continue
        item = {
            "title": title,
            "owner": row[index["owner"]] if 0 <= index["owner"] < len(row) else "",
            "phase": row[index["phase"]] if 0 <= index["phase"] < len(row) else "",
            "acceptance": (
                row[index["acceptance"]]
                if 0 <= index["acceptance"] < len(row) else ""
            ),
        }
        items.append(item)
    return _normalise_items(items, valid_owners)


def _parse_list(text: str, valid_owners: set[str]) -> list[PlannedChunk]:
    items: list[dict[str, str]] = []
    for line in text.splitlines():
        if "CHUNK_" in line.upper():
            continue
        match = _LIST_RE.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if not raw or raw.startswith("|"):
            continue
        # Optional trailing metadata: (owner=engineer, phase=实现)
        owner = ""
        phase = ""
        tag_match = re.search(r"[（(]([^）)]*)[）)]\s*$", raw)
        if tag_match:
            for part in re.split(r"[,，;；]", tag_match.group(1)):
                if "=" in part:
                    key, value = part.split("=", 1)
                    if key.strip().lower() in ("owner", "负责"):
                        owner = value.strip()
                    elif key.strip().lower() in ("phase", "阶段"):
                        phase = value.strip()
        items.append({"title": raw, "owner": owner, "phase": phase})
    return _normalise_items(items, valid_owners)


def _title_key(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text.lower())


def _dedupe(chunks: list[PlannedChunk]) -> list[PlannedChunk]:
    result: list[PlannedChunk] = []
    keys: list[str] = []
    for chunk in chunks:
        key = _title_key(chunk.title)
        if not key:
            continue
        if any(
            key == existing
            or SequenceMatcher(None, key, existing).ratio() >= 0.92
            for existing in keys
        ):
            continue
        keys.append(key)
        result.append(chunk)
    return result


def parse_plan(
    text: str,
    max_chunks: int = 30,
    valid_owners: Iterable[str] | None = None,
) -> list[PlannedChunk]:
    """Parse a planner response into a deduplicated, capped chunk list."""
    owners = set(valid_owners or ())
    cleaned = _FENCE_RE.sub("", text or "")

    payload = _balanced_json(cleaned)
    if payload:
        try:
            data = json.loads(payload)
            if isinstance(data, dict):
                data = data.get("chunks") or data.get("work_units") or []
            if isinstance(data, list):
                chunks = _normalise_items(data, owners)
                if chunks:
                    return _dedupe(chunks)[:max_chunks]
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("Chunk planner JSON parse failed", exc_info=True)

    for parser in (_parse_table, _parse_list):
        chunks = parser(cleaned, owners)
        if chunks:
            return _dedupe(chunks)[:max_chunks]
    return []


# ---------------------------------------------------------------------------
# Planner entry point
# ---------------------------------------------------------------------------

def plan_chunks(
    *,
    project_root: Path,
    task: Any,
    requirements: str,
    agents: dict[str, Any],
    runtime: Any,
    runtime_name: str,
    model: str = "",
    max_chunks: int = 30,
    language: str = "",
    timeout: int = 180,
) -> list[PlannedChunk]:
    """Ask the model to split *requirements* and return the planned chunks."""
    from aqm.core.agent import AgentDefinition

    prompt = build_split_prompt(requirements, agents, max_chunks, language)
    splitter_agent = AgentDefinition(
        id="_chunk_planner",
        name="工作块拆分器",
        runtime=runtime_name or "claude",
        model=model or None,
        system_prompt="",
    )

    try:
        response = runtime.run(prompt, splitter_agent, task)
    except Exception as exc:  # noqa: BLE001 - planning must never block the task
        logger.warning("[ChunkPlanner] model call failed: %s", exc)
        return []

    task_dir = _task_dir(project_root, getattr(task, "id", ""))
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "chunks.split.raw.txt").write_text(
            str(response or ""), encoding="utf-8"
        )
    except OSError:
        logger.debug("Could not persist raw planner response", exc_info=True)

    chunks = parse_plan(response or "", max_chunks, agents.keys())
    logger.info("[ChunkPlanner] model returned %d usable chunks", len(chunks))
    return chunks


def _task_dir(project_root: Path, task_id: str) -> Path:
    from aqm.core.project import get_tasks_dir

    return get_tasks_dir(project_root) / task_id
