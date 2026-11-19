# -*- coding: utf-8 -*-
"""Rolling meeting-context compaction (``aqm.core.meeting_context``)."""

from __future__ import annotations

import pytest

from aqm.core.meeting_context import (
    build_member_context,
    extract_human_messages,
    merge_pinned_human,
    resolve_compact_settings,
)


def _settings(**overrides):
    """Compaction settings scaled down so fixtures stay small."""
    base = {
        "trigger_chars": 4000,
        "keep_recent_chars": 1000,
        "max_summary_chars": 500,
        "chunk_chars": 4000,
        "max_fold_chars": 40000,
        "max_human_chars": 12000,
    }
    base.update(overrides)
    return resolve_compact_settings(base)


class _Summariser:
    def __init__(self, answer="【摘要】讨论要点", fail=False):
        self.prompts: list[str] = []
        self.answer = answer
        self.fail = fail

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("model down")
        return self.answer


def _speech(round_num, *, name="成员", runtime="claude", size=500, marker=""):
    body = (marker + "内容" * (size // 2 + 1))[:size]
    return f"### {name} ({runtime}) - 第{round_num}轮\n\n{body}\n\n"


def _human(round_num, text, *, when=None):
    suffix = when or f"第{round_num}轮"
    return f"### 人类参与者 (human) - {suffix}\n\n{text}\n\n"


def test_small_transcript_is_fed_whole_without_compaction():
    text = _speech(1, size=1800) + _speech(2, size=1800)
    summariser = _Summariser()

    result = build_member_context(
        text, offset=None, settings=_settings(), invoke=summariser
    )

    assert result.feed == text
    assert result.compacted is False
    assert summariser.prompts == []


def test_small_transcript_shows_a_returning_member_only_what_is_new():
    text = _speech(1, size=1800, marker="甲：") + _speech(
        2, size=1800, marker="乙："
    )
    offset = len(_speech(1, size=1800, marker="甲："))

    result = build_member_context(
        text, offset=offset, settings=_settings(), invoke=_Summariser()
    )

    assert result.feed == text[offset:]
    assert "甲：" not in result.feed


def test_long_transcript_is_folded_into_a_rolling_summary():
    text = _speech(1, size=4000, marker="旧结论：") + _speech(
        2, size=4000, marker="中段："
    ) + _speech(3, size=900, marker="最新发言：")
    summariser = _Summariser("【摘要】旧结论与中段的要点")

    result = build_member_context(
        text, offset=None, settings=_settings(), invoke=summariser
    )

    assert result.compacted is True
    assert len(summariser.prompts) >= 1
    assert result.summary == "【摘要】旧结论与中段的要点"
    assert result.summary_upto == len(text) - 1000
    assert "【前情摘要】" in result.feed
    assert "最新发言：" in result.feed
    # The folded part is represented by the summary, not by raw text.
    assert "旧结论：" not in result.feed


def test_human_instruction_is_pinned_verbatim():
    text = (
        _speech(1, name="主持", size=3000)
        + _human(2, "不要有收费项目，可以有赞助", when="第2轮 · 插话")
        + _speech(2, name="产品", size=3000)
        + _speech(3, name="工程", size=1200)
    )
    summariser = _Summariser("【摘要】普通讨论，没有提到人类要求")

    result = build_member_context(
        text, offset=None, settings=_settings(), invoke=summariser
    )

    assert result.compacted is True
    assert result.pinned_human == [
        "（第2轮 · 插话）不要有收费项目，可以有赞助"
    ]
    assert "【人类指令 · 全文保留，必须遵守】" in result.feed
    assert "不要有收费项目，可以有赞助" in result.feed


def test_human_instruction_survives_the_fold_cap():
    text = (
        _human(1, "必须使用简体中文回复")
        + _speech(1, size=10000)
        + _speech(2, size=10000)
        + _speech(3, size=900, marker="最新：")
    )
    summariser = _Summariser("【摘要】只覆盖最后一段")

    result = build_member_context(
        text,
        offset=None,
        settings=_settings(max_fold_chars=4000),
        invoke=summariser,
    )

    assert result.dropped_chars > 0
    assert result.pinned_human == ["（第1轮）必须使用简体中文回复"]
    assert "必须使用简体中文回复" in result.feed
    assert "未纳入摘要" in result.feed


def test_summariser_failure_falls_back_to_tail_truncation():
    text = _speech(1, size=8000) + _speech(2, size=2000)
    summariser = _Summariser(fail=True)

    result = build_member_context(
        text, offset=None, settings=_settings(), invoke=summariser
    )

    assert result.error
    assert result.compacted is False
    assert result.summary == ""
    assert result.summary_upto == 0
    assert "前文省略" in result.feed
    assert result.feed.endswith(text[-1000:].strip())


def test_disabled_compaction_keeps_the_old_per_member_slice():
    text = _speech(1, size=10000, marker="甲：") + _speech(
        2, size=2000, marker="乙："
    )
    offset = len(text) - 2000
    summariser = _Summariser()

    result = build_member_context(
        text,
        offset=offset,
        settings=_settings(enabled=False),
        invoke=summariser,
    )

    assert summariser.prompts == []
    assert result.compacted is False
    assert "甲：" not in result.feed
    assert result.feed == "...[前文省略]...\n" + text[offset:][-1000:]


def test_disabled_compaction_on_first_turn_keeps_the_old_behaviour():
    text = _speech(1, size=12000, marker="开头：")

    result = build_member_context(
        text, offset=None, settings=_settings(enabled=False), invoke=_Summariser()
    )

    assert result.feed.startswith("...[前文省略]...")
    assert result.feed.endswith(text[-1000:])


def test_summary_is_reused_while_the_unread_slice_stays_small():
    text = _speech(1, size=9000)
    summariser = _Summariser()

    result = build_member_context(
        text,
        offset=None,
        summary="【摘要】已有结论",
        summary_upto=len(text) - 500,
        settings=_settings(),
        invoke=summariser,
    )

    # One model call per ~trigger chars, not one per turn.
    assert summariser.prompts == []
    assert result.compacted is False
    assert result.summary == "【摘要】已有结论"
    assert result.feed.startswith("【前情摘要】")
    assert result.feed.endswith(text[-500:].strip())


def test_folding_is_chunked_so_one_call_stays_small():
    text = _speech(1, size=12000)

    summariser = _Summariser()
    result = build_member_context(
        text, offset=None, settings=_settings(), invoke=summariser
    )

    # 11000 chars to fold, chunk_chars=4000 => 3 calls.
    assert len(summariser.prompts) == 3
    assert result.compacted is True


def test_summary_offset_is_clamped_to_the_transcript():
    result = build_member_context(
        "abc",
        offset=None,
        summary="旧摘要",
        summary_upto=999,
        settings=_settings(),
        invoke=_Summariser(),
    )

    assert result.summary_upto == 3
    assert "旧摘要" in result.feed


def test_empty_transcript_has_no_feed():
    result = build_member_context(
        "", offset=None, settings=_settings(), invoke=_Summariser()
    )

    assert result.feed == ""
    assert result.compacted is False


def test_extract_human_messages_stops_at_the_next_speaker():
    text = (
        _human(2, "这条要记住", when="第2轮 · 插话")
        + _speech(2, name="产品", size=200, marker="产品发言：")
    )

    assert extract_human_messages(text) == ["（第2轮 · 插话）这条要记住"]


def test_merge_pinned_human_keeps_the_newest_within_budget():
    merged = merge_pinned_human(["旧指令" * 20], ["新指令"], max_chars=20)

    assert merged == ["新指令"]


def test_merge_pinned_human_deduplicates():
    merged = merge_pinned_human(["同一条"], ["同一条", "另一条"], max_chars=0)

    assert merged == ["同一条", "另一条"]


def test_resolve_settings_enforces_floors_and_trigger_invariant():
    settings = resolve_compact_settings(
        {
            "trigger_chars": 1,
            "keep_recent_chars": 99999,
            "max_summary_chars": 1,
            "chunk_chars": 1,
            "timeout": 1,
        }
    )

    assert settings["trigger_chars"] == settings["keep_recent_chars"] * 2
    assert settings["max_summary_chars"] == 500
    assert settings["chunk_chars"] == 4000
    assert settings["timeout"] == 10


def test_resolve_settings_ignores_unknown_keys():
    settings = resolve_compact_settings({"nope": 1, "enabled": False})

    assert "nope" not in settings
    assert settings["enabled"] is False


def test_resolve_settings_normalises_the_mode():
    assert resolve_compact_settings({})["mode"] == "auto"
    assert resolve_compact_settings({"mode": "EXTRACT"})["mode"] == "extract"
    assert resolve_compact_settings({"mode": "llm"})["mode"] == "llm"
    assert resolve_compact_settings({"mode": "bogus"})["mode"] == "auto"


class _NoModel:
    def __call__(self, prompt: str) -> str:
        raise AssertionError("extract mode must not call a model")


def test_extract_mode_digests_old_speeches_without_a_model():
    text = (
        _speech(1, size=3000, marker="旧结论：采用 6 crate 切分。")
        + _speech(2, size=3000, marker="还要保留 geometryHash。")
        + _speech(3, size=900, marker="最新发言：确认收口。")
    )

    result = build_member_context(
        text,
        offset=None,
        settings=_settings(mode="extract"),
        invoke=_NoModel(),
    )

    assert result.compacted is True
    assert result.summary_upto == len(text) - 1000
    # The digest keeps decisions and speaker labels ...
    assert "采用 6 crate 切分" in result.summary
    assert "成员" in result.summary
    # ... while the verbatim part is only the recent tail.
    recent = result.feed.split("【最新讨论原文】", 1)[1]
    assert len(recent) <= 1100
    assert "【前情摘要】" in result.feed
    assert "最新发言" in result.feed


def test_extract_digest_stays_within_the_summary_budget():
    text = "".join(
        _speech(round_num, size=2500, marker=f"第{round_num}轮结论：必须保留。")
        for round_num in range(1, 7)
    )

    result = build_member_context(
        text,
        offset=None,
        settings=_settings(mode="extract", max_summary_chars=600),
        invoke=None,
    )

    assert result.compacted is True
    assert len(result.summary) <= 600


def test_extract_keeps_a_decision_ledger_when_the_budget_is_tight():
    filler = "补充说明。\n" * 600

    def speech(round_num, intro, decision):
        return f"### 成员 (claude) - 第{round_num}轮\n\n{intro}\n{filler}{decision}\n\n"

    text = (
        speech(1, "本轮先回顾背景与整体思路。", "决定：冻结 6 crate 方案。")
        + speech(2, "继续讨论实现细节与边界。", "未决：放码规则由谁维护？")
        + speech(3, "最新发言：确认收口。", "")
    )

    result = build_member_context(
        text,
        offset=None,
        settings=_settings(mode="extract", max_summary_chars=800),
        invoke=None,
    )

    assert result.compacted is True
    # Decisions and open questions survive even though the per-speaker notes
    # are clipped hard.
    assert "冻结 6 crate 方案" in result.summary
    assert "未决：放码规则由谁维护" in result.summary
    assert len(result.summary) <= 800


def test_extract_dedupes_repeated_decisions_across_rounds():
    def speech(round_num):
        return (
            f"### 成员 (claude) - 第{round_num}轮\n\n"
            "本轮先回顾背景与整体思路。\n" + "补充说明。\n" * 600
            + "决定：默认使用简体中文。\n\n"
        )

    text = speech(1) + speech(2)
    result = build_member_context(
        text,
        offset=None,
        settings=_settings(mode="extract", max_summary_chars=800),
        invoke=None,
    )

    assert result.compacted is True
    assert result.summary.count("默认使用简体中文") == 1


def test_auto_mode_without_a_summariser_keeps_tail_truncation():
    text = _speech(1, size=9000, marker="开头：")

    result = build_member_context(
        text, offset=None, settings=_settings(), invoke=None
    )

    assert result.compacted is False
    assert result.feed.startswith("...[前文省略]...")
    assert result.summary == ""
