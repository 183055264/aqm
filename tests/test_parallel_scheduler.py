"""Tests for deterministic batch scheduling and the block-level review loop."""

from __future__ import annotations

import threading

import pytest

from aqm.core.agent import AgentDefinition, Handoff
from aqm.core.chunks import ChunkManager, ChunkStatus
from aqm.core.config import ProjectConfig
from aqm.core.pipeline import Pipeline
from aqm.core.scheduler import (
    AgentPlan,
    audit_changes,
    plan_batch,
    resolve_parallel_settings,
    scope_matches,
    scopes_overlap,
)
from aqm.core.task import Task, TaskStatus
from aqm.queue.file import FileQueue


# ---------------------------------------------------------------------------
# Scheduler units
# ---------------------------------------------------------------------------


class TestPlanBatch:
    def test_parallel_settings_are_disabled_by_default(self):
        settings = resolve_parallel_settings({})
        assert settings["enabled"] is False
        assert settings["max_workers"] == 2
        assert settings["on_conflict"] == "block"

    def test_member_without_scope_runs_alone(self):
        decision = plan_batch(
            [
                AgentPlan("ui", ["src/ui/**"], 2),
                AgentPlan("api", ["src/api/**"], 2),
                AgentPlan("chaos", [], 2),
            ],
            max_workers=3,
        )
        assert decision.selected == ["api", "ui"]
        assert decision.mode == "parallel"
        reasons = {item["agent_id"]: item["reason"] for item in decision.waiting}
        assert "write_scope" in reasons["chaos"]

    def test_overlapping_scopes_degrade_to_serial(self):
        decision = plan_batch(
            [
                AgentPlan("ui", ["src/**"], 2),
                AgentPlan("api", ["src/api/**"], 2),
            ],
            max_workers=2,
        )
        assert decision.selected == ["api"]
        assert decision.mode == "serial"
        assert "重叠" in decision.waiting[-1]["reason"]

    def test_hot_file_forces_serial(self):
        decision = plan_batch(
            [
                AgentPlan("ui", ["src/ui/**", "package.json"], 2),
                AgentPlan("api", ["src/api/**"], 2),
            ],
            max_workers=2,
            hot_files=["package.json"],
        )
        assert decision.selected == ["api"]
        assert any("热文件" in item["reason"] for item in decision.waiting)

    def test_max_workers_truncates_and_reports(self):
        decision = plan_batch(
            [
                AgentPlan("a", ["src/a/**"], 1),
                AgentPlan("b", ["src/b/**"], 1),
                AgentPlan("c", ["src/c/**"], 1),
            ],
            max_workers=2,
        )
        assert decision.selected == ["a", "b"]
        assert any("并行上限" in item["reason"] for item in decision.waiting)

    def test_zero_pending_is_reported(self):
        decision = plan_batch([AgentPlan("a", ["src/a/**"], 0)], max_workers=2)
        assert decision.selected == []
        assert "工作块" in decision.waiting[0]["reason"]

    def test_scope_helpers(self):
        assert scope_matches("src/ui/**", "src/ui/App.tsx")
        assert not scope_matches("src/ui/**", "src/api/x.py")
        assert scopes_overlap("src/**", "src/ui/**")
        assert not scopes_overlap("src/ui/**", "src/api/**")

    def test_audit_attributes_files(self):
        conflicts, unclaimed = audit_changes(
            {"a": ["src/a/**"], "b": ["src/b/**"]},
            ["src/a/x.py", "docs/z.md"],
        )
        assert conflicts == []
        assert unclaimed == ["docs/z.md"]


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


class RecordingRuntime:
    """Runtime double that records start order and peak concurrency."""

    def __init__(self, outputs, delay: float = 0.02, writes=None, root=None):
        self.outputs = outputs
        self.delay = delay
        self.writes = writes or {}
        self.root = root
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.order: list[str] = []

    def run(self, prompt, agent, task, on_output=None, on_thinking=None, on_tool=None):
        with self._lock:
            self.calls.append((agent.id, prompt))
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.order.append(agent.id)
            index = sum(1 for call in self.calls if call[0] == agent.id) - 1
        try:
            import time

            time.sleep(self.delay)
            for rel, content in self.writes.get(agent.id, []):
                from pathlib import Path

                path = Path(self.root or ".") / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            queue = self.outputs.get(agent.id, [])
            if index >= len(queue):
                return queue[-1] if queue else ""
            return queue[index]
        finally:
            with self._lock:
                self.active -= 1


