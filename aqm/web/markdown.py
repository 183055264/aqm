"""Dependency-free Markdown subset renderer for the web UI.

Used by the task overview to display descriptions (which may contain the full
requirements document produced by a meeting) as formatted HTML instead of a
single blob of text.

Supported: headings, ordered/unordered lists (with one or more nesting levels),
fenced code blocks, tables, blockquotes, horizontal rules, and inline code /
bold / italic / strikethrough / links. Everything is HTML-escaped before tags
are emitted, so untrusted task text cannot inject markup.
"""

from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+.-]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE_RE = re.compile(r"`([^`\n]+)`")


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    if "|" not in s or "-" not in s:
        return False
    return bool(_TABLE_SEP_RE.fullmatch(s))


def _inline(text: str) -> str:
    """Apply inline formatting to already HTML-escaped text."""
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return f"\x00C{len(codes) - 1}\x00"

    text = _CODE_RE.sub(_stash, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    text = _LINK_RE.sub(
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    for idx, code in enumerate(codes):
        text = text.replace(f"\x00C{idx}\x00", f"<code>{code}</code>")
    return text


def _split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _render_table(block: list[str]) -> str:
    header = _split_row(block[0])
    body = [_split_row(line) for line in block[2:]]
    parts = ["<table><thead><tr>"]
    parts.extend(f"<th>{_inline(cell)}</th>" for cell in header)
    parts.append("</tr></thead><tbody>")
    for row in body:
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        parts.append("<tr>")
        parts.extend(f"<td>{_inline(cell)}</td>" for cell in row[: len(header)])
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_list(items: list[tuple[int, bool, str]], start: int = 0, base: int | None = None) -> str:
    """Render list items as (indent_level, ordered, text) tuples."""
    if start >= len(items):
        return ""
    if base is None:
        base = items[start][0]
    parts: list[str] = []
    i = start
    while i < len(items) and items[i][0] >= base:
        level, ordered, _ = items[i]
        tag = "ol" if ordered else "ul"
        entries: list[str] = []
        while i < len(items) and items[i][0] == level and items[i][1] == ordered:
            text = items[i][2]
            i += 1
            sub_start = i
            while i < len(items) and items[i][0] > level:
                i += 1
            nested = ""
            if i > sub_start:
                nested = _render_list(items, sub_start, items[sub_start][0])
            entries.append(f"<li>{_inline(text)}{nested}</li>")
        parts.append(f"<{tag}>" + "".join(entries) + f"</{tag}>")
    return "".join(parts)


def _starts_block(lines: list[str], idx: int) -> bool:
    line = lines[idx]
    if _FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line):
        return True
    if _LIST_RE.match(line) or line.strip().startswith("&gt;"):
        return True
    if "|" in line and idx + 1 < len(lines) and _is_table_sep(lines[idx + 1]):
        return True
    return False


def render_markdown(text: str) -> str:
    """Render a Markdown subset to safe HTML. Returns "" for empty input."""
    if not text or not text.strip():
        return ""

    src = html.escape(text.replace("\r\n", "\n").replace("\r", "\n"), quote=False)
    lines = src.split("\n")
    out: list[str] = []
    i, total = 0, len(lines)

    while i < total:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            lang = fence.group(2)
            body: list[str] = []
            i += 1
            while i < total and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            cls = f' class="language-{lang}"' if lang else ""
            out.append(
                '<pre class="md-code"><code'
                f"{cls}>" + "\n".join(body) + "</code></pre>"
            )
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if "|" in line and i + 1 < total and _is_table_sep(lines[i + 1]):
            block: list[str] = []
            while i < total and lines[i].strip() and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            if len(block) >= 2:
                out.append(_render_table(block))
            continue

        if stripped.startswith("&gt;"):
            quoted: list[str] = []
            while i < total and lines[i].strip().startswith("&gt;"):
                quoted.append(re.sub(r"^\s*&gt;\s?", "", lines[i]))
                i += 1
            out.append(
                "<blockquote>"
                + "<br>".join(_inline(q) for q in quoted)
                + "</blockquote>"
            )
            continue

        if _LIST_RE.match(line):
            items: list[tuple[int, bool, str]] = []
            while i < total:
                match = _LIST_RE.match(lines[i])
                if not match:
                    if not lines[i].strip() and i + 1 < total and _LIST_RE.match(lines[i + 1]):
                        i += 1
                        continue
                    break
                indent = len(match.group(1).expandtabs(4))
                ordered = match.group(2)[0].isdigit()
                items.append((indent // 2, ordered, match.group(3)))
                i += 1
            out.append(_render_list(items))
            continue

        para: list[str] = []
        while i < total and lines[i].strip() and not _starts_block(lines, i):
            para.append(lines[i].strip())
            i += 1
        if not para:
            para.append(stripped)
            i += 1
        out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")

    return "".join(out)
