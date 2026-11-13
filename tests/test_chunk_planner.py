"""Tests for automatic work-block (chunk) planning."""

from __future__ import annotations

from pathlib import Path

import pytest

from aqm.core.chunk_planner import (
    PlannedChunk,
    build_split_prompt,
    find_requirements,
    parse_plan,
    plan_chunks,
    truncate_document,
)
from aqm.core.chunks import content_hash
from aqm.core.task import Task


# ── Requirements discovery ────────────────────────────────────────────


class TestFindRequirements:
    def test_prefers_metadata_path(self, tmp_path):
        doc = tmp_path / "docs" / "REQ.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# 需求\n要做的事情", encoding="utf-8")
        (tmp_path / "REQUIREMENTS.md").write_text("# 顶层需求", encoding="utf-8")
        task = Task(description="desc", metadata={"requirements_path": str(doc)})

        text, source, digest = find_requirements(task, tmp_path)
        assert text.startswith("# 需求")
        assert source == str(doc)
        assert digest == content_hash(text)

    def test_relative_metadata_path(self, tmp_path):
        doc = tmp_path / "REQ.md"
        doc.write_text("相对路径需求", encoding="utf-8")
        task = Task(description="desc", metadata={"requirements_path": "REQ.md"})
        text, _, _ = find_requirements(task, tmp_path)
        assert text == "相对路径需求"

    def test_falls_back_to_project_requirements(self, tmp_path):
        (tmp_path / "REQUIREMENTS.md").write_text("项目需求", encoding="utf-8")
        text, source, _ = find_requirements(Task(description="d"), tmp_path)
        assert text == "项目需求"
        assert source.endswith("REQUIREMENTS.md")

    def test_falls_back_to_docs_dir(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "REQUIREMENTS.md").write_text("docs 需求", encoding="utf-8")
        text, _, _ = find_requirements(Task(description="d"), tmp_path)
        assert text == "docs 需求"

    def test_falls_back_to_description(self, tmp_path):
        text, source, _ = find_requirements(Task(description="只有任务描述"), tmp_path)
        assert text == "只有任务描述"
        assert source == "task.description"


# ── Truncation ────────────────────────────────────────────────────────


class TestTruncateDocument:
    def test_short_document_untouched(self):
        text = "# 标题\n内容"
        assert truncate_document(text, 1000) == text

    def test_long_document_keeps_headings(self):
        text = "\n\n".join(
            f"# 第{i}节\n" + ("内容" * 400) for i in range(10)
        )
        result = truncate_document(text, 2000)
        assert len(result) <= 2400
        assert "# 第0节" in result
        assert "截断" in result

    def test_acceptance_section_kept(self):
        sections = ["# 背景\n" + "背景" * 300, "# 验收标准\n" + "验收" * 100]
        result = truncate_document("\n\n".join(sections), 1200)
        assert "验收标准" in result


# ── Plan parsing ──────────────────────────────────────────────────────


