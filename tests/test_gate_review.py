"""Tests for the pending-gate review description."""

from __future__ import annotations

import pytest

from aqm.core.agent import AgentDefinition, GateConfig, Handoff
from aqm.core.chunks import ChunkManager
from aqm.core.gate_review import build_gate_review, extract_review_items
from aqm.core.project import get_tasks_dir, init_project
from aqm.core.task import StageRecord, Task, TaskStatus


REVIEWER_OUTPUT = """\
## 审核结论
通过

## 问题清单
- [ ] 统计口径是否全部使用 B 轨
- [x] 代码中是否存在 ROUND/fix 混用
- [ ] 产物是否注明路径

## 建议
补充单元测试。
"""


def test_extract_items_from_gate_prompt():
    prompt = """\
1. 口径是否为 B 轨
2. 是否保留回测脚本
"""
    items, source = extract_review_items(prompt, REVIEWER_OUTPUT)
    assert source == "gate_prompt"
    assert [i["text"] for i in items] == ["口径是否为 B 轨", "是否保留回测脚本"]
    assert all(i["state"] == "check" for i in items)


def test_extract_items_from_stage_output():
    items, source = extract_review_items("", REVIEWER_OUTPUT)
    assert source == "stage_output"
    assert items[0]["state"] == "conclusion"
    assert "审核结论" in items[0]["text"]
    assert "通过" in items[0]["text"]
    todos = [i for i in items if i["state"] == "todo"]
    dones = [i for i in items if i["state"] == "done"]
    assert [i["text"] for i in todos] == [
        "统计口径是否全部使用 B 轨",
        "产物是否注明路径",
    ]
    assert [i["text"] for i in dones] == ["代码中是否存在 ROUND/fix 混用"]


def test_extract_items_falls_back_to_sections():
    items, source = extract_review_items("", "## 结论\n通过\n\n## 风险\n无\n")
    assert source == "stage_output"
    assert items[0]["state"] == "conclusion"
    assert any(i["state"] == "section" and "风险" in i["text"] for i in items)


def test_extract_items_empty():
    items, source = extract_review_items("", "")
    assert items == []
    assert source == "none"


PROSE_OUTPUT = """复核结论：返工。 本轮独立复核完成，关键发现：
【本轮核心改动——蓝球 sl(10) 补全正确，可保留】
- vba.txt 第294行 sl(10)=he+lb-3；引擎 backtest_engine.py:174 已同步
- 规则表与定稿 vba.txt 仍有 13 条差异
【需要人工裁决】
1. sh(10) 四舍五入口径（13.14% vs 12.08%）
"""


def test_extract_items_from_prose_report():
    """Prose-only reports (no markdown headings) still yield useful items."""
    items, source = extract_review_items("", PROSE_OUTPUT)
    assert source == "stage_output"
    assert items[0]["state"] == "conclusion"
    assert "返工" in items[0]["text"]
    texts = [item["text"] for item in items]
    assert any("sl(10)" in text for text in texts)
    assert any("sh(10)" in text for text in texts)
    assert all(item["state"] != "conclusion" for item in items[1:])


@pytest.fixture
def gate_task(tmp_path):
    root = init_project(tmp_path)
    task = Task(description="gate review")
    task.status = TaskStatus.awaiting_gate
    task.current_agent_id = "reviewer"
    task.add_stage(
        StageRecord(
            stage_number=1,
            agent_id="reviewer",
            task_name="评审实现与统计口径",
            input_text="impl",
            output_text=REVIEWER_OUTPUT,
        )
    )
    manager = ChunkManager(get_tasks_dir(root) / task.id)
    chunk_list = manager.load()
    from aqm.core.chunks import Chunk, ChunkStatus

    chunk_list.chunks = [
        Chunk(id="C-001", description="实现红球规则", status=ChunkStatus.done),
        Chunk(
            id="C-002",
            description="修正蓝球重复规则",
            status=ChunkStatus.blocked,
            owner="engineer",
        ),
        Chunk(
            id="C-003",
            description="生成分级表",
            status=ChunkStatus.pending,
            owner="analyst",
        ),
    ]
    manager.save(chunk_list)
    return root, task


def test_build_gate_review_combines_context(gate_task):
    root, task = gate_task
    agents = {
        "reviewer": AgentDefinition(
            id="reviewer",
            name="评审与合规官",
            runtime="cline",
            gate=GateConfig(type="human"),
            handoffs=[
                Handoff(to="writer", condition="on_approve", task="撰写文档"),
                Handoff(to="engineer", condition="on_reject", task="返工"),
            ],
        ),
        "writer": AgentDefinition(id="writer", name="文档与交付", runtime="kilo"),
        "engineer": AgentDefinition(id="engineer", name="实现工程师", runtime="claude"),
    }
    review = build_gate_review(task, agents, root)

    assert review["stage_number"] == 1
    assert review["agent_id"] == "reviewer"
    assert review["agent_name"] == "评审与合规官"
    assert review["task_name"] == "评审实现与统计口径"
    assert review["items_source"] == "stage_output"
    assert review["chunks"]["total"] == 3
    assert review["chunks"]["done"] == 1
    assert review["chunks"]["pending_count"] == 2
    # Blocked chunks come first so the reviewer sees the real blocker.
    assert review["chunks"]["pending"][0]["id"] == "C-002"
    assert review["chunks"]["pending"][0]["status"] == "blocked"
    assert review["approve_to"] == [
        {"id": "writer", "name": "文档与交付", "task": "撰写文档"}
    ]
    assert review["reject_to"] == [
        {"id": "engineer", "name": "实现工程师", "task": "返工"}
    ]


def test_build_gate_review_without_stages():
    review = build_gate_review(Task(description="empty"), {}, None)
    assert review["stage_number"] is None
    assert review["items"] == []
    assert review["items_source"] == "none"
    assert review["chunks"]["total"] == 0
