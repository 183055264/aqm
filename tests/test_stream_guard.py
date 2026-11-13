"""Tests for the degenerate-output guard used by streaming runtimes."""

from __future__ import annotations

from aqm.runtime.stream_guard import ThinkingGuard, collapse_repeats


def test_thinking_guard_stops_repeated_sentence():
    sentence = "Today's date is 2026-11-06. "
    guard = ThinkingGuard(check_every=100)

    forwarded = "".join(guard.feed(sentence) for _ in range(200))

    assert guard.tripped
    assert forwarded.count(sentence) <= 8
    assert "已折叠" in forwarded


def test_thinking_guard_allows_normal_prose():
    guard = ThinkingGuard(check_every=10)
    parts = [
        "先分析需求。",
        "需要锁定统计口径。",
        "然后检查数据来源是否完整。",
        "接下来规划执行步骤。",
        "最后整理风险与待确认事项。",
    ]

    assert "".join(guard.feed(part) for part in parts) == "".join(parts)
    assert not guard.tripped


def test_collapse_repeats_keeps_single_copy():
    sentence = "Today's date is 2026-11-06. "
    block = "开头说明。\n\n" + sentence * 50

    collapsed = collapse_repeats(block)

    assert collapsed.count(sentence) == 1
    assert "原文重复 50 次" in collapsed


def test_thinking_guard_stops_chunked_fragments():
    guard = ThinkingGuard(check_every=50)
    fragment = "2026-11-06 今天日期。"

    out = "".join(guard.feed(fragment) for _ in range(300))

    assert guard.tripped
    assert len(out) < len(fragment) * 50


def test_collapse_repeats_leaves_normal_text_alone():
    text = "第一步：阅读需求。\n第二步：核对口径。\n第三步：输出结论。"

    assert collapse_repeats(text) == text
