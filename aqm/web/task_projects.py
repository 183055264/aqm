"""Locate Web tasks across the user's recent aqm projects."""

from __future__ import annotations

import logging
from pathlib import Path

from aqm.core.config import get_recent_projects
from aqm.core.project import get_db_path
from aqm.core.task import Task
from aqm.queue.sqlite import SQLiteQueue

logger = logging.getLogger(__name__)


def project_roots(current_root: Path) -> list[Path]:
    roots = [current_root.resolve()]
    for root in get_recent_projects():
        resolved = root.resolve()
        if resolved not in roots and (resolved / ".aqm").is_dir():
            roots.append(resolved)
    return roots


def find_task(current_root: Path, task_id: str) -> tuple[Task, Path] | None:
    for root in project_roots(current_root):
        db_path = get_db_path(root)
        if not db_path.exists():
            continue
        queue = SQLiteQueue(db_path)
        try:
            task = queue.get(task_id)
        finally:
            queue.close()
        if task is not None:
            task.metadata.setdefault("project_dir", str(root))
            return task, root
    return None


def list_project_tasks(current_root: Path) -> list[Task]:
    tasks: list[Task] = []
    for root in project_roots(current_root):
        db_path = get_db_path(root)
        if not db_path.exists():
            continue
        queue = SQLiteQueue(db_path)
        try:
            project_tasks = queue.list_tasks()
        except Exception:
            logger.exception("Could not list tasks from project %s", root)
            continue
        finally:
            queue.close()
        for task in project_tasks:
            task.metadata.setdefault("project_dir", str(root))
            tasks.append(task)
    tasks.sort(key=lambda task: task.updated_at, reverse=True)
    return tasks
