# -*- coding: utf-8 -*-
"""Task artifact collection (files written during a run)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aqm.core.artifacts import (
    collect_task_files,
    describe_artifact,
    format_size,
    task_run_started_at,
)
from aqm.core.project import get_db_path
from aqm.core.task import Task
from aqm.queue.sqlite import SQLiteQueue
from aqm.web.api.tasks import create_tasks_router


def _make_task(root, *, started_seconds_ago=5):
    return Task(
        description="artifact test",
        metadata={
            "project_dir": str(root),
            "run_started_at": (
                datetime.now(timezone.utc)
                - timedelta(seconds=started_seconds_ago)
            ).isoformat(),
        },
    )


def _age(path, seconds):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_collects_files_written_during_run(tmp_path):
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    old = root / "docs" / "old.md"
    old.write_text("old", encoding="utf-8")
    _age(old, 600)

    task = _make_task(root, started_seconds_ago=5)
    new = root / "docs" / "report.md"
    new.write_text("hello", encoding="utf-8")

    files = collect_task_files(root, task)

    paths = [f["path"] for f in files]
    assert paths == ["docs/report.md"]
    assert files[0]["size"] == 5
    assert files[0]["mtime"].endswith("+00:00")
    assert "description" in files[0]


def test_ignores_internal_and_dependency_dirs(tmp_path):
    root = tmp_path / "project"
    for sub in (".aqm", ".git", "node_modules", "__pycache__"):
        (root / sub).mkdir(parents=True)
        (root / sub / "written.txt").write_text("x", encoding="utf-8")
    (root / "kept.txt").write_text("x", encoding="utf-8")

    task = _make_task(root)
    paths = [f["path"] for f in collect_task_files(root, task)]

    assert paths == ["kept.txt"]


def test_sorted_by_mtime_desc(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    task = _make_task(root, started_seconds_ago=10)
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_text("1", encoding="utf-8")
    _age(first, 8)
    second.write_text("2", encoding="utf-8")
    _age(second, 1)

    paths = [f["path"] for f in collect_task_files(root, task)]

    assert paths == ["second.txt", "first.txt"]


def test_falls_back_to_earliest_stage_start(tmp_path):
    from aqm.core.task import StageRecord

    root = tmp_path / "project"
    root.mkdir()
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    task = Task(
        description="fallback",
        metadata={"project_dir": str(root)},
        stages=[StageRecord(stage_number=1, agent_id="a", started_at=started)],
    )
    (root / "stage.txt").write_text("x", encoding="utf-8")

    assert task_run_started_at(task) == started
    assert [f["path"] for f in collect_task_files(root, task)] == ["stage.txt"]


def test_missing_or_invalid_root_is_empty(tmp_path):
    task = _make_task(tmp_path)
    assert collect_task_files("", task) == []
    assert collect_task_files(tmp_path / "missing", task) == []


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"


# ── Descriptions ────────────────────────────────────────────────────────


def test_describe_markdown_uses_heading_and_intro(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(
        "# 回测报告\n\n本报告汇总 30 红 + 10 蓝规则的命中情况。\n\n## 附录\n",
        encoding="utf-8",
    )

    desc = describe_artifact(path, path.stat().st_size)

    assert desc.startswith("Markdown 文档")
    assert "回测报告" in desc
    assert "30 红 + 10 蓝" in desc


def test_describe_python_uses_docstring(tmp_path):
    path = tmp_path / "build.py"
    path.write_text(
        '"""一键构建: 运行全部计算并生成文档。"""\n\nprint(1)\n',
        encoding="utf-8",
    )

    desc = describe_artifact(path, path.stat().st_size)

    assert desc == "Python 脚本 · 一键构建: 运行全部计算并生成文档。"


def test_describe_json_lists_keys(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(
        '{"sample": 1, "red_metrics": [], "blue_metrics": []}',
        encoding="utf-8",
    )

    desc = describe_artifact(path, path.stat().st_size)

    assert desc.startswith("JSON 数据")
    assert "sample" in desc
    assert "red_metrics" in desc


def test_describe_csv_reports_columns(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("issue,red,blue\n1,2,3\n4,5,6\n", encoding="utf-8")

    desc = describe_artifact(path, path.stat().st_size)

    assert desc.startswith("CSV 表格")
    assert "3 列" in desc


def test_describe_yaml_lists_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("name: demo\nsteps:\n  - one\n", encoding="utf-8")

    desc = describe_artifact(path, path.stat().st_size)

    assert desc.startswith("YAML 配置")
    assert "name" in desc


def test_describe_binary_and_empty(tmp_path):
    binary = tmp_path / "image.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    assert describe_artifact(binary, binary.stat().st_size) == "图片"

    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert describe_artifact(empty, 0) == "空文件"


def test_describe_large_structured_file_does_not_parse(tmp_path):
    path = tmp_path / "huge.json"
    path.write_text("{}", encoding="utf-8")

    desc = describe_artifact(path, 5_000_000)

    assert desc.startswith("JSON 数据")
    assert "文件较大" in desc



def test_files_api_returns_task_artifacts(tmp_path):
    root = tmp_path / "project"
    (root / ".aqm").mkdir(parents=True)
    task = _make_task(root)
    (root / "out.md").write_text("artifact", encoding="utf-8")

    queue = SQLiteQueue(get_db_path(root))
    queue.push(task, "agent")
    queue.close()

    app = FastAPI()
    app.include_router(create_tasks_router(root))
    client = TestClient(app)

    response = client.get(f"/api/tasks/{task.id}/files")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task.id
    assert [f["path"] for f in body["files"]] == ["out.md"]

    missing = client.get("/api/tasks/T-DOES-NOT-EXIST/files")
    assert missing.status_code == 404
