"""Render every Web page and make sure its inline JavaScript still parses.

A Python-level escape bug (``"\n"`` written into a non-raw page string) turns
into a literal newline inside a JS string and silently breaks the whole
``<script>`` block: every handler on the page stops working.  ``node --check``
catches that class of regression, so run it over the rendered HTML.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from aqm.core.task import Task, TaskStatus
from aqm.web.pages.agents import render_agents
from aqm.web.pages.dashboard import render_dashboard
from aqm.web.pages.meeting import render_meeting
from aqm.web.pages.pipelines import render_pipelines
from aqm.web.pages.registry import render_registry
from aqm.web.pages.task_detail import render_task_detail
from aqm.web.pages.tasks import render_tasks
from aqm.web.pages.validate import render_validate

SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S)


def _pages() -> dict[str, str]:
    task = Task(description="演示任务", status=TaskStatus.completed)
    project_dir = str(Path(tempfile.gettempdir()))
    return {
        "dashboard": render_dashboard([task]),
        "tasks": render_tasks([task], {}, current_project_dir=project_dir),
        "meeting": render_meeting(),
        "agents": render_agents({}),
        "pipelines": render_pipelines([], "default"),
        "registry": render_registry(),
        "validate": render_validate(),
        "task_detail": render_task_detail(
            task, {}, "", current_project_dir=project_dir
        ),
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("page", sorted(_pages()))
def test_inline_scripts_parse(page: str) -> None:
    html = _pages()[page]
    blocks = [b for b in SCRIPT_RE.findall(html) if b.strip()]
    assert blocks, f"{page} has no inline script blocks"

    with tempfile.TemporaryDirectory(prefix="aqm-js-") as tmp:
        for index, source in enumerate(blocks):
            path = Path(tmp) / f"{page}_{index}.js"
            path.write_text(source, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert result.returncode == 0, (
                f"{page} script block {index} failed to parse:\n"
                f"{result.stderr}"
            )
