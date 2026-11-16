# -*- coding: utf-8 -*-
"""A task owned by another project shows a self-refreshing snapshot card.

Its live events are published by that project's own aqm process, so this
console cannot subscribe to them.  The card therefore polls the task record,
keeps the status badge fresh and reloads when the snapshot moves on.
"""

from __future__ import annotations

from aqm.core.agent import AgentDefinition
from aqm.core.task import StageRecord, Task, TaskStatus
from aqm.web.pages.task_detail import render_task_detail

AGENTS = {
    "engineer": AgentDefinition(id="engineer", name="实现工程师", runtime="claude")
}


def _render(other_dir, current_dir, status=TaskStatus.in_progress, stages=()):
    task = Task(
        description="跨项目运行中的任务",
        status=status,
        current_agent_id="engineer",
        metadata={"project_dir": str(other_dir)},
    )
    for number in stages:
        task.add_stage(
            StageRecord(
                stage_number=number,
                agent_id="engineer",
                task_name="execute",
                input_text="in",
                output_text=f"第 {number} 阶段输出",
            )
        )
    return render_task_detail(
        task,
        AGENTS,
        "",
        task_project_dir=str(other_dir),
        current_project_dir=str(current_dir),
    )


def test_cross_project_running_task_renders_snapshot_card(tmp_path):
    html = _render(tmp_path / "other", tmp_path / "current")

    assert "任务在另一个项目中运行" in html
    # The poller only rewrites the badge and the timestamp; anything else is
    # picked up with a full reload.
    assert 'id="remoteStatusBadge"' in html
    assert 'id="remoteUpdated"' in html
    # No SSE stream for a task owned by another process.
    assert "new EventSource" not in html
    assert "function pollRemoteTask" in html
    assert "}, 5000);" in html
    assert "出现新阶段或任务状态变化时本页自动刷新" in html


def test_cross_project_poller_tracks_stage_count_and_agent(tmp_path):
    html = _render(tmp_path / "other", tmp_path / "current", stages=(1, 2))

    # Baseline for the "did the snapshot move on?" check.
    assert "const knownStages = 2;" in html
    assert 'const knownAgent = "engineer";' in html
    # A paused task must not trigger a reload loop: the DB keeps in_progress.
    assert "const st = paused ? 'paused' : String(data.status || '');" in html
    assert "RELOAD_STATES.includes(st) || stages.length !== knownStages" in html


def test_cross_project_card_shows_latest_persisted_stage(tmp_path):
    html = _render(tmp_path / "other", tmp_path / "current", stages=(1, 2))

    assert "已落盘 2 个阶段" in html
    assert "第 2 阶段输出" in html


def test_current_project_task_keeps_live_stream(tmp_path):
    html = _render(tmp_path, tmp_path)

    assert "任务在另一个项目中运行" not in html
    # Defined on every task page, but guarded by LIVE_RENDERED.
    assert "function pollRemoteTask" in html
    assert "new EventSource" in html
