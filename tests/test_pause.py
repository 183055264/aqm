# -*- coding: utf-8 -*-
"""Pause / resume cooperatively stops a running task at the next boundary."""

from __future__ import annotations

import threading

from aqm.core import pipeline as pipeline_mod
from aqm.core.pipeline import (
    cancel_task,
    clear_pause,
    is_cancelled,
    is_paused,
    pause_task,
    resume_paused_task,
    wait_while_paused,
)


def _cleanup(task_id: str) -> None:
    clear_pause(task_id)
    with pipeline_mod._cancel_lock:
        pipeline_mod._cancelled_tasks.discard(task_id)


def test_pause_flag_round_trip():
    try:
        assert not is_paused("T-PAUSE-1")
        pause_task("T-PAUSE-1")
        assert is_paused("T-PAUSE-1")
        resume_paused_task("T-PAUSE-1")
        assert not is_paused("T-PAUSE-1")
    finally:
        _cleanup("T-PAUSE-1")


def test_paused_worker_blocks_until_resumed():
    done = threading.Event()

    def worker() -> None:
        wait_while_paused("T-PAUSE-2", poll=0.02)
        done.set()

    try:
        pause_task("T-PAUSE-2")
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert not done.wait(0.2), "worker should stay blocked while paused"

        resume_paused_task("T-PAUSE-2")
        assert done.wait(2.0), "worker should continue after resume"
    finally:
        _cleanup("T-PAUSE-2")


def test_cancel_releases_a_paused_worker():
    done = threading.Event()
    result: dict[str, bool] = {}

    def worker() -> None:
        result["continued"] = wait_while_paused("T-PAUSE-3", poll=0.02)
        done.set()

    try:
        pause_task("T-PAUSE-3")
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert not done.wait(0.2)

        cancel_task("T-PAUSE-3")
        assert done.wait(2.0), "cancel must wake a paused worker"
        assert result["continued"] is False
        assert is_cancelled("T-PAUSE-3")
        # Cancellation also drops the pause so a later restart starts clean.
        assert not is_paused("T-PAUSE-3")
    finally:
        _cleanup("T-PAUSE-3")
