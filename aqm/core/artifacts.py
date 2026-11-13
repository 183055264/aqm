"""Collect files created or modified while a task was running.

Agents write deliverables (docs, code, reports) through their CLI tools, but
the pipeline itself never records those paths.  The file system is the source
of truth: anything whose mtime falls inside the task's run window is reported
as a task artifact.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aqm.core.task import Task

# Directories that never contain task deliverables.  Pruning them keeps the
# walk fast even for large projects.
IGNORED_DIRS = {
    ".aqm",
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    "site-packages",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    ".next",
    ".nuxt",
    ".gradle",
    "coverage",
    "htmlcov",
}

DEFAULT_LIMIT = 200
DEFAULT_SCAN_LIMIT = 60000
# Filesystem timestamps are coarse and the file may be written a fraction of a
# second before the pipeline records the run start.
MTIME_TOLERANCE_SECONDS = 3.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def task_run_started_at(task: Task) -> datetime:
    """Return the moment the current run of *task* started."""
    raw = (task.metadata or {}).get("run_started_at")
    if isinstance(raw, str) and raw.strip():
        try:
            return _as_utc(datetime.fromisoformat(raw))
        except ValueError:
            pass
    stage_starts = [
        _as_utc(stage.started_at)
        for stage in task.stages
        if getattr(stage, "started_at", None)
    ]
    if stage_starts:
        return min(stage_starts)
    return _as_utc(task.created_at)


# ── Artifact descriptions ────────────────────────────────────────────────
# A short, deterministic "what is this file for?" line built from the file
# itself (title / docstring / schema), not from an LLM call.

_TEXT_HEAD_BYTES = 16384
_DESCRIPTION_CHARS = 120
# Structured data needs the whole file to summarise its keys; cap it anyway so
# a huge dump cannot stall the task page.
_STRUCTURED_READ_LIMITS = {
    ".json": 4_000_000,
    ".jsonl": 4_000_000,
    ".yaml": 1_000_000,
    ".yml": 1_000_000,
    ".csv": 4_000_000,
    ".tsv": 4_000_000,
}

_DOC_LABELS = {
    ".pdf": "PDF 文档",
    ".doc": "Word 文档",
    ".docx": "Word 文档",
    ".xls": "Excel 工作表",
    ".xlsx": "Excel 工作簿",
    ".ppt": "PPT 演示文稿",
    ".pptx": "PPT 演示文稿",
    ".png": "图片",
    ".jpg": "图片",
    ".jpeg": "图片",
    ".gif": "图片",
    ".svg": "矢量图",
    ".zip": "压缩包",
    ".7z": "压缩包",
    ".db": "SQLite 数据库",
    ".sqlite": "SQLite 数据库",
}

_CODE_LABELS = {
    ".py": "Python 脚本",
    ".js": "JavaScript 脚本",
    ".mjs": "JavaScript 模块",
    ".cjs": "JavaScript 模块",
    ".ts": "TypeScript 源码",
    ".tsx": "TypeScript 组件",
    ".jsx": "React 组件",
    ".sh": "Shell 脚本",
    ".bash": "Shell 脚本",
    ".ps1": "PowerShell 脚本",
    ".bat": "批处理脚本",
    ".cmd": "批处理脚本",
    ".java": "Java 源码",
    ".go": "Go 源码",
    ".rs": "Rust 源码",
    ".c": "C 源码",
    ".h": "C 头文件",
    ".cpp": "C++ 源码",
    ".cs": "C# 源码",
    ".rb": "Ruby 脚本",
    ".php": "PHP 脚本",
    ".sql": "SQL 脚本",
    ".css": "样式表",
    ".scss": "样式表",
}

_DATA_LABELS = {
    ".json": "JSON 数据",
    ".jsonl": "JSON Lines 数据",
    ".yaml": "YAML 配置",
    ".yml": "YAML 配置",
    ".toml": "TOML 配置",
    ".ini": "配置文件",
    ".cfg": "配置文件",
    ".env": "环境配置",
}

_MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
_PLAIN_TEXT_EXTS = {".txt", ".log", ".rst", ".text"}


def _read_text_head(path: Path, limit: int = _TEXT_HEAD_BYTES) -> str | None:
    """Read the first chunk of a text file; ``None`` for binary files."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    if not raw:
        return ""
    if b"\x00" in raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        alt = raw.decode("gbk", errors="replace")
        if alt.count("\ufffd") < text.count("\ufffd"):
            text = alt
    return text


