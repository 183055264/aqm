# -*- coding: utf-8 -*-
"""Durable human directives (interjections) and reply-language handling."""

from __future__ import annotations

import aqm.runtime.meeting_session as meeting_session
from aqm.core.agent import AgentDefinition
from aqm.core.config import ProjectConfig
from aqm.core.pipeline import (
    DIRECTIVE_FOOTER,
    Pipeline,
    build_directive_block,
    detect_language_directive,
)
from aqm.core.task import Task
from aqm.queue.file import FileQueue


class RecordingSession:
    def __init__(self, agent_id, prompts):
        self.agent_id = agent_id
        self.prompts = prompts
        self.closed = False

    @property
    def is_alive(self):
        return not self.closed

    def run(self, prompt, timeout, stop_event, **kwargs):
        self.prompts.append(prompt)
        return f"{self.agent_id} output"

    def close(self):
        self.closed = True


# ── Language detection ──────────────────────────────────────────────────


def test_detect_language_chinese_reply():
    assert (
        detect_language_directive(["各位成员回复问题时全部使用中文回复"])
        == "简体中文"
    )


def test_detect_language_english_reply():
    assert detect_language_directive(["请用英文回复我"]) == "英文"
    assert detect_language_directive(["please reply in English"]) == "英文"


def test_detect_language_ignores_unrelated_notes():
    assert detect_language_directive(["请补充错误处理"]) is None
    assert detect_language_directive(["用中文写代码注释"]) is None


def test_detect_language_ignores_old_notes_when_newer_is_silent():
    notes = ["请用英文回复", "请补充错误处理"]
    assert detect_language_directive(notes) == "英文"


# ── Directive block ─────────────────────────────────────────────────────


def test_directive_block_is_appended_at_the_end():
    prompt = "SYSTEM: do the work"
    block = build_directive_block(["请补充错误处理"], None)
    assert block.startswith("\n\n")
    assert "请补充错误处理" in block
    assert block.endswith(DIRECTIVE_FOOTER)


def test_directive_block_adds_canonical_language_line():
    block = build_directive_block(["全部使用中文回复"], None)
    assert "回复语言：简体中文" in block
    assert "不要中英文混杂" in block


def test_project_language_applies_without_notes():
    block = build_directive_block([], "简体中文")
    assert "回复语言：简体中文" in block
    assert block.endswith(DIRECTIVE_FOOTER)


def test_no_block_when_nothing_to_say():
    assert build_directive_block([], None) == ""


def test_directive_block_skips_pure_nudges():
    block = build_directive_block(["继续啊", "全部使用中文回复"], None)
    assert "继续啊" not in block
    assert "全部使用中文回复" in block


def test_nudges_only_do_not_create_a_block():
    assert build_directive_block(["继续啊", "go on"], None) == ""


# ── Deliverable naming ──────────────────────────────────────────────────


def test_deliverable_naming_chinese_is_explicit():
    block = build_directive_block([], "简体中文", "chinese")
    assert "交付物命名" in block
    assert "项目报告.md" in block
    assert "临时脚本" in block
    assert "PROJECT_REPORT.md" in block


def test_deliverable_naming_auto_follows_chinese_language():
    block = build_directive_block([], "简体中文")
    assert "交付物命名" in block
    assert "中文文件名" in block


def test_deliverable_naming_auto_without_language_stays_silent():
    assert "交付物命名" not in build_directive_block([], None)


def test_deliverable_naming_english():
    block = build_directive_block([], "英文", "english")
    assert "Deliverable naming" in block
    assert "English filenames" in block
    assert "交付物命名" not in block


def test_deliverable_naming_accepts_chinese_config_value():
    block = build_directive_block([], None, "中文")
    assert "交付物命名" in block


def test_pipeline_apply_directives_passes_naming_config(tmp_project):
    queue = FileQueue(tmp_project / ".aqm" / "naming-queue")
    pipeline = Pipeline(
        {},
        queue,
        tmp_project,
        config=ProjectConfig(deliverable_naming="chinese"),
    )
    task = Task(description="x")

    prompt = pipeline._apply_directives("BASE PROMPT", task)

    assert "交付物命名" in prompt
    assert "临时脚本" in prompt