def make_pipeline(tmp_project, runtime, agents, *, config=None):
    queue = FileQueue(tmp_project / ".aqm" / "parallel-queue")
    pipeline = Pipeline(
        agents, queue, tmp_project, config=config or ProjectConfig(),
        resident_agents=False,
    )
    pipeline._get_runtime = lambda agent: runtime
    return pipeline, queue


def module_agents(**extra):
    return {
        "architect": AgentDefinition(
            id="architect", runtime="claude", system_prompt="规划: {{ input }}",
        ),
        "ui": AgentDefinition(
            id="ui", runtime="claude", system_prompt="界面: {{ input }} {{ chunks }}",
            write_scope=["src/ui/**"], **extra.get("ui", {}),
        ),
        "api": AgentDefinition(
            id="api", runtime="claude", system_prompt="接口: {{ input }} {{ chunks }}",
            write_scope=["src/api/**"], **extra.get("api", {}),
        ),
    }


def seed(task_dir, owner, text, order=0):
    mgr = ChunkManager(task_dir)
    return mgr.add(text, owner=owner, source="manual")


class TestParallelPhase:
    def test_disabled_by_default_keeps_serial_flow(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["CHUNK_DONE: C-001"],
            "api": ["CHUNK_DONE: C-002"],
        })
        agents = module_agents()
        pipeline, queue = make_pipeline(tmp_project, runtime, agents)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.status == TaskStatus.completed
        # Nothing ran in parallel: only the serial stage was executed.
        assert runtime.peak == 1
        assert [agent_id for agent_id, _ in runtime.calls] == ["architect"]

    def test_two_scoped_members_run_together(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["CHUNK_DONE: C-001"],
            "api": ["CHUNK_DONE: C-002"],
        })
        agents = module_agents()
        config = ProjectConfig(pipeline={"max_stages": 20, "parallel": {
            "enabled": True, "max_workers": 2, "audit": False,
        }})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.status == TaskStatus.completed
        assert runtime.peak == 2
        assert set(runtime.order[1:]) == {"ui", "api"}
        assert len(result.stages) == 3
        assert len({s.stage_number for s in result.stages}) == 3
        mgr = ChunkManager(tmp_project / ".aqm" / "tasks" / task.id)
        assert all(c.status == ChunkStatus.done for c in mgr.load().chunks)
        decision = result.metadata["_last_schedule_decision"]
        log = result.metadata["_schedule_log"]
        assert any(entry["mode"] == "parallel" for entry in log)
        assert decision["mode"] == "serial"  # the closing round explains why it stopped
        assert any("不足 2 人" in note for note in decision["notes"])

    def test_parallel_batch_then_serial_chain_skips_covered_members(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["CHUNK_DONE: C-001"],
            "api": ["CHUNK_DONE: C-002"],
            "qa": ["验收通过"],
        })
        agents = module_agents()
        agents["architect"].handoffs = [Handoff(to="ui")]
        agents["ui"].handoffs = [Handoff(to="api")]
        agents["api"].handoffs = [Handoff(to="qa")]
        agents["qa"] = AgentDefinition(
            id="qa", runtime="claude", system_prompt="验收: {{ input }}",
        )
        config = ProjectConfig(pipeline={"max_stages": 20, "parallel": {
            "enabled": True, "max_workers": 2, "audit": False,
        }})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.status == TaskStatus.completed
        # ui/api ran once (inside the batch); only the integration stage re-runs.
        calls = [agent_id for agent_id, _ in runtime.calls]
        assert calls[0] == "architect"
        assert calls[-1] == "qa"
        assert sorted(calls[1:-1]) == ["api", "ui"]
        assert set(result.metadata["_parallel_covered"]) == {"ui", "api"}

    def test_overlapping_scopes_run_serially(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["CHUNK_DONE: C-001"],
            "api": ["CHUNK_DONE: C-002"],
        })
        agents = module_agents()
        agents["api"].write_scope = ["src/**"]
        config = ProjectConfig(pipeline={"max_stages": 20, "parallel": {
            "enabled": True, "max_workers": 2, "audit": False,
        }})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.status == TaskStatus.completed
        assert runtime.peak == 1
        # The overlapping members are still handled, just one after another.
        rounds = result.metadata.get("_schedule_log", [])
        assert any(
            "重叠" in note
            for entry in rounds
            for note in entry.get("notes", [])
        ) or any(
            "重叠" in item.get("reason", "")
            for entry in rounds
            for item in entry.get("waiting", [])
        )

    def test_parallel_member_emitting_no_done_stops_after_stall(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["我还在想"],
            "api": ["我也还在想"],
        })
        agents = module_agents()
        config = ProjectConfig(pipeline={"max_stages": 20, "parallel": {
            "enabled": True, "max_workers": 2, "audit": False,
        }})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.metadata.get("_parallel_stalled") is True
        assert len(runtime.calls) <= 1 + 4  # architect + at most two stalled rounds

    def test_parallel_round_copies_checkpoint(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["CHUNK_DONE: C-001"],
            "api": ["CHUNK_DONE: C-002"],
        }, root=tmp_project)
        (tmp_project / "src" / "ui").mkdir(parents=True)
        (tmp_project / "src" / "ui" / "App.tsx").write_text("old", encoding="utf-8")
        agents = module_agents()
        config = ProjectConfig(pipeline={"max_stages": 20, "parallel": {
            "enabled": True, "max_workers": 2, "audit": False, "checkpoint": True,
        }})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.status == TaskStatus.completed
        checkpoint = tmp_project / ".aqm" / "checkpoints" / task.id / "stage-002"
        assert (checkpoint / "src" / "ui" / "App.tsx").read_text(encoding="utf-8") == "old"
        assert (checkpoint / "manifest.json").exists()

    def test_write_outside_scope_blocks_the_task(self, tmp_project):
        runtime = RecordingRuntime({
            "architect": ["规划完成"],
            "ui": ["CHUNK_DONE: C-001"],
            "api": ["CHUNK_DONE: C-002"],
        }, root=tmp_project, writes={"api": [("docs/leak.md", "oops")]})
        agents = module_agents()
        config = ProjectConfig(pipeline={"max_stages": 20, "parallel": {
            "enabled": True, "max_workers": 2, "audit": True, "checkpoint": False,
        }})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="做界面和接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "ui", "界面首页")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "api", "接口列表")
        queue.push(task, "architect")

        result = pipeline.run_task(task, "architect")

        assert result.status == TaskStatus.failed
        assert "写冲突" in result.metadata.get("error", "")
        assert "docs/leak.md" in result.metadata.get("error", "")