def _clip(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= _DESCRIPTION_CHARS:
        return value
    cut = value[:_DESCRIPTION_CHARS]
    # Prefer ending on a sentence boundary instead of mid-word.
    for sep in ("。", "；", "！", "？", ". ", "; "):
        index = cut.rfind(sep)
        if index >= int(_DESCRIPTION_CHARS * 0.5):
            return cut[: index + len(sep)].rstrip()
    return cut.rstrip() + "…"


def _strip_inline_markup(value: str) -> str:
    """Turn inline Markdown into plain text for one-line descriptions."""
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"__([^_]+)__", r"\1", value)
    value = re.sub(r"\*([^*]+)\*", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value


def _markdown_summary(text: str) -> tuple[str, str]:
    heading = ""
    paragraph: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith("#"):
            if not heading:
                heading = line.lstrip("#").strip()
                continue
            if paragraph:
                break
            continue
        if line.startswith(("```", "~~~")):
            break
        if paragraph:
            paragraph.append(line)
            if sum(len(part) for part in paragraph) >= 80:
                break
            continue
        # Skip structural lines (bullets, tables, quotes) until prose starts.
        if line.startswith(("-", "*", "+", "|", ">", "1.", "2.", "3.")):
            continue
        paragraph.append(line)
    return heading, " ".join(paragraph)


def _first_docstring_line(text: str) -> str:
    match = re.search(
        r'^\s*[rubfRUBF]*("""|\'\'\')(.+?)\1',
        text,
        re.DOTALL | re.MULTILINE,
    )
    if match:
        for line in match.group(2).splitlines():
            if line.strip():
                return line.strip()
    return ""


def _first_comment_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//", "--", ";", "<!--")):
            comment = stripped.lstrip("#/-—;<! ").strip()
            if comment and not comment.startswith(("!", "-*-", "coding")):
                return comment
        elif stripped and not stripped.startswith(("@", "import ", "from ")):
            break
    return ""


def _describe_text_file(path: Path, text: str) -> str:
    ext = path.suffix.lower()
    if ext in _MARKDOWN_EXTS:
        heading, paragraph = _markdown_summary(text)
        heading = _strip_inline_markup(heading)
        paragraph = _strip_inline_markup(paragraph)
        parts = [part for part in (heading, paragraph) if part]
        detail = " — ".join(parts)
        return _clip(f"Markdown 文档 · {detail}" if detail else "Markdown 文档")
    if ext in _CODE_LABELS:
        detail = _first_docstring_line(text) or _first_comment_line(text)
        if ext == ".sh" and not detail:
            detail = "命令行脚本"
        return _clip(f"{_CODE_LABELS[ext]} · {detail}" if detail else _CODE_LABELS[ext])
    if ext == ".json":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return "JSON 数据 · 解析失败或内容过长"
        if isinstance(data, list):
            return f"JSON 数据 · 数组，{len(data)} 项"
        if isinstance(data, dict):
            keys = list(data.keys())
            shown = "、".join(str(k) for k in keys[:6])
            more = "…" if len(keys) > 6 else ""
            return _clip(f"JSON 数据 · 字段 {shown}{more}" if keys else "JSON 数据 · 空对象")
        return "JSON 数据"
    if ext in (".yaml", ".yml"):
        try:
            import yaml as _yaml

            data = _yaml.safe_load(text)
        except Exception:
            return "YAML 配置 · 解析失败或内容过长"
        if isinstance(data, dict) and data:
            keys = "、".join(str(k) for k in list(data.keys())[:6])
            more = "…" if len(data) > 6 else ""
            return _clip(f"YAML 配置 · 键 {keys}{more}")
        return "YAML 配置"
    if ext in (".csv", ".tsv"):
        delimiter = "\t" if ext == ".tsv" else ","
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return "CSV 表格 · 空文件"
        columns = len(lines[0].split(delimiter))
        return f"CSV 表格 · {columns} 列" + (
            f"，{len(lines) - 1}+ 行" if len(lines) > 1 else ""
        )
    if ext in (".html", ".htm"):
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        title = _clip(match.group(1)) if match else ""
        return _clip(f"HTML 页面 · {title}" if title else "HTML 页面")
    if ext in _PLAIN_TEXT_EXTS:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return _clip(f"文本文件 · {first}" if first else "文本文件")
    if ext in _DATA_LABELS:
        return _DATA_LABELS[ext]
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return _clip(f"文件 · {first}" if first else "文件")


def describe_artifact(path: Path, size: int) -> str:
    """Return a one-line description of what a file is and what it contains."""
    ext = path.suffix.lower()
    if ext in _DOC_LABELS:
        return _DOC_LABELS[ext]
    if size == 0:
        return "空文件"
    limit = _STRUCTURED_READ_LIMITS.get(ext)
    if limit is None:
        limit = _TEXT_HEAD_BYTES
    elif size > limit:
        label = (
            _DATA_LABELS.get(ext)
            or _CODE_LABELS.get(ext)
            or _DOC_LABELS.get(ext)
            or "文件"
        )
        return f"{label} · 文件较大（{format_size(size)}）"
    text = _read_text_head(path, limit)
    if text is None:
        label = _DOC_LABELS.get(ext) or _CODE_LABELS.get(ext) or "二进制文件"
        return label
    if text == "":
        return _DOC_LABELS.get(ext) or _DATA_LABELS.get(ext) or "空文件"
    return _describe_text_file(path, text)


def collect_task_files(
    project_root: Path | str,
    task: Task,
    *,
    limit: int = DEFAULT_LIMIT,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Return files under *project_root* modified during the task's run window.

    Each entry looks like ``{"path": "docs/report.md", "size": 1234,
    "mtime": "2026-11-07T05:52:40.496688+00:00"}`` where ``path`` is relative
    to the project root and uses forward slashes.
    """
    if not project_root:
        return []
    root = Path(project_root)
    try:
        root = root.resolve()
    except OSError:
        return []
    if not root.is_dir():
        return []

    since = task_run_started_at(task).timestamp() - MTIME_TOLERANCE_SECONDS
    files: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    def _ignore(_error: OSError) -> None:
        return None

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, onerror=_ignore, followlinks=False
    ):
        dirnames[:] = [
            name for name in dirnames if name.lower() not in IGNORED_DIRS
        ]
        for name in filenames:
            scanned += 1
            if scanned > scan_limit:
                truncated = True
                break
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            if not full.is_file():
                continue
            if stat.st_mtime < since:
                continue
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                rel = full.as_posix()
            files.append({
                "path": rel,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            })
        if truncated:
            break

    files.sort(key=lambda item: (item["mtime"], item["path"]), reverse=True)
    files = files[:limit]
    # Only describe the files we are actually going to show.
    for item in files:
        item["description"] = describe_artifact(
            root / item["path"], item["size"]
        )
    return files


def format_size(size: int) -> str:
    """Human readable file size (e.g. ``12.4 KB``)."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
