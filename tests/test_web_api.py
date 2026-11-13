"""Web API tests — FastAPI TestClient for all endpoints.

Tests all REST API endpoints for tasks, pipelines, chunks, agents,
registry, validation, and SSE.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from aqm.core.agent import AgentDefinition
from aqm.core.project import init_project, save_pipeline, set_default_pipeline
from aqm.core.task import Task, TaskStatus
from aqm.queue.file import FileQueue


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path):
    """Create a temporary aqm project with a default pipeline."""
    root = init_project(tmp_path)
    yaml_content = yaml.dump({
        "agents": [
            {
                "id": "writer",
                "name": "Writer",
                "runtime": "claude",
                "system_prompt": "Write: {{ input }}",
                "handoffs": [{"to": "reviewer"}],
            },
            {
                "id": "reviewer",
                "name": "Reviewer",
                "runtime": "claude",
                "system_prompt": "Review: {{ input }}",
            },
        ]
    })
    save_pipeline(root, "default", yaml_content)
    set_default_pipeline(root, "default")
    return root


@pytest.fixture
def client(project):
    """FastAPI TestClient for the aqm web app."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi not installed")
    from aqm.web.app import create_app
    app = create_app(project)
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════
# HTML PAGES
# ═══════════════════════════════════════════════════════════════════════


class TestHTMLPages:

    def test_dashboard(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Dashboard" in r.text
        assert "sidebar" in r.text  # new sidebar layout

    def test_agents_page(self, client):
        r = client.get("/agents")
        assert r.status_code == 200
        assert "Agent Pipeline" in r.text

    def test_pipelines_page(self, client):
        r = client.get("/pipelines")
        assert r.status_code == 200
        assert "Pipelines" in r.text
        assert "default" in r.text

    def test_pipelines_edit(self, client):
        r = client.get("/pipelines?edit=default")
        assert r.status_code == 200
        assert "Editing: default" in r.text
        assert "yamlEditor" in r.text

    def test_task_detail(self, client):
        # Create a task first
        r = client.post("/api/tasks", json={"description": "Test task"})
        assert r.status_code == 200
        task_id = r.json()["id"]
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        assert task_id in r.text
        assert "breadcrumb" in r.text  # breadcrumbs
        # Work-block panel: owner picker + per-block start button.
        assert "chunk-owner-select" in r.text
        assert "开始处理" in r.text
        # Tab radios live outside .tabs (CSS-only sibling switching), so they
        # need the global input[name="tab"] rule to stay invisible.
        assert 'input[name="tab"] { display:none; }' in r.text

    def test_task_detail_not_found(self, client):
        r = client.get("/tasks/T-NONEXISTENT")
        assert r.status_code == 404

    def test_live_page_forces_output_tab_for_approval(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "等待审批"})
        task_id = r.json()["id"]
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.awaiting_gate
            queue.update(task)
        finally:
            queue.close()
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        # Approval happens on the live-output tab, and a stale #overview hash
        # must not drag the user back to the overview page.
        assert "FORCE_OUTPUT = true" in r.text
        assert "gate-reason" in r.text
        assert 'location.hash = \'output\'' in r.text
        # Interjections are submitted with Enter (Shift+Enter = newline).
        assert "Enter 发送，Shift+Enter 换行" in r.text

    def test_chunk_start_jumps_to_live_output(self, client):
        r = client.post("/api/tasks", json={"description": "启动工作块"})
        task_id = r.json()["id"]
        page = client.get(f"/tasks/{task_id}")
        assert page.status_code == 200
        assert "waitForTaskActive" in page.text
        assert "location.hash = 'output'" in page.text

    def test_timeline_hides_interjections_from_previous_run(self, client):
        from datetime import datetime, timedelta, timezone

        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "重启后时间线"})
        task_id = r.json()["id"]
        now = datetime.now(timezone.utc)
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.metadata["run_started_at"] = now.isoformat()
            task.metadata["interjection_log"] = [
                {
                    "text": "旧插话-重启前",
                    "agent_id": "writer",
                    "stage_number": 1,
                    "at": (now - timedelta(hours=1)).isoformat(),
                },
                {
                    "text": "新插话-本次运行",
                    "agent_id": "analyst",
                    "stage_number": 1,
                    "at": (now + timedelta(minutes=1)).isoformat(),
                },
            ]
            queue.update(task)
        finally:
            queue.close()

        page = client.get(f"/tasks/{task_id}")
        assert page.status_code == 200
        assert "旧插话-重启前" not in page.text
        assert "新插话-本次运行" in page.text

    def test_task_detail_stalled_shows_confirm_box(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "需要确认"})
        task_id = r.json()["id"]
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.stalled
            task.metadata["stall_reason"] = "等待人工确认"
            queue.update(task)
        finally:
            queue.close()
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        # A prominent reply box lives at the top of the overview tab.
        assert "人工确认 / 补充说明" in r.text
        assert "confirmNote" in r.text
        assert "带说明重启" in r.text
        assert "submitConfirm" in r.text

    def test_registry_page(self, client):
        r = client.get("/registry")
        assert r.status_code == 200
        assert "Registry" in r.text

    def test_validate_page(self, client):
        r = client.get("/validate")
        assert r.status_code == 200
        assert "校验流水线" in r.text
        assert "pipelineSelect" in r.text

    def test_sidebar_navigation_links(self, client):
        r = client.get("/")
        assert 'href="/"' in r.text
        assert 'href="/agents"' in r.text
        assert 'href="/pipelines"' in r.text
        assert 'href="/registry"' in r.text
        assert 'href="/validate"' in r.text

    def test_theme_toggle_present(self, client):
        r = client.get("/")
        assert "toggleTheme" in r.text
        assert "data-theme" in r.text or "aqm-theme" in r.text

    def test_theme_toggle_button(self, client):
        r = client.get("/")
        assert "toggleTheme" in r.text