class TestBlockReviewHandoff:
    def build(self, tmp_project):
        agents = {
            "writer": AgentDefinition(
                id="writer", runtime="claude",
                system_prompt="写: {{ input }} {{ chunks }}",
                handoffs=[{"to": "reviewer"}],
                chunks={"enabled": True, "auto_split": False,
                        "forward_on_done": True, "review_owner": "reviewer",
                        "enforce": "off"},
            ),
            "reviewer": AgentDefinition(
                id="reviewer", runtime="claude",
                system_prompt="复核: {{ input }} {{ chunks }}",
                chunks={"enabled": True, "auto_split": False, "enforce": "off"},
            ),
        }
        return agents

    def test_reject_returns_block_to_owner(self, tmp_project):
        runtime = RecordingRuntime({
            "writer": ["实现完成 CHUNK_DONE: C-001", "补上校验 CHUNK_DONE: C-001"],
            "reviewer": ["CHUNK_REJECT: C-001 缺少参数校验", "复核通过"],
        })
        agents = self.build(tmp_project)
        config = ProjectConfig(chunks={"auto_split": False, "enforce": "off"})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="实现接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "writer", "实现接口")
        queue.push(task, "writer")

        result = pipeline.run_task(task, "writer")

        assert result.status == TaskStatus.completed
        assert [agent_id for agent_id, _ in runtime.calls] == [
            "writer", "reviewer", "writer", "reviewer",
        ]
        reviewer_prompt = runtime.calls[1][1]
        assert "块级复核请求" in reviewer_prompt
        writer_prompt = runtime.calls[2][1]
        assert "块级复核退回" in writer_prompt
        assert "缺少参数校验" in writer_prompt
        mgr = ChunkManager(tmp_project / ".aqm" / "tasks" / task.id)
        chunk = mgr.get("C-001")
        assert chunk.status == ChunkStatus.done
        assert chunk.completed_by == "writer"
        assert "_chunk_review" not in result.metadata

    def test_approve_loop_drains_all_blocks(self, tmp_project):
        runtime = RecordingRuntime({
            "writer": ["CHUNK_DONE: C-001", "CHUNK_DONE: C-002"],
            "reviewer": ["通过，继续", "全部通过"],
        })
        agents = self.build(tmp_project)
        agents["writer"].chunks.max_chunks_per_stage = 1
        config = ProjectConfig(chunks={"auto_split": False, "enforce": "off"})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="实现接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "writer", "实现接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "writer", "补充文档")
        queue.push(task, "writer")

        result = pipeline.run_task(task, "writer")

        assert result.status == TaskStatus.completed
        assert [agent_id for agent_id, _ in runtime.calls] == [
            "writer", "reviewer", "writer", "reviewer",
        ]
        # The reviewer's approval hand-back names the next block.
        assert "继续处理 C-002" in runtime.calls[2][1]
        mgr = ChunkManager(tmp_project / ".aqm" / "tasks" / task.id)
        assert all(c.status == ChunkStatus.done for c in mgr.load().chunks)

    def test_review_disabled_skips_loop(self, tmp_project):
        runtime = RecordingRuntime({
            "writer": ["CHUNK_DONE: C-001"],
            "reviewer": ["复核"],
        })
        agents = self.build(tmp_project)
        agents["writer"].chunks.forward_on_done = False
        config = ProjectConfig(chunks={"auto_split": False, "enforce": "off"})
        pipeline, queue = make_pipeline(tmp_project, runtime, agents, config=config)
        task = Task(description="实现接口")
        seed(tmp_project / ".aqm" / "tasks" / task.id, "writer", "实现接口")
        queue.push(task, "writer")

        pipeline.run_task(task, "writer")

        assert [agent_id for agent_id, _ in runtime.calls] == ["writer", "reviewer"]
        assert "块级复核请求" not in runtime.calls[1][1]


