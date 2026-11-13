# -*- coding: utf-8 -*-
"""Human gate rejection routing: explicit target + reason-based inference."""

from __future__ import annotations

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aqm.core.agent import AgentDefinition
from aqm.core.pipeline import Pipeline
from aqm.core.project import get_db_path, init_project
from aqm.core.task import StageRecord, Task, TaskStatus
from aqm.queue.file import FileQueue
from aqm.queue.sqlite import SQLiteQueue
from aqm.web.api.tasks import create_tasks_router


def _build(monkeypatch, tmp_project, *, agents=None, gate_agent="reviewer"):
    agents = agents or {
        "engineer": AgentDefinition(
            id="engineer", name="实现与回测工程师", runtime="claude",
            system_prompt="Build: {{ input }}",
        ),
        "reviewer": AgentDefinition(
            id="reviewer", name="复核与合规审查", runtime="claude",
            system_prompt="Review: {{ input }}",
        ),
    }
    queue = FileQueue(tmp_project / ".aqm" / "gate-queue")
    pipeline = Pipeline(agents, queue, tmp_project)
    task = Task(
        description="gate routing test",
        status=TaskStatus.awaiting_gate,
        metadata={"pipeline": "default"},
    )
    task.add_stage(
        StageRecord(
            stage_number=1,
            agent_id=gate_agent,
            input_text="原始任务输入",
            output_text="这是被拒绝的上一版输出",
            gate_result="rejected",
        )
    )
    queue.push(task, gate_agent)

    prompts: list[tuple[str, str]] = []

    def fake_execute(prompt, agent, task, **_kwargs):
        prompts.append((agent.id, prompt))
        return f"{agent.id} new output"

    monkeypatch.setattr(pipeline, "_execute_agent", fake_execute)
    return pipeline, task, prompts


def test_explicit_target_overrides_config(monkeypatch, tmp_project):
    pipeline, task, prompts = _build(monkeypatch, tmp_project)

    result = pipeline.resume_task(
        task.id, "rejected", "格式不对，重新整理", target_agent_id="engineer"
    )

    assert prompts[0][0] == "engineer"
    assert "人工审批未通过" in prompts[0][1]
    assert "格式不对，重新整理" in prompts[0][1]
    assert "这是被拒绝的上一版输出" in prompts[0][1]
    assert "原始任务输入" in prompts[0][1]
    assert result.metadata["last_reject_target"] == "engineer"


def test_reason_infers_member_by_name(monkeypatch, tmp_project):
    pipeline, task, prompts = _build(monkeypatch, tmp_project)

    # No explicit target: the reason names the engineer.
    result = pipeline.resume_task(
        task.id, "rejected", "请实现与回测工程师补充回测样本"
    )

    assert prompts[0][0] == "engineer"
    assert "补充回测样本" in prompts[0][1]
    assert result.metadata["last_reject_target"] == "engineer"


def test_reason_infers_member_by_id(monkeypatch, tmp_project):
    pipeline, task, prompts = _build(monkeypatch, tmp_project)

    pipeline.resume_task(task.id, "rejected", "engineer 需要重跑一遍")

    assert prompts[0][0] == "engineer"


def test_ambiguous_reason_falls_back_to_configured_handoff(monkeypatch, tmp_project):
    # reviewer has an on_reject handoff to writer; the reason names two members
    # so inference must not guess.
    agents = {
        "engineer": AgentDefinition(id="engineer", name="工程师", runtime="claude"),
        "writer": AgentDefinition(id="writer", name="文档员", runtime="claude"),
        "reviewer": AgentDefinition(
            id="reviewer",
            name="复核员",
            runtime="claude",
            handoffs=[{"to": "writer", "condition": "on_reject"}],
        ),
    }
    pipeline, task, prompts = _build(
        monkeypatch, tmp_project, agents=agents, gate_agent="reviewer"
    )

    pipeline.resume_task(task.id, "rejected", "工程师和文档员都看一下")

    assert prompts[0][0] == "writer"


def test_no_handoff_rejection_re_runs_same_agent(monkeypatch, tmp_project):
    pipeline, task, prompts = _build(monkeypatch, tmp_project)

    result = pipeline.resume_task(task.id, "rejected", "太短了，需要展开")

    assert prompts[0][0] == "reviewer"
    assert "太短了，需要展开" in prompts[0][1]
    assert result.metadata["last_reject_target"] == "reviewer"


def test_unknown_target_is_rejected(monkeypatch, tmp_project):
    pipeline, task, _prompts = _build(monkeypatch, tmp_project)

    with pytest.raises(ValueError, match="not defined"):
        pipeline.resume_task(
            task.id, "rejected", "原因", target_agent_id="ghost"
        )


def test_approval_without_handoff_completes(monkeypatch, tmp_project):
    pipeline, task, prompts = _build(monkeypatch, tmp_project)

    result = pipeline.resume_task(task.id, "approved", "")

    assert prompts == []
    assert result.status == TaskStatus.completed


def test_infer_target_ignores_ascii_inside_words(monkeypatch, tmp_project):
    pipeline, task, _prompts = _build(monkeypatch, tmp_project)

    assert pipeline._infer_target_from_reason("copywriter 需要调整") is None
    assert pipeline._infer_target_from_reason("让 engineer 调整") == "engineer"


def test_reject_api_rejects_unknown_target(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    root = init_project(project_dir)
    pipelines_dir = root / ".aqm" / "pipelines"
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    (pipelines_dir / "default.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aqm/v0.1",
                "entry_point": "first",
                "agents": [
                    {"id": "reviewer", "name": "复核员", "runtime": "claude"},
                    {"id": "writer", "name": "文档员", "runtime": "claude"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    queue = SQLiteQueue(get_db_path(root))
    task = Task(
        description="gate api",
        status=TaskStatus.awaiting_gate,
        metadata={"pipeline": "default"},
    )
    task.add_stage(
        StageRecord(
            stage_number=1,
            agent_id="reviewer",
            input_text="input",
            output_text="output",
        )
    )
    queue.push(task, "reviewer")
    queue.close()

    app = FastAPI()
    app.include_router(create_tasks_router(root))
    client = TestClient(app)

    response = client.post(
        f"/api/tasks/{task.id}/reject",
        json={"reason": "原因", "target_agent_id": "ghost"},
    )
    assert response.status_code == 400
    assert "ghost" in response.json()["detail"]

