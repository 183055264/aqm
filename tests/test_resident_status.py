# -*- coding: utf-8 -*-
"""Resident-session status tracking for the task member roster."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aqm.web.api import tasks as tasks_api
from aqm.web.api.tasks import create_tasks_router


def _clear(task_id: str) -> None:
    with tasks_api._resident_lock:
        tasks_api._resident_status.pop(task_id, None)


def test_resident_handler_starting_ready_failed():
    task_id = "T-RESIDENT"
    _clear(task_id)
    handler = tasks_api._resident_status_handler(task_id, {"a": None, "b": None})

    handler("starting", {"agents": ["a", "b"]})
    assert tasks_api._resident_status[task_id] == {
        "a": "starting",
        "b": "starting",
    }

    handler("ready", {"agent_id": "a", "name": "A", "runtime": "claude"})
    assert tasks_api._resident_status[task_id] == {
        "a": "ready",
        "b": "starting",
    }

    handler("failed", {"error": "boom"})
    assert tasks_api._resident_status[task_id] == {
        "a": "ready",
        "b": "failed",
    }
    _clear(task_id)


def test_resident_status_endpoint():
    root = Path(tempfile.mkdtemp(prefix="aqm-resident-"))
    (root / ".aqm").mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(create_tasks_router(root))
    client = TestClient(app)

    task_id = "T-RESIDENT-API"
    _clear(task_id)
    assert client.get(f"/api/tasks/{task_id}/residents").json() == {
        "id": task_id,
        "statuses": {},
    }

    handler = tasks_api._resident_status_handler(task_id, {"a": None})
    handler("starting", {"agents": ["a"]})
    handler("ready", {"agent_id": "a"})

    body = client.get(f"/api/tasks/{task_id}/residents").json()
    assert body["statuses"] == {"a": "ready"}
    _clear(task_id)