class TestParsePlan:
    OWNERS = {"engineer", "writer"}

    def test_plain_json(self):
        text = (
            '{"chunks": ['
            '{"title": "实现登录", "detail": "写后端", "owner": "engineer", "phase": "实现"},'
            '{"title": "写文档", "owner": "writer", "phase": "文档"}'
            "]}"
        )
        chunks = parse_plan(text, 30, self.OWNERS)
        assert [c.title for c in chunks] == ["实现登录", "写文档"]
        assert chunks[0].owner == "engineer"
        assert chunks[1].phase == "文档"

    def test_json_in_markdown_fence(self):
        text = (
            "```json\n"
            '{"chunks": [{"title": "建表", "owner": "engineer"}]}\n'
            "```"
        )
        chunks = parse_plan(text, 30, self.OWNERS)
        assert len(chunks) == 1
        assert chunks[0].title == "建表"

    def test_json_with_prose_around(self):
        text = (
            "好的，拆分如下：\n"
            '{"chunks": [{"title": "搭建项目", "owner": "engineer"}]}\n'
            "以上共 1 块。"
        )
        chunks = parse_plan(text, 30, self.OWNERS)
        assert len(chunks) == 1

    def test_bare_list_response(self):
        text = '[{"title": "初始化仓库"}, {"title": "补测试"}]'
        chunks = parse_plan(text, 30, self.OWNERS)
        assert [c.title for c in chunks] == ["初始化仓库", "补测试"]

    def test_unknown_owner_is_cleared(self):
        text = '{"chunks": [{"title": "X", "owner": "nobody"}]}'
        chunks = parse_plan(text, 30, self.OWNERS)
        assert chunks[0].owner == ""

    def test_deduplicates_near_identical_titles(self):
        text = (
            '{"chunks": ['
            '{"title": "实现登录接口"},'
            '{"title": "实现登录接口。"},'
            '{"title": "编写单元测试"}'
            "]}"
        )
        chunks = parse_plan(text, 30, self.OWNERS)
        assert len(chunks) == 2

    def test_respects_max_chunks(self):
        items = ",".join(f'{{"title": "任务{i}"}}' for i in range(40))
        chunks = parse_plan('{"chunks": [' + items + "]}", 5, self.OWNERS)
        assert len(chunks) == 5

    def test_markdown_table_fallback(self):
        text = (
            "| 工作块 | 负责人 | 阶段 |\n"
            "|---|---|---|\n"
            "| 实现接口 | engineer | 实现 |\n"
            "| 写文档 | writer | 文档 |\n"
        )
        chunks = parse_plan(text, 30, self.OWNERS)
        assert [c.title for c in chunks] == ["实现接口", "写文档"]
        assert chunks[1].owner == "writer"

    def test_numbered_list_fallback(self):
        text = "1. 初始化项目\n2. 实现功能（owner=engineer, phase=实现）\n3. 补测试"
        chunks = parse_plan(text, 30, self.OWNERS)
        assert len(chunks) == 3
        assert chunks[1].owner == "engineer"
        assert chunks[1].phase == "实现"

    def test_garbage_returns_empty(self):
        assert parse_plan("完全不是清单", 30, self.OWNERS) == []

    def test_empty_title_skipped(self):
        text = '{"chunks": [{"title": "  "}, {"title": "有效"}]}'
        chunks = parse_plan(text, 30, self.OWNERS)
        assert [c.title for c in chunks] == ["有效"]


# ── Prompt ────────────────────────────────────────────────────────────


class TestBuildSplitPrompt:
    def test_includes_roles_and_rules(self):
        agents = {
            "engineer": type("A", (), {
                "id": "engineer",
                "name": "工程师",
                "runtime": "claude",
                "system_prompt": "写代码",
                "handoffs": [],
            })(),
        }
        prompt = build_split_prompt("需求正文", agents, 12, "简体中文")
        assert "engineer" in prompt
        assert "需求正文" in prompt
        assert "12" in prompt
        assert "简体中文" in prompt
        assert "JSON" in prompt


# ── Planner entry point ───────────────────────────────────────────────


class TestPlanChunks:
    def test_calls_runtime_and_parses(self, tmp_project):
        class FakeRuntime:
            def __init__(self):
                self.prompts = []

            def run(self, prompt, agent, task, **kwargs):
                self.prompts.append(prompt)
                return (
                    '{"chunks": [{"title": "实现登录", "owner": "engineer"}]}'
                )

        runtime = FakeRuntime()
        agents = {
            "engineer": type("A", (), {
                "id": "engineer", "name": "工程师", "runtime": "claude",
                "system_prompt": "", "handoffs": [],
            })(),
        }
        task = Task(description="登录功能")
        planned = plan_chunks(
            project_root=tmp_project,
            task=task,
            requirements="实现登录功能",
            agents=agents,
            runtime=runtime,
            runtime_name="claude",
            model="sonnet",
        )
        assert len(planned) == 1
        assert planned[0].title == "实现登录"
        assert runtime.prompts and "实现登录功能" in runtime.prompts[0]
        raw = (
            tmp_project / ".aqm" / "tasks" / task.id / "chunks.split.raw.txt"
        )
        assert raw.exists()

    def test_runtime_failure_returns_empty(self, tmp_project):
        class BoomRuntime:
            def run(self, *args, **kwargs):
                raise RuntimeError("CLI exploded")

        planned = plan_chunks(
            project_root=tmp_project,
            task=Task(description="x"),
            requirements="y",
            agents={},
            runtime=BoomRuntime(),
            runtime_name="claude",
        )
        assert planned == []