def test_pipeline_apply_directives_uses_task_metadata(tmp_project):
    queue = FileQueue(tmp_project / ".aqm" / "directive-queue")
    pipeline = Pipeline({}, queue, tmp_project, config=ProjectConfig())
    task = Task(description="x", metadata={"interjections": ["全部使用中文回复"]})

    prompt = pipeline._apply_directives("BASE PROMPT", task)

    assert prompt.startswith("BASE PROMPT")
    assert prompt.endswith(DIRECTIVE_FOOTER)
    assert "全部使用中文回复" in prompt
    assert "回复语言：简体中文" in prompt


# ── Persistence across stages ───────────────────────────────────────────


def test_interjection_persists_across_later_stages(monkeypatch, tmp_project):
    from aqm.core.pipeline import submit_interjection

    agents = {
        "planner": AgentDefinition(
            id="planner",
            runtime="claude",
            system_prompt="Plan: {{ input }}",
            handoffs=[{"to": "engineer"}],
        ),
        "engineer": AgentDefinition(
            id="engineer",
            runtime="claude",
            system_prompt="Build: {{ input }}",
            handoffs=[{"to": "writer"}],
        ),
        "writer": AgentDefinition(
            id="writer",
            runtime="claude",
            system_prompt="Write: {{ input }}",
        ),
    }
    prompts = {"planner": [], "engineer": [], "writer": []}
    task = Task(description="persistence test")

    class InterjectingSession(RecordingSession):
        def run(self, prompt, timeout, stop_event, **kwargs):
            self.prompts.append(prompt)
            if self.agent_id == "planner":
                submit_interjection(task.id, "各位成员回复问题时全部使用中文回复")
            return f"{self.agent_id} output"

    def start(agent, project_root, stop_event):
        return InterjectingSession(agent.id, prompts[agent.id])

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "persist-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    queue.push(task, "planner")

    pipeline.run_task(task, "planner")

    # The note was issued during stage 1; it must reach both later stages.
    assert "全部使用中文回复" not in prompts["planner"][0]
    assert "全部使用中文回复" in prompts["engineer"][0]
    assert "全部使用中文回复" in prompts["writer"][0]
    assert "回复语言：简体中文" in prompts["writer"][0]
    assert task.metadata["interjections"] == [
        "各位成员回复问题时全部使用中文回复"
    ]


def test_project_language_is_used_by_new_tasks(monkeypatch, tmp_project):
    agents = {
        "writer": AgentDefinition(
            id="writer", runtime="claude", system_prompt="Write: {{ input }}"
        )
    }
    prompts = []

    def start(agent, project_root, stop_event):
        return RecordingSession(agent.id, prompts)

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "language-queue")
    pipeline = Pipeline(
        agents,
        queue,
        tmp_project,
        resident_agents=True,
        config=ProjectConfig(language="简体中文"),
    )
    task = Task(description="language test")
    queue.push(task, "writer")

    pipeline.run_task(task, "writer")

    assert "回复语言：简体中文" in prompts[0]


# ── Meeting members ─────────────────────────────────────────────────────


def test_meeting_session_applies_project_language(tmp_project):
    (tmp_project / ".aqm").mkdir(exist_ok=True)
    (tmp_project / ".aqm" / "config.yaml").write_text(
        "language: 简体中文\n", encoding="utf-8"
    )
    agent = AgentDefinition(id="a", runtime="claude", system_prompt="角色设定")

    updated = meeting_session._apply_project_language(agent, tmp_project)

    assert updated is not agent
    assert "角色设定" in updated.system_prompt
    assert "【语言要求】" in updated.system_prompt
    assert "简体中文" in updated.system_prompt

    again = meeting_session._apply_project_language(updated, tmp_project)
    assert again.system_prompt.count("【语言要求】") == 1

