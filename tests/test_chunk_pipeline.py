"""End-to-end tests for work blocks (chunks) inside a pipeline run."""

from __future__ import annotations

from pathlib import Path

import pytest

from aqm.core.agent import AgentDefinition
from aqm.core.chunks import ChunkManager, ChunkStatus
from aqm.core.config import ProjectConfig
from aqm.core.pipeline import Pipeline
from aqm.core.task import Task, TaskStatus
from aqm.queue.file import FileQueue


REQUIREMENTS = """# 登录功能需求

## 背景与目标
我们需要一套完整的登录能力，覆盖注册、登录、找回密码、会话安全。

## 功能需求
1. 用户可以通过邮箱和密码注册账号。
2. 支持记住登录状态与会话刷新。
3. 忘记密码时可以通过邮件重置。
4. 所有接口需要审计日志。
5. 前端需要给出明确的错误提示。
6. 需要提供接口文档与部署说明。
7. 需要单元测试覆盖核心路径。

## 非功能需求
- 密码必须加盐哈希存储。
- 登录接口响应时间小于 300ms。
- 失败次数超过阈值需要验证码。

## 验收标准
- 注册、登录、重置密码三条主链路可以走通。
- 单元测试全部通过。
- 文档包含部署与回滚步骤。

## 未决问题
- 是否接入第三方登录。
""" + ("补充说明。" * 120)