# ═══════════════════════════════════════════════════════════════════════
# TASK API
# ═══════════════════════════════════════════════════════════════════════


class TestTaskAPI:

    def test_recent_project_tasks_can_be_viewed_and_restarted_from_their_project(self, project, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from aqm.core.project import get_db_path, init_project
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.app import create_app

        remote_root = tmp_path / "remote-project"
        remote_root.mkdir()
        remote_project = init_project(remote_root)
        task = Task(
            description="Remote project task",
            status=TaskStatus.failed,
            metadata={"project_dir": str(remote_project), "pipeline": "default"},
        )
        remote_queue = SQLiteQueue(get_db_path(remote_project))
        remote_queue.push(task, queue_name="writer")
        remote_queue.close()
        monkeypatch.setattr(
            "aqm.web.task_projects.get_recent_projects",
            lambda: [remote_project],
        )

        client = TestClient(create_app(project))

        task_list = client.get("/tasks")
        assert task.id in task_list.text
        assert "remote-project" in task_list.text
        assert "disabled" in task_list.text

        detail = client.get(f"/tasks/{task.id}")
        assert detail.status_code == 200
        assert str(remote_project) in detail.text
        assert "restartTaskInProject" in detail.text

        task_response = client.get(f"/api/tasks/{task.id}")
        assert task_response.status_code == 200
        assert task_response.json()["metadata"]["project_dir"] == str(remote_project)

        import aqm.web.api.tasks as tasks_api

        thread_args = []

        class CapturedThread:
            def __init__(self, target, args, daemon):
                thread_args.append(args)

            def start(self):
                pass

        monkeypatch.setattr(tasks_api.threading, "Thread", CapturedThread)
        router = tasks_api.create_tasks_router(project)
        restart_route = next(
            route for route in router.routes
            if getattr(route, "path", "") == "/api/tasks/{task_id}/restart"
        )
        response = asyncio.run(restart_route.endpoint(task.id, None))
        assert response["status"] == "restarting"
        assert thread_args[0][0] == remote_project

    def test_create_task(self, client):
        r = client.post("/api/tasks", json={"description": "Test"})
        assert r.status_code == 200
        assert "id" in r.json()

    def test_interject_echoes_client_id_and_persists(self, project):
        from fastapi.testclient import TestClient

        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.app import create_app

        queue = SQLiteQueue(get_db_path(project))
        task = Task(description="interject echo", status=TaskStatus.in_progress)
        queue.push(task, queue_name="writer")
        queue.close()

        client = TestClient(create_app(project))
        response = client.post(
            f"/api/tasks/{task.id}/interject",
            json={"text": "全部使用中文回复", "client_id": "ij-test-1"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["client_id"] == "ij-test-1"

        detail = client.get(f"/api/tasks/{task.id}").json()
        assert "全部使用中文回复" in detail["metadata"]["pending_interjections"]

    def test_background_exception_marks_task_failed(self, project):
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import _persist_task_failure

        queue = SQLiteQueue(get_db_path(project))
        task = Task(description="Background failure", status=TaskStatus.in_progress)
        queue.push(task, queue_name="writer")
        queue.close()

        _persist_task_failure(project, task.id, RuntimeError("agent startup failed"))

        queue = SQLiteQueue(get_db_path(project))
        try:
            failed_task = queue.get(task.id)
            assert failed_task.status == TaskStatus.failed
            assert failed_task.metadata["error"] == "agent startup failed"
        finally:
            queue.close()

    def test_list_tasks(self, client):
        client.post("/api/tasks", json={"description": "Task 1"})
        client.post("/api/tasks", json={"description": "Task 2"})
        r = client.get("/api/tasks")
        assert r.status_code == 200
        assert len(r.json()) >= 2

    def test_list_tasks_with_filter(self, client):
        r = client.get("/api/tasks?status=completed")
        assert r.status_code == 200

    def test_get_task(self, client):
        r = client.post("/api/tasks", json={"description": "Detail test"})
        task_id = r.json()["id"]
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["id"] == task_id

    def test_get_task_not_found(self, client):
        r = client.get("/api/tasks/T-NONEXIST")
        assert r.status_code == 404

    def test_set_priority(self, client):
        r = client.post("/api/tasks", json={"description": "Prio test"})
        task_id = r.json()["id"]
        r = client.post(f"/api/tasks/{task_id}/priority", json={"priority": "high"})
        assert r.status_code == 200

    def test_cancel_task(self, client):
        r = client.post("/api/tasks", json={"description": "Cancel test"})
        task_id = r.json()["id"]
        r = client.post(f"/api/tasks/{task_id}/cancel", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cancel_with_reason(self, client):
        r = client.post("/api/tasks", json={"description": "Cancel reason"})
        task_id = r.json()["id"]
        r = client.post(f"/api/tasks/{task_id}/cancel", json={"reason": "No longer needed"})
        assert r.status_code == 200
        # Verify reason stored
        task = client.get(f"/api/tasks/{task_id}").json()
        assert task["metadata"].get("cancel_reason") == "No longer needed"

    def test_cancel_already_cancelled(self, client):
        r = client.post("/api/tasks", json={"description": "Double cancel"})
        task_id = r.json()["id"]
        client.post(f"/api/tasks/{task_id}/cancel", json={})
        r = client.post(f"/api/tasks/{task_id}/cancel", json={})
        assert r.status_code == 400

    def test_events_snapshot_reflects_running_stage(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import StageRecord, TaskStatus
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import task_snapshot

        r = client.post("/api/tasks", json={"description": "snapshot"})
        task_id = r.json()["id"]
        root = client.app.state.project_root
        queue = SQLiteQueue(get_db_path(root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            task.current_agent_id = "writer"
            task.add_stage(
                StageRecord(
                    stage_number=1,
                    agent_id="reviewer",
                    input_text="done",
                    output_text="ok",
                )
            )
            task.metadata["_checkpoint_stage"] = 2
            task.metadata["_checkpoint_agent_id"] = "writer"
            task.metadata["_stage_started_at"] = "2026-01-01T00:00:00+00:00"
            task.metadata["paused"] = True
            queue.update(task)
        finally:
            queue.close()

        snap = task_snapshot(root, task_id)
        assert snap["status"] == "in_progress"
        assert snap["agent_id"] == "writer"
        assert snap["stage_number"] == 2
        assert snap["stage_running"] is True
        assert snap["started_at"] == "2026-01-01T00:00:00+00:00"
        assert snap["paused"] is True
        assert snap["stages"] == 1

    def test_events_snapshot_hides_finished_stage(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import StageRecord, TaskStatus
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import task_snapshot

        r = client.post("/api/tasks", json={"description": "snapshot done"})
        task_id = r.json()["id"]
        root = client.app.state.project_root
        queue = SQLiteQueue(get_db_path(root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            task.current_agent_id = "writer"
            task.add_stage(
                StageRecord(
                    stage_number=1,
                    agent_id="writer",
                    input_text="done",
                    output_text="ok",
                )
            )
            task.metadata["_checkpoint_stage"] = 1
            queue.update(task)
        finally:
            queue.close()

        snap = task_snapshot(root, task_id)
        assert snap["stage_running"] is False

    def test_paused_task_is_reported_as_paused_everywhere(self, client):
        """A paused task must not be shown as running on any page.

        The database keeps ``in_progress`` so the worker can block at a
        stage boundary, but the overview, task list, member roster and live
        output must all present one consistent "paused" state.
        """
        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "paused everywhere"})
        task_id = r.json()["id"]
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            task.current_agent_id = "writer"
            task.metadata["paused"] = True
            queue.update(task)
        finally:
            queue.close()

        tasks_html = client.get("/tasks").text
        row = next(
            line for line in tasks_html.splitlines()
            if f'data-id="{task_id}"' in line
        )
        assert 'data-status="paused"' in row
        assert "已暂停" in row
        assert "运行中" not in row
        assert '<option value="paused">已暂停</option>' in tasks_html

        dash = client.get("/").text
        recent = next(
            line for line in dash.splitlines() if f"/tasks/{task_id}" in line
        )
        assert "已暂停" in recent
        assert 'id="statPaused"' in dash
        assert 'id="statRunning"' in dash

        detail = client.get(f"/tasks/{task_id}").text
        assert "流水线已暂停" in detail
        assert (
            '<span id="taskStatusBadge">'
            '<span class="badge badge-paused">已暂停</span></span>'
        ) in detail
        assert "paused: '已暂停'" in detail
        assert "已暂停，等待继续" in detail
        assert "if (st === 'in_progress' && d.paused)" in detail

    def test_task_detail_status_badge_is_live(self, client):
        r = client.post("/api/tasks", json={"description": "badge"})
        task_id = r.json()["id"]
        html = client.get(f"/tasks/{task_id}").text
        assert 'id="taskStatusBadge"' in html
        assert "badge-paused" in html
        assert "renderTaskControl()" in html

    def test_pause_toggle_uses_server_state(self, client):
        """Pause/resume must not flip a local flag.

        The pause endpoint broadcasts ``task_paused`` before it replies, so a
        local toggle would cancel the SSE update and the button would look
        unchanged until a manual refresh (the exact bug users reported).
        """
        r = client.post("/api/tasks", json={"description": "toggle"})
        task_id = r.json()["id"]
        html = client.get(f"/tasks/{task_id}").text
        assert "TASK_PAUSED = !TASK_PAUSED" not in html
        assert "r.status === 'paused'" in html

    def test_pending_gate_spells_out_review_items(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import StageRecord, TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "gate items"})
        task_id = r.json()["id"]
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.awaiting_gate
            task.current_agent_id = "reviewer"
            task.add_stage(
                StageRecord(
                    stage_number=1,
                    agent_id="reviewer",
                    task_name="评审实现与统计口径",
                    input_text="impl",
                    output_text=(
                        "## 审核结论\n通过\n\n"
                        "- [ ] 统计口径是否全部使用 B 轨\n"
                    ),
                )
            )
            queue.update(task)
        finally:
            queue.close()

        r = client.get(f"/api/tasks/{task_id}/gate")
        assert r.status_code == 200
        gate = r.json()["gate"]
        assert gate["agent_id"] == "reviewer"
        assert gate["task_name"] == "评审实现与统计口径"
        assert gate["items"][0]["state"] == "conclusion"
        assert "通过" in gate["items"][0]["text"]
        assert any("B 轨" in item["text"] for item in gate["items"])

        html = client.get(f"/tasks/{task_id}").text
        assert "审批对象" in html
        assert "需要确认的事项" in html
        assert "决策后果" in html
        assert "统计口径是否全部使用 B 轨" in html

        # Leaving the gate state makes the endpoint refuse instead of lying.
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            queue.update(task)
        finally:
            queue.close()
        assert client.get(f"/api/tasks/{task_id}/gate").status_code == 409

    def test_chunk_start_forces_full_live_output_navigation(self, client):
        """Starting a chunk must render the live panel, not just change the hash.

        The chunks tab is usually open while the task is failed/completed, so
        the page was rendered without the live-output panel or SSE stream.
        ``location.assign('/tasks/<id>#output')`` is a same-document hash
        change in that situation: the output tab stays static until the user
        presses F5. The start handler must force a real navigation instead.
        """
        r = client.post("/api/tasks", json={"description": "chunk live nav"})
        task_id = r.json()["id"]
        html = client.get(f"/tasks/{task_id}").text

        assert "const LIVE_RENDERED" in html
        assert "function openLiveOutput" in html
        assert "view=live" in html
        # Only the helper itself may fall back to a hash-only navigation.
        assert html.count("window.location.assign(taskUrl + '#output')") == 1
        assert html.count("openLiveOutput(taskUrl)") >= 3

    def test_pause_and_unpause_task(self, client):
        r = client.post("/api/tasks", json={"description": "Pause test"})
        task_id = r.json()["id"]
        # A pending task is not active yet.
        r = client.post(f"/api/tasks/{task_id}/pause")
        assert r.status_code == 400

        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            queue.update(task)
        finally:
            queue.close()

        r = client.post(f"/api/tasks/{task_id}/pause")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"
        r = client.post(f"/api/tasks/{task_id}/pause")
        assert r.json().get("already") is True

        saved = client.get(f"/api/tasks/{task_id}").json()
        assert saved["metadata"].get("paused") is True

        r = client.post(f"/api/tasks/{task_id}/unpause")
        assert r.status_code == 200
        assert r.json()["was_paused"] is True
        saved = client.get(f"/api/tasks/{task_id}").json()
        assert not saved["metadata"].get("paused")

    def test_cancel_clears_pause(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "Stop paused"})
        task_id = r.json()["id"]
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            queue.update(task)
        finally:
            queue.close()
        client.post(f"/api/tasks/{task_id}/pause")
        r = client.post(f"/api/tasks/{task_id}/cancel", json={})
        assert r.status_code == 200
        saved = client.get(f"/api/tasks/{task_id}").json()
        assert not saved["metadata"].get("paused")

    def test_restart_task(self, client):
        r = client.post("/api/tasks", json={"description": "Restart test"})
        task_id = r.json()["id"]
        # Cancel first to make it restartable
        client.post(f"/api/tasks/{task_id}/cancel", json={})
        r = client.post(f"/api/tasks/{task_id}/restart", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "restarting"

    def test_restart_with_from_stage(self, client):
        r = client.post("/api/tasks", json={"description": "Restart stage"})
        task_id = r.json()["id"]
        client.post(f"/api/tasks/{task_id}/cancel", json={})
        r = client.post(f"/api/tasks/{task_id}/restart", json={"from_stage": 1})
        assert r.status_code == 200

    def test_restart_with_note_persists_directive(self, client, monkeypatch):
        import aqm.web.api.tasks as tasks_api

        monkeypatch.setattr(tasks_api, "_launch_restart", lambda *a, **k: None)
        r = client.post("/api/tasks", json={"description": "Restart note"})
        task_id = r.json()["id"]
        client.post(f"/api/tasks/{task_id}/cancel", json={})
        note = "确认只补文档，不要再修改 vba.txt"
        r = client.post(
            f"/api/tasks/{task_id}/restart", json={"note": note},
        )
        assert r.status_code == 200

        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            saved = queue.get(task_id)
        finally:
            queue.close()
        assert note in saved.metadata["interjections"]
        assert any(
            e["text"] == note for e in saved.metadata["interjection_log"]
        )


class TestMeetingAPI:

    def test_claude_directory_access_is_saved_and_returned(self, client):
        response = client.post("/api/meetings", json={
            "topic": "Claude directory access",
            "agents": [{
                "id": "claude",
                "name": "Claude",
                "runtime": "claude",
                "directory_access": "project_read_write",
            }],
        })

        assert response.status_code == 200
        meeting_id = response.json()["id"]
        assert response.json()["agents"][0]["directory_access"] == "project_read_write"
        restored = client.get(f"/api/meetings/{meeting_id}")
        assert restored.json()["agents"][0]["directory_access"] == "project_read_write"

    def test_directory_access_is_rejected_for_unsupported_runtime(self, client):
        response = client.post("/api/meetings", json={
            "topic": "Unsupported directory access",
            "agents": [{
                "id": "cline",
                "name": "Cline",
                "runtime": "cline",
                "directory_access": "project_read_write",
            }],
        })

        assert response.status_code == 422

    def test_claude_read_only_directory_access_is_rejected(self, client):
        response = client.post("/api/meetings", json={
            "topic": "Claude fixed directory access",
            "agents": [{
                "id": "claude",
                "name": "Claude",
                "runtime": "claude",
                "directory_access": "project_read_only",
            }],
        })

        assert response.status_code == 422

    def test_legacy_cline_provider_id_is_ignored(self, client):
        response = client.post("/api/meetings", json={
            "topic": "Cline provider test",
            "agents": [{
                "id": "cline",
                "name": "Cline",
                "runtime": "cline",
                "provider_id": "custom-provider",
                "model": "custom-model",
            }],
        })

        assert response.status_code == 200
        meeting_id = response.json()["id"]
        agent = response.json()["agents"][0]
        assert "provider_id" not in agent
        assert agent["model"] == "custom-model"

        restored = client.get(f"/api/meetings/{meeting_id}")
        assert "provider_id" not in restored.json()["agents"][0]


# ═══════════════════════════════════════════════════════════════════════
# CONTEXT API
# ═══════════════════════════════════════════════════════════════════════


class TestContextAPI:

    def test_get_context_not_found(self, client):
        r = client.get("/api/tasks/T-NONEXIST/context")
        assert r.status_code == 404

    def test_get_context(self, client, project):
        # Create a task and write context
        from aqm.core.project import get_tasks_dir
        from aqm.core.context_file import ContextFile
        r = client.post("/api/tasks", json={"description": "Context test"})
        task_id = r.json()["id"]
        task_dir = get_tasks_dir(project) / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        ctx = ContextFile(task_dir)
        ctx.append_stage(
            stage_number=1, agent_id="writer", task_name="write",
            status="completed", input_text="hello", output_text="world",
        )
        r = client.get(f"/api/tasks/{task_id}/context")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "stage 1" in r.text


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE API
# ═══════════════════════════════════════════════════════════════════════


class TestPipelineAPI:

    def test_list_pipelines(self, client):
        r = client.get("/api/pipelines")
        assert r.status_code == 200
        data = r.json()
        assert "pipelines" in data
        assert "default" in data
        assert any(p["name"] == "default" for p in data["pipelines"])

    def test_get_pipeline(self, client):
        r = client.get("/api/pipelines/default")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "default"
        assert "content" in data
        assert "agents" in data["content"]

    def test_get_pipeline_not_found(self, client):
        r = client.get("/api/pipelines/nonexistent")
        assert r.status_code == 404

    def test_create_pipeline(self, client):
        content = yaml.dump({"agents": [{"id": "a", "runtime": "claude", "system_prompt": "hi"}]})
        r = client.post("/api/pipelines", json={"name": "new-pipe", "content": content})
        assert r.status_code == 200
        assert r.json()["status"] == "created"
        # Verify it exists
        r = client.get("/api/pipelines/new-pipe")
        assert r.status_code == 200

    def test_create_duplicate_pipeline(self, client):
        r = client.post("/api/pipelines", json={"name": "default", "content": "agents: []"})
        assert r.status_code == 409

    def test_update_pipeline(self, client):
        new_content = yaml.dump({"agents": [{"id": "updated", "runtime": "claude", "system_prompt": "x"}]})
        r = client.put("/api/pipelines/default", json={"content": new_content})
        assert r.status_code == 200
        # Verify content updated
        r = client.get("/api/pipelines/default")
        assert "updated" in r.json()["content"]

    def test_update_nonexistent(self, client):
        r = client.put("/api/pipelines/nope", json={"content": "x"})
        assert r.status_code == 404

    def test_visual_agent_update_preserves_unedited_fields(self, client, project):
        config = {
            "agents": [{
                "id": "writer",
                "name": "Writer",
                "runtime": "claude",
                "model": "claude-sonnet",
                "system_prompt": "old prompt",
                "cli_flags": ["--allowedTools", "Read"],
                "mcp": [{"server": "filesystem", "args": ["."]}],
                "retry": {"max_retries": 2},
                "context_window": 7,
                "handoffs": [{"to": "reviewer"}],
                "gate": {"type": "human"},
            }, {
                "id": "reviewer",
                "runtime": "claude",
                "system_prompt": "review",
            }],
        }
        save_pipeline(project, "default", yaml.safe_dump(config))

        response = client.put(
            "/api/pipelines/default/agents/writer",
            json={
                "id": "writer",
                "name": "Updated writer",
                "runtime": "claude",
                "system_prompt": "new prompt",
                "handoffs": [],
                "gate": None,
            },
        )

        assert response.status_code == 200
        updated = yaml.safe_load(
            client.get("/api/pipelines/default").json()["content"]
        )["agents"][0]
        assert updated["name"] == "Updated writer"
        assert updated["system_prompt"] == "new prompt"
        assert updated["handoffs"] == []
        assert "gate" not in updated
        assert updated["model"] == "claude-sonnet"
        assert updated["cli_flags"] == ["--allowedTools", "Read"]
        assert updated["mcp"] == [{"server": "filesystem", "args": ["."]}]
        assert updated["retry"] == {"max_retries": 2}
        assert updated["context_window"] == 7

    def test_delete_pipeline(self, client):
        # Create a second pipeline first
        client.post("/api/pipelines", json={"name": "temp", "content": "agents: []"})
        r = client.delete("/api/pipelines/temp")
        assert r.status_code == 200

    def test_delete_last_pipeline_rejected(self, client):
        r = client.delete("/api/pipelines/default")
        assert r.status_code == 400
        assert "only pipeline" in r.json()["detail"].lower() or "Cannot delete" in r.json()["detail"]

    def test_duplicate_pipeline(self, client):
        r = client.post("/api/pipelines/default/duplicate", json={"new_name": "copy-of-default"})
        assert r.status_code == 200
        # Verify copy exists
        r = client.get("/api/pipelines/copy-of-default")
        assert r.status_code == 200

    def test_set_default_pipeline(self, client):
        client.post("/api/pipelines", json={"name": "alt", "content": "agents: []"})
        r = client.post("/api/pipelines/default", json={"name": "alt"})
        assert r.status_code == 200
        assert r.json()["default"] == "alt"

    def test_download_yaml(self, client):
        r = client.get("/api/pipelines/default/yaml")
        assert r.status_code == 200
        assert "text/yaml" in r.headers["content-type"]
        assert "attachment" in r.headers.get("content-disposition", "")


# ═══════════════════════════════════════════════════════════════════════
# AGENTS API
# ═══════════════════════════════════════════════════════════════════════


class TestAgentsAPI:

    def test_get_agents(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()
        assert len(agents) == 2
        ids = [a["id"] for a in agents]
        assert "writer" in ids
        assert "reviewer" in ids

    def test_get_agents_with_pipeline(self, client):
        r = client.get("/api/agents?pipeline=default")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_get_agents_fields(self, client):
        r = client.get("/api/agents")
        agent = r.json()[0]
        assert "id" in agent
        assert "name" in agent
        assert "runtime" in agent
        assert "handoffs" in agent
        assert "mcp" in agent


# ═══════════════════════════════════════════════════════════════════════
# CHUNKS API
# ═══════════════════════════════════════════════════════════════════════


class TestChunksAPI:

    def _create_task(self, client):
        r = client.post("/api/tasks", json={"description": "Chunk test"})
        return r.json()["id"]

    def test_list_chunks_empty(self, client):
        task_id = self._create_task(client)
        r = client.get(f"/api/tasks/{task_id}/chunks")
        assert r.status_code == 200
        assert r.json() == []

    def test_add_chunk(self, client):
        task_id = self._create_task(client)
        r = client.post(f"/api/tasks/{task_id}/chunks", json={"description": "Build login"})
        assert r.status_code == 200

    def test_add_and_list_chunks(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "Chunk A"})
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "Chunk B"})
        r = client.get(f"/api/tasks/{task_id}/chunks")
        assert len(r.json()) == 2

    def test_update_chunk_status(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "X"})
        chunks = client.get(f"/api/tasks/{task_id}/chunks").json()
        chunk_id = chunks[0]["id"]
        r = client.patch(f"/api/tasks/{task_id}/chunks/{chunk_id}", json={"status": "done"})
        assert r.status_code == 200

    def test_delete_chunk(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "Del"})
        chunks = client.get(f"/api/tasks/{task_id}/chunks").json()
        chunk_id = chunks[0]["id"]
        r = client.delete(f"/api/tasks/{task_id}/chunks/{chunk_id}")
        assert r.status_code == 200
        # Verify deleted
        chunks = client.get(f"/api/tasks/{task_id}/chunks").json()
        assert len(chunks) == 0

    def test_add_multiple_chunks_at_once(self, client):
        task_id = self._create_task(client)
        r = client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"descriptions": ["A", "B", "C"]},
        )
        assert r.status_code == 200
        assert len(r.json()["created"]) == 3
        assert len(client.get(f"/api/tasks/{task_id}/chunks").json()) == 3

    def test_add_chunk_with_owner_and_phase(self, client):
        task_id = self._create_task(client)
        r = client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"description": "写文档", "owner": "writer", "phase": "文档"},
        )
        data = r.json()
        assert data["owner"] == "writer"
        assert data["phase"] == "文档"
        assert data["source"] == "manual"

    def test_add_empty_description_rejected(self, client):
        task_id = self._create_task(client)
        r = client.post(f"/api/tasks/{task_id}/chunks", json={"description": "  "})
        assert r.status_code == 400

    def test_update_chunk_fields(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "X"})
        chunk_id = client.get(f"/api/tasks/{task_id}/chunks").json()[0]["id"]
        r = client.patch(
            f"/api/tasks/{task_id}/chunks/{chunk_id}",
            json={"description": "新描述", "owner": "engineer", "status": "blocked",
                  "note": "等待接口"},
        )
        assert r.status_code == 200
        chunk = r.json()["chunk"]
        assert chunk["description"] == "新描述"
        assert chunk["owner"] == "engineer"
        assert chunk["status"] == "blocked"
        assert chunk["note"] == "等待接口"

    def test_update_chunk_invalid_status(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "X"})
        chunk_id = client.get(f"/api/tasks/{task_id}/chunks").json()[0]["id"]
        r = client.patch(
            f"/api/tasks/{task_id}/chunks/{chunk_id}", json={"status": "nope"},
        )
        assert r.status_code == 400

    def test_update_missing_chunk(self, client):
        task_id = self._create_task(client)
        r = client.patch(
            f"/api/tasks/{task_id}/chunks/C-999", json={"status": "done"},
        )
        assert r.status_code == 404

    def test_reorder_chunks(self, client):
        task_id = self._create_task(client)
        client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"descriptions": ["A", "B", "C"]},
        )
        r = client.post(
            f"/api/tasks/{task_id}/chunks/reorder",
            json={"order": ["C-003", "C-001", "C-002"]},
        )
        assert r.status_code == 200
        ids = [c["id"] for c in client.get(f"/api/tasks/{task_id}/chunks").json()]
        assert ids == ["C-003", "C-001", "C-002"]

    def test_reset_chunks_keeps_done(self, client):
        task_id = self._create_task(client)
        client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"descriptions": ["A", "B"]},
        )
        client.patch(
            f"/api/tasks/{task_id}/chunks/C-001", json={"status": "done"},
        )
        client.patch(
            f"/api/tasks/{task_id}/chunks/C-002", json={"status": "blocked"},
        )
        r = client.post(
            f"/api/tasks/{task_id}/chunks/reset", json={"keep_done": True},
        )
        assert r.status_code == 200
        chunks = {c["id"]: c for c in client.get(
            f"/api/tasks/{task_id}/chunks").json()}
        assert chunks["C-001"]["status"] == "done"
        assert chunks["C-002"]["status"] == "pending"

    def test_clear_requires_confirm(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "A"})
        assert client.delete(f"/api/tasks/{task_id}/chunks").status_code == 400
        assert client.delete(
            f"/api/tasks/{task_id}/chunks?confirm=1").status_code == 200
        assert client.get(f"/api/tasks/{task_id}/chunks").json() == []

    def test_summary_endpoint(self, client):
        task_id = self._create_task(client)
        client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"descriptions": ["A", "B"]},
        )
        client.patch(
            f"/api/tasks/{task_id}/chunks/C-001", json={"status": "done"},
        )
        data = client.get(f"/api/tasks/{task_id}/chunks/summary").json()
        assert data["progress"]["total"] == 2
        assert data["progress"]["done"] == 1
        assert data["progress"]["percent"] == 50

    def test_split_requires_inactive_task(self, client):
        task_id = self._create_task(client)
        # Simulate a running task.
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.task_projects import find_task
        root = client.app.state.project_root
        queue = SQLiteQueue(get_db_path(root))
        try:
            task, _ = find_task(root, task_id)
            task.status = __import__(
                "aqm.core.task", fromlist=["TaskStatus"]).TaskStatus.in_progress
            queue.update(task)
        finally:
            queue.close()
        r = client.post(f"/api/tasks/{task_id}/chunks/split", json={})
        assert r.status_code == 409

    def test_start_chunk_requires_owner(self, client):
        task_id = self._create_task(client)
        client.post(f"/api/tasks/{task_id}/chunks", json={"description": "写文档"})
        chunk_id = client.get(f"/api/tasks/{task_id}/chunks").json()[0]["id"]
        r = client.post(f"/api/tasks/{task_id}/chunks/{chunk_id}/start", json={})
        assert r.status_code == 400
        assert "负责人" in r.json()["detail"]

    def test_start_chunk_rejects_unknown_owner(self, client):
        task_id = self._create_task(client)
        client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"description": "写文档", "owner": "writer"},
        )
        chunk_id = client.get(f"/api/tasks/{task_id}/chunks").json()[0]["id"]
        r = client.post(
            f"/api/tasks/{task_id}/chunks/{chunk_id}/start",
            json={"owner": "nobody"},
        )
        assert r.status_code == 400
        assert "不在流水线" in r.json()["detail"]

    def test_start_chunk_missing_chunk(self, client):
        task_id = self._create_task(client)
        r = client.post(
            f"/api/tasks/{task_id}/chunks/C-999/start", json={"owner": "writer"},
        )
        assert r.status_code == 404

    def test_start_chunk_launches_restart_with_focus(
        self, client, monkeypatch,
    ):
        import aqm.web.api.tasks as tasks_api

        calls = []
        monkeypatch.setattr(
            tasks_api,
            "_launch_restart",
            lambda root, task_id, from_stage: calls.append(
                (str(root), task_id, from_stage)
            ),
        )
        task_id = self._create_task(client)
        client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"description": "写文档", "owner": "writer"},
        )
        chunk_id = client.get(f"/api/tasks/{task_id}/chunks").json()[0]["id"]

        r = client.post(
            f"/api/tasks/{task_id}/chunks/{chunk_id}/start",
            json={"owner": "writer"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "starting"
        assert data["owner"] == "writer"
        assert data["from_stage"] == 1
        assert calls and calls[0][1] == task_id

        # Focus metadata is persisted so the agent prompt can prioritise it.
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            saved = queue.get(task_id)
        finally:
            queue.close()
        assert saved.metadata["_chunk_focus"]["chunk_id"] == chunk_id
        chunk = client.get(f"/api/tasks/{task_id}/chunks").json()[0]
        assert chunk["owner"] == "writer"
        assert chunk["status"] == "pending"

    def test_start_chunk_rejects_running_task(self, client):
        task_id = self._create_task(client)
        client.post(
            f"/api/tasks/{task_id}/chunks",
            json={"description": "写文档", "owner": "writer"},
        )
        chunk_id = client.get(f"/api/tasks/{task_id}/chunks").json()[0]["id"]
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.task_projects import find_task
        root = client.app.state.project_root
        queue = SQLiteQueue(get_db_path(root))
        try:
            task, _ = find_task(root, task_id)
            task.status = __import__(
                "aqm.core.task", fromlist=["TaskStatus"]).TaskStatus.in_progress
            queue.update(task)
        finally:
            queue.close()
        r = client.post(
            f"/api/tasks/{task_id}/chunks/{chunk_id}/start",
            json={"owner": "writer"},
        )
        assert r.status_code == 409


# ═══════════════════════════════════════════════════════════════════════
# VALIDATE API
# ═══════════════════════════════════════════════════════════════════════


class TestValidateAPI:

    def test_validate_valid_yaml(self, client):
        r = client.post("/api/validate")
        assert r.status_code == 200
        # Default pipeline should be valid
        data = r.json()
        assert data.get("valid") is True or "agent_count" in data

    def test_validate_with_content(self, client):
        yaml_content = yaml.dump({
            "agents": [{"id": "a", "name": "A", "runtime": "claude", "system_prompt": "{{ input }}"}]
        })
        r = client.post("/api/validate", json={"yaml_content": yaml_content})
        assert r.status_code == 200

    def test_validate_pipeline_listing(self, client):
        r = client.get("/api/validate/pipelines")
        assert r.status_code == 200
        data = r.json()
        names = [p["name"] for p in data["pipelines"]]
        assert "default" in names
        assert data["default"] in names

    def test_validate_named_pipeline(self, client):
        r = client.post("/api/validate", json={"pipeline": "default"})
        assert r.status_code == 200
        assert r.json()["pipeline"] == "default"

    def test_validate_unknown_pipeline_returns_404(self, client):
        r = client.post("/api/validate", json={"pipeline": "no-such-pipeline"})
        assert r.status_code == 404


class TestRegistrySearchQuery:
    """Chinese queries are expanded into registry terms before matching."""

    def test_chinese_query_expands_to_english_terms(self):
        from aqm.web.api.registry import _query_terms

        terms = _query_terms("代码审查")
        assert "code" in terms
        assert "review" in terms
        # The original query is always kept as a term
        assert "代码审查" in terms

    def test_alias_matching(self):
        from aqm.web.api.registry import _matches, _query_terms

        assert _matches("code-review-pipeline", "", ["code"], _query_terms("代码审查"))
        assert not _matches("blog-seo-pipeline", "", ["blog"], _query_terms("数据"))

    def test_empty_query_matches_everything(self):
        from aqm.web.api.registry import _matches, _query_terms

        assert _query_terms("") == []
        assert _matches("anything", "", [], [])


# ═══════════════════════════════════════════════════════════════════════
# AUTO-RESUME AFTER SERVER RESTART
# ═══════════════════════════════════════════════════════════════════════


class TestAutoResume:

    def test_cleanup_releases_stale_in_progress_chunks(self, client):
        from aqm.core.chunks import ChunkManager, ChunkStatus
        from aqm.core.project import get_tasks_dir
        from aqm.web.api.tasks import cleanup_stale_chunks

        r = client.post("/api/tasks", json={"description": "stale chunks"})
        task_id = r.json()["id"]
        root = client.app.state.project_root
        mgr = ChunkManager(get_tasks_dir(root) / task_id)
        mgr.add("A")
        mgr.add("B")
        mgr.start("C-001")
        mgr.start("C-002")

        cleaned = cleanup_stale_chunks(root)

        assert cleaned >= 2
        statuses = {c.id: c.status for c in mgr.load().chunks}
        assert statuses == {
            "C-001": ChunkStatus.pending,
            "C-002": ChunkStatus.pending,
        }

    def _stalled_task(self, client, reason: str = "Server restarted while task was running"):
        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue

        r = client.post("/api/tasks", json={"description": "自动恢复"})
        task_id = r.json()["id"]
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.stalled
            task.metadata["stall_reason"] = reason
            queue.update(task)
        finally:
            queue.close()
        return task_id

    def test_resumes_server_restart_stall(self, client):
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import _prepare_auto_resume

        task_id = self._stalled_task(client)
        ok, from_stage = _prepare_auto_resume(
            client.app.state.project_root, task_id
        )
        assert ok is True
        assert from_stage is None

        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            saved = queue.get(task_id)
        finally:
            queue.close()
        assert saved.metadata["_auto_resume_at"]

    def test_skips_manual_stall(self, client):
        from aqm.web.api.tasks import _prepare_auto_resume

        task_id = self._stalled_task(client, reason="等待人工确认")
        ok, _ = _prepare_auto_resume(client.app.state.project_root, task_id)
        assert ok is False

    def test_skips_running_task(self, client):
        from aqm.core.project import get_db_path
        from aqm.core.task import TaskStatus
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import _prepare_auto_resume

        task_id = self._stalled_task(client)
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.status = TaskStatus.in_progress
            queue.update(task)
        finally:
            queue.close()
        ok, _ = _prepare_auto_resume(client.app.state.project_root, task_id)
        assert ok is False

    def test_uses_checkpoint_stage(self, client):
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import _prepare_auto_resume

        task_id = self._stalled_task(client)
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.metadata["_checkpoint_stage"] = 1
            queue.update(task)
        finally:
            queue.close()
        ok, from_stage = _prepare_auto_resume(
            client.app.state.project_root, task_id
        )
        assert ok is True
        assert from_stage == 1

    def test_stops_after_repeated_attempts(self, client):
        from datetime import datetime, timezone

        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import (
            AUTO_RESUME_LIMIT,
            _prepare_auto_resume,
        )

        task_id = self._stalled_task(client)
        now = datetime.now(timezone.utc).isoformat()
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.metadata["_auto_resume_at"] = [now] * AUTO_RESUME_LIMIT
            task.metadata["_auto_resume_last_stage"] = 1
            queue.update(task)
        finally:
            queue.close()

        ok, _ = _prepare_auto_resume(client.app.state.project_root, task_id)
        assert ok is False
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            saved = queue.get(task_id)
        finally:
            queue.close()
        assert "自动恢复已尝试" in saved.metadata["stall_reason"]

    def test_progress_resets_attempt_budget(self, client):
        from datetime import datetime, timezone

        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue
        from aqm.web.api.tasks import AUTO_RESUME_LIMIT, _prepare_auto_resume

        task_id = self._stalled_task(client)
        now = datetime.now(timezone.utc).isoformat()
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.metadata["_auto_resume_at"] = [now] * AUTO_RESUME_LIMIT
            # The last attempts were at a different stage: progress resets them.
            task.metadata["_auto_resume_last_stage"] = 7
            queue.update(task)
        finally:
            queue.close()

        ok, from_stage = _prepare_auto_resume(
            client.app.state.project_root, task_id
        )
        assert ok is True
        assert from_stage is None

    def test_worker_restarts_once(self, client, monkeypatch):
        import aqm.web.api.tasks as tasks_api

        task_id = self._stalled_task(client)
        calls = []
        monkeypatch.setattr(
            tasks_api,
            "_restart_task_worker",
            lambda root, tid, stage, keep_pause=False: calls.append((tid, stage)),
        )
        tasks_api._auto_resume_worker(
            client.app.state.project_root, [task_id], 0,
        )
        assert calls == [(task_id, None)]

    def test_worker_keeps_paused_tasks_paused(self, client, monkeypatch):
        import aqm.web.api.tasks as tasks_api
        from aqm.core.project import get_db_path
        from aqm.queue.sqlite import SQLiteQueue

        task_id = self._stalled_task(client)
        queue = SQLiteQueue(get_db_path(client.app.state.project_root))
        try:
            task = queue.get(task_id)
            task.metadata["paused"] = True
            queue.update(task)
        finally:
            queue.close()

        calls = []
        monkeypatch.setattr(
            tasks_api,
            "_restart_task_worker",
            lambda root, tid, stage, keep_pause=False: calls.append(keep_pause),
        )
        tasks_api._auto_resume_worker(
            client.app.state.project_root, [task_id], 0,
        )
        assert calls == [True]
