# -*- coding: utf-8 -*-
"""Human interjections are rendered as notes in the task live stream."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from aqm.core.agent import AgentDefinition
from aqm.core.pipeline import is_pure_nudge
from aqm.core.task import StageRecord, Task, TaskStatus
from aqm.web.pages.task_detail import render_task_detail

HISTORY_RE = re.compile(r"const INTERJECT_HISTORY = (\[.*?\]);", re.S)


def _render(tmp_path, metadata):
    task = Task(
        description="interjection view",
        status=TaskStatus.in_progress,
        metadata={"project_dir": str(tmp_path), **metadata},
    )
    agents = {
        "engineer": AgentDefinition(
            id="engineer", name="实现与回测工程师", runtime="claude"
        )
    }
    return render_task_detail(
        task, agents, "", task_project_dir=str(tmp_path),
        current_project_dir=str(tmp_path),
    )


def _history(html):
    match = HISTORY_RE.search(html)
    assert match, "INTERJECT_HISTORY not rendered"
    return json.loads(match.group(1))


def test_is_pure_nudge():
    assert is_pure_nudge("继续啊")
    assert is_pure_nudge("go on")
    assert not is_pure_nudge("全部使用中文回复")


def test_history_merges_applied_and_pending_notes(tmp_path):
    html = _render(
        tmp_path,
        {
            "interjections": ["全部使用中文回复"],
            "interjection_log": [
                {
                    "text": "全部使用中文回复",
                    "agent_id": "engineer",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            "pending_interjections": ["补充 100 期回测样本"],
        },
    )

    history = _history(html)
    assert history == [
        {
            "text": "全部使用中文回复",
            "state": "applied",
            "agent_id": "engineer",
            "at": history[0]["at"],
        },
        {
            "text": "补充 100 期回测样本",
            "state": "pending",
            "agent_id": "",
            "at": "",
        },
    ]
    assert ".live-note-pending" in html
    assert ".live-note-applied" in html
    assert "window.addInterjection" in html


def test_history_skips_duplicates_and_pure_nudges(tmp_path):
    html = _render(
        tmp_path,
        {
            "interjections": ["全部使用中文回复", "继续啊"],
            "interjection_log": [
                {"text": "全部使用中文回复", "agent_id": "engineer", "at": ""}
            ],
        },
    )

    history = _history(html)
    assert [item["text"] for item in history] == ["全部使用中文回复"]
    assert history[0]["state"] == "applied"


def _render_with_stages(tmp_path, metadata, stages):
    task = Task(
        description="interjection timeline",
        status=TaskStatus.in_progress,
        metadata={"project_dir": str(tmp_path), **metadata},
    )
    for stage in stages:
        task.add_stage(
            StageRecord(
                stage_number=stage,
                agent_id="engineer",
                task_name="execute",
                input_text="in",
                output_text="out",
            )
        )
    agents = {
        "engineer": AgentDefinition(
            id="engineer", name="实现与回测工程师", runtime="claude"
        )
    }
    return render_task_detail(
        task, agents, "", task_project_dir=str(tmp_path),
        current_project_dir=str(tmp_path),
    )


def _timeline(html):
    """Slice the server-rendered timeline, excluding the inline JS templates."""
    start = html.index('id="liveTimeline"')
    end = html.index("<script", start)
    return html[start:end]


def test_interjection_rendered_in_timeline_under_its_stage(tmp_path):
    html = _render_with_stages(
        tmp_path,
        {
            "interjection_log": [
                {
                    "text": "全部使用中文回复",
                    "agent_id": "engineer",
                    "stage_number": 1,
                    "at": "2026-11-07T10:00:00+00:00",
                }
            ],
        },
        [1],
    )

    timeline = _timeline(html)
    assert timeline.count('class="timeline-note"') == 1
    assert timeline.count("人类插话") >= 1
    assert "全部使用中文回复" in timeline
    # The note is nested inside the stage block, not a detached duplicate.
    stage_start = timeline.index("阶段 1")
    note_pos = timeline.index("全部使用中文回复")
    assert note_pos > stage_start


def test_interjection_without_known_stage_gets_own_timeline_entry(tmp_path):
    html = _render_with_stages(
        tmp_path,
        {
            "interjection_log": [
                {
                    "text": "补充 100 期回测样本",
                    "agent_id": "engineer",
                    "stage_number": 99,
                    "at": "2026-11-07T10:00:00+00:00",
                }
            ],
        },
        [1],
    )

    timeline = _timeline(html)
    assert timeline.count('class="timeline-note"') == 1
    assert "补充 100 期回测样本" in timeline
    assert timeline.index("补充 100 期回测样本") > timeline.index("阶段 1")


def test_pure_nudge_never_rendered_in_timeline(tmp_path):
    html = _render_with_stages(
        tmp_path,
        {
            "interjection_log": [
                {
                    "text": "继续啊",
                    "agent_id": "engineer",
                    "stage_number": 1,
                    "at": "2026-11-07T10:00:00+00:00",
                }
            ],
        },
        [1],
    )

    timeline = _timeline(html)
    assert 'class="timeline-note"' not in timeline
    assert "继续啊" not in timeline


def test_live_note_leaves_stream_after_member_replies(tmp_path):
    html = _render(tmp_path, {})

    # Note lifecycle: sent -> applied -> settled into the timeline.
    assert "function settleInterjections" in html
    assert "window.settleInterjections = settleInterjections" in html
    assert "settleInterjections(d.agent_id)" in html
    assert "function addTimelineNote" in html
    assert "已发送 · 等待成员响应" in html
    assert "本条随后归入时间线" in html