class FakeRuntime:
    """Deterministic runtime double for pipeline and planner calls."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def run(self, prompt, agent, task, on_output=None, on_thinking=None, on_tool=None):
        self.calls.append((agent.id, prompt))
        if agent.id == "_chunk_planner":
            return (
                '{"chunks": ['
                '{"title": "实现登录接口", "owner": "engineer", "phase": "实现"},'
                '{"title": "补齐单元测试", "owner": "engineer", "phase": "测试"}'
                "]}"
            )
        queue = self.outputs.get(agent.id, [])
        index = sum(1 for call in self.calls if call[0] == agent.id) - 1
        if index >= len(queue):
            return queue[-1] if queue else ""
        return queue[index]


def make_pipeline(tmp_project, runtime, *, chunks=None, config=None):
    agents = {
        "engineer": AgentDefinition(
            id="engineer",
            runtime="claude",
            system_prompt="实现: {{ input }} {{ chunks }}",
            handoffs=[{"to": "reviewer"}],
        ),
        "reviewer": AgentDefinition(
            id="reviewer",
            runtime="claude",
            system_prompt="评审: {{ input }}",
        ),
    }
    queue = FileQueue(tmp_project / ".aqm" / "chunk-queue")
    config = config or ProjectConfig(chunks=chunks or {})
    pipeline = Pipeline(
        agents, queue, tmp_project, config=config, resident_agents=False,
    )
    pipeline._get_runtime = lambda agent: runtime
    return pipeline, queue


def seed_chunks(tmp_project, task):
    mgr = ChunkManager(tmp_project / ".aqm" / "tasks" / task.id)
    mgr.add("实现登录接口", owner="engineer", source="manual")
    mgr.add("补齐单元测试", owner="engineer", source="manual")
    return mgr


class TestChunkPipeline:
    def test_auto_split_runs_before_first_stage(self, tmp_project):
        (tmp_project / "REQUIREMENTS.md").write_text(REQUIREMENTS, encoding="utf-8")
        runtime = FakeRuntime({
            "engineer": ["CHUNK_DONE: C-001 C-002\n完成登录实现"],
            "reviewer": ["评审通过"],
        })
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="做登录功能")
        queue.push(task, "engineer")
        events = []

        result = pipeline.run_task(
            task, "engineer", on_chunk_event=lambda e, d: events.append((e, d)),
        )

        mgr = ChunkManager(tmp_project / ".aqm" / "tasks" / task.id)
        chunks = mgr.load().chunks
        assert [c.description for c in chunks] == ["实现登录接口", "补齐单元测试"]
        assert all(c.status == ChunkStatus.done for c in chunks)
        assert mgr.load().generated_from.endswith("REQUIREMENTS.md")
        assert any(e == "chunk_split_started" for e, _ in events)
        assert any(e == "chunk_split_done" for e, _ in events)
        assert result.status == TaskStatus.completed
        # Planner prompt must include the requirements document.
        planner_prompts = [p for a, p in runtime.calls if a == "_chunk_planner"]
        assert planner_prompts and "登录功能需求" in planner_prompts[0]
        # Chunk directives must not leak into the stored stage output.
        engineer_stage = next(s for s in result.stages if s.agent_id == "engineer")
        assert "CHUNK_DONE" not in engineer_stage.output_text

    def test_chunks_are_injected_into_prompt(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["CHUNK_DONE: C-001 C-002\n完成"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="做一个功能")
        seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        pipeline.run_task(task, "engineer")

        engineer_prompts = [p for a, p in runtime.calls if a == "engineer"]
        assert "C-001" in engineer_prompts[0]
        assert "工作块协议" in engineer_prompts[0]

    def test_retry_reruns_member_until_chunks_done(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": [
                "先做了一半",
                "还是差一点",
                "CHUNK_DONE: C-001 C-002\n终于完成",
            ],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "retry", "retry_limit": 2},
        )
        task = Task(description="做一个功能")
        mgr = seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        engineer_calls = [p for a, p in runtime.calls if a == "engineer"]
        assert len(engineer_calls) == 3
        assert "本轮工作块未完成" in engineer_calls[1]
        assert "本轮工作块未完成" in engineer_calls[2]
        assert mgr.all_done()
        assert result.status == TaskStatus.completed

    def test_retry_limit_stops_and_continues(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["没做", "还是没做", "仍然没做"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "retry", "retry_limit": 1},
        )
        task = Task(description="做一个功能")
        mgr = seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        engineer_calls = [p for a, p in runtime.calls if a == "engineer"]
        assert len(engineer_calls) == 2  # first attempt + one retry
        assert not mgr.all_done()
        # The default policy must not fail the whole task.
        assert result.status == TaskStatus.completed

    def test_enforce_fail_stops_task(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["没做"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "fail"},
        )
        task = Task(description="做一个功能")
        seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        assert result.status == TaskStatus.failed
        assert "unfinished" in (result.metadata.get("error") or "")

    def test_enforce_off_ignores_pending_chunks(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["没做"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "off"},
        )
        task = Task(description="做一个功能")
        mgr = seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        assert len([1 for a, _ in runtime.calls if a == "engineer"]) == 1
        assert not mgr.all_done()
        assert result.status == TaskStatus.completed

    def test_owner_filter_ignores_other_members_chunks(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["我完成我的部分"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "retry", "retry_limit": 2},
        )
        task = Task(description="做一个功能")
        mgr = ChunkManager(tmp_project / ".aqm" / "tasks" / task.id)
        mgr.add("写文档", owner="writer")
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        # engineer is not responsible for writer's chunk, so one call is enough.
        assert len([1 for a, _ in runtime.calls if a == "engineer"]) == 1
        assert not mgr.all_done()
        assert result.status == TaskStatus.completed

    def test_auto_claim_marks_first_chunk_in_progress(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["CHUNK_DONE: C-001 C-002\n完成"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="做一个功能")
        seed_chunks(tmp_project, task)
        queue.push(task, "engineer")
        events = []

        pipeline.run_task(
            task, "engineer", on_chunk_event=lambda e, d: events.append((e, d)),
        )

        starts = [d["chunk_id"] for e, d in events if d.get("action") == "start"]
        assert "C-001" in starts

    def test_stage_start_releases_stale_in_progress_chunks(self, tmp_project):
        """Only the member currently working may hold an in-progress block.

        Older runs claimed a block per stage but never cleared it when the
        member forgot ``CHUNK_DONE``, so the panel showed several blocks
        "in progress" at once.
        """
        runtime = FakeRuntime({
            "engineer": ["CHUNK_DONE: C-001\n完成"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="做一个功能")
        mgr = seed_chunks(tmp_project, task)
        # Simulate leftovers from earlier stages/runs.
        mgr.start("C-001")
        mgr.start("C-002")
        queue.push(task, "engineer")
        events = []

        pipeline.run_task(
            task, "engineer", on_chunk_event=lambda e, d: events.append((e, d)),
        )

        released = [
            d["chunk_id"] for e, d in events if d.get("action") == "release"
        ]
        assert released == ["C-001", "C-002"]
        starts = [d["chunk_id"] for e, d in events if d.get("action") == "start"]
        assert starts == ["C-001"]

    def test_finished_task_has_no_in_progress_chunks(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["先做了一半，没有打勾"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "off"},
        )
        task = Task(description="做一个功能")
        mgr = seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        assert result.status == TaskStatus.completed
        assert all(
            c.status != ChunkStatus.in_progress for c in mgr.load().chunks
        )

    def test_agent_can_add_and_block_chunks(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": [
                "CHUNK_ADD: 补充审计日志 [owner=engineer] [phase=实现]\n"
                "CHUNK_BLOCK: C-001 等待接口文档\n"
                "先做别的"
            ],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enforce": "off"},
        )
        task = Task(description="做一个功能")
        seed_chunks(tmp_project, task)
        queue.push(task, "engineer")

        pipeline.run_task(task, "engineer")

        chunks = {c.id: c for c in ChunkManager(
            tmp_project / ".aqm" / "tasks" / task.id).load().chunks}
        assert chunks["C-001"].status == ChunkStatus.blocked
        assert chunks["C-001"].note.startswith("等待接口文档")
        assert any(c.description == "补充审计日志" for c in chunks.values())

    def test_split_skipped_for_short_description(self, tmp_project):
        runtime = FakeRuntime({"engineer": ["完成了"], "reviewer": ["通过"]})
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="小改动")
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        assert not [p for a, p in runtime.calls if a == "_chunk_planner"]
        assert result.metadata.get("_chunk_split") == "skipped"

    def test_disabled_chunks_do_nothing(self, tmp_project):
        runtime = FakeRuntime({"engineer": ["完成了"], "reviewer": ["通过"]})
        pipeline, queue = make_pipeline(
            tmp_project, runtime, chunks={"enabled": False},
        )
        task = Task(description="做功能")
        seed_chunks(tmp_project, task)
        queue.push(task, "engineer")
        events = []

        pipeline.run_task(task, "engineer", on_chunk_event=lambda e, d: events.append(e))

        assert events == []
        engineer_prompt = [p for a, p in runtime.calls if a == "engineer"][0]
        assert "工作块清单" not in engineer_prompt

    def test_focus_block_prioritises_one_chunk(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["CHUNK_DONE: C-001 C-002\n完成"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="做一个功能")
        seed_chunks(tmp_project, task)
        task.metadata["_chunk_focus"] = {
            "chunk_id": "C-001", "agent_id": "engineer",
        }
        queue.push(task, "engineer")

        result = pipeline.run_task(task, "engineer")

        engineer_prompt = [p for a, p in runtime.calls if a == "engineer"][0]
        assert "人类指定优先处理" in engineer_prompt
        assert "C-001" in engineer_prompt
        reviewer_prompt = [p for a, p in runtime.calls if a == "reviewer"][0]
        assert "人类指定优先处理" not in reviewer_prompt
        # The hint is dropped once the focused block is done.
        assert "_chunk_focus" not in result.metadata

    def test_restart_accepts_pending_task(self, tmp_project):
        runtime = FakeRuntime({
            "engineer": ["CHUNK_DONE: C-001 C-002\n完成"],
            "reviewer": ["通过"],
        })
        pipeline, queue = make_pipeline(tmp_project, runtime)
        task = Task(description="做一个功能")
        mgr = seed_chunks(tmp_project, task)
        queue.push(task, "engineer")
        assert task.status == TaskStatus.pending

        result = pipeline.restart_task(task.id)

        assert result.status == TaskStatus.completed
        assert mgr.all_done()
