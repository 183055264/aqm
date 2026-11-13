# -*- coding: utf-8 -*-
"""Stage checkpoints and restart-after-stall behaviour."""

from __future__ import annotations

from aqm.core.agent import AgentDefinition, Handoff
from aqm.core.pipeline import Pipeline
from aqm.core.task import StageRecord, Task, TaskStatus
from aqm.queue.file import FileQueue


def _build(tmp_project, monkeypatch):
    agents = {
        "engineer": AgentDefinition(
            id="engineer", name="工程师", runtime="claude",
            system_prompt="Build: {{ input }}",
            handoffs=[Handoff(to="reviewer", task="复核", condition="always")],
        ),
        "reviewer": AgentDefinition(
            id="reviewer", name="复核", runtime="claude",
            system_prompt="Review: {{ input }}",
        ),
    }
    queue = FileQueue(tmp_project / ".aqm" / "auto-resume-queue")
    pipeline = Pipeline(agents, queue, tmp_project)
    prompts: list[str] = []

    def fake_execute(prompt, agent, task, **_kwargs):
        prompts.append(agent.id)
        return f"{agent.id} output"

    monkeypatch.setattr(pipeline, "_execute_agent", fake_execute)
    return pipeline, queue, prompts


def test_stage_start_records_checkpoint(tmp_project, monkeypatch):
    pipeline, queue, prompts = _build(tmp_project, monkeypatch)
    task = Task(description="记录检查点")
    queue.push(task, "engineer")

    result = pipeline.run_task(task, "engineer")

    saved = queue.get(task.id)
    assert prompts == ["engineer", "reviewer"]
    assert saved.metadata["_checkpoint_stage"] == result.stages[-1].stage_number
    assert saved.metadata["_checkpoint_agent_id"] == "reviewer"
    assert saved.metadata.get("_stage_started_at")


def test_stalled_task_resumes_from_checkpoint(tmp_project, monkeypatch):
    pipeline, queue, prompts = _build(tmp_project, monkeypatch)
    task = Task(
        description="停在第二阶段",
        status=TaskStatus.stalled,
        metadata={"pipeline": "default", "_checkpoint_stage": 2},
    )
    task.add_stage(
        StageRecord(
            stage_number=1,
            agent_id="engineer",
            input_text="原始任务",
            output_text="第一阶段完成",
        )
    )
    queue.push(task, "engineer")

    result = pipeline.restart_task(task.id)

    # Stage 1 must not be replayed: the run resumes at the in-progress stage 2.
    assert prompts == ["reviewer"]
    assert [s.stage_number for s in result.stages] == [1, 2]
    assert result.stages[-1].agent_id == "reviewer"


def test_invalid_checkpoint_falls_back_to_last_stage(tmp_project, monkeypatch):
    pipeline, queue, prompts = _build(tmp_project, monkeypatch)
    task = Task(
        description="检查点越界",
        status=TaskStatus.stalled,
        metadata={"pipeline": "default", "_checkpoint_stage": 5},
    )
    task.add_stage(
        StageRecord(
            stage_number=1,
            agent_id="engineer",
            input_text="原始任务",
            output_text="第一阶段完成",
        )
    )
    queue.push(task, "engineer")

    result = pipeline.restart_task(task.id)

    # Out-of-range checkpoint is ignored: the task restarts from stage 1 and
    # replays the normal pipeline.
    assert prompts == ["engineer", "reviewer"]
    assert [s.stage_number for s in result.stages] == [1, 2]