class TestChunkClaimConcurrency:
    def test_chunk_reject_directive_returns_block_to_pending(self, tmp_path):
        from aqm.core.chunks import parse_chunk_directives, sanitize_chunk_directives

        mgr = ChunkManager(tmp_path)
        chunk = mgr.add("实现接口", owner="writer", source="manual")
        mgr.mark_done(chunk.id, completed_by="writer")

        text = f"CHUNK_REJECT: {chunk.id} 缺少参数校验"
        actions = parse_chunk_directives(text, mgr, "reviewer")

        assert actions[0]["action"] == "reject"
        assert actions[0]["reason"] == "缺少参数校验"
        updated = mgr.get(chunk.id)
        assert updated.status == ChunkStatus.pending
        assert updated.note == "缺少参数校验"
        assert updated.completed_by == ""
        assert updated.owner == "writer"
        assert "CHUNK_REJECT" not in sanitize_chunk_directives(text)

    def test_two_threads_never_claim_the_same_block(self, tmp_path):
        mgr = ChunkManager(tmp_path)
        for index in range(1, 21):
            mgr.add(f"块 {index}", owner="writer", source="manual")

        claimed: list[str] = []
        lock = threading.Lock()

        def worker():
            while True:
                chunk = mgr.claim_next("writer")
                if chunk is None:
                    return
                with lock:
                    claimed.append(chunk.id)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(claimed) == 20
        assert len(set(claimed)) == 20
