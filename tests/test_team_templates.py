"""Tests for the global (project-independent) team templates."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aqm.core import team_templates as store


@pytest.fixture
def tpl_dir(tmp_path, monkeypatch):
    """Point the template library at a temporary directory."""
    base = tmp_path / "templates"
    base.mkdir()
    monkeypatch.setenv("AQM_TEMPLATES_DIR", str(base))
    return base


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".aqm" / "pipelines").mkdir(parents=True)
    (root / ".aqm" / "config.yaml").write_text("", encoding="utf-8")
    return root


# ═══════════════════════════════════════════════════════════════════════
# Core store
# ═══════════════════════════════════════════════════════════════════════


class TestTemplateStore:

    def test_default_root_is_installation_templates(self, monkeypatch):
        monkeypatch.delenv("AQM_TEMPLATES_DIR", raising=False)
        root = store.templates_root()
        assert root.name == "templates"
        # Independent of the current working directory / project.
        assert root.parent.name != ".aqm"

    def test_save_list_load_delete_roundtrip(self, tpl_dir):
        payload = store.save_template(
            "team-a",
            {
                "description": "alpha",
                "agents": [
                    {"id": "claude", "runtime": "claude", "role": "架构师"},
                    {"id": "kilo", "runtime": "kilo", "role": "审核"},
                ],
            },
        )
        assert payload["name"] == "team-a"
        assert (tpl_dir / "team-a.yaml").exists()

        items = store.list_templates()
        assert [i["name"] for i in items] == ["team-a"]
        assert items[0]["agent_count"] == 2

        loaded = store.load_template("team-a")
        assert loaded is not None
        assert loaded["description"] == "alpha"
        assert [a["id"] for a in loaded["agents"]] == ["claude", "kilo"]

        assert store.delete_template("team-a") is True
        assert store.load_template("team-a") is None
        assert store.delete_template("team-a") is False

    def test_empty_prompt_and_role_are_kept_but_resolved(self, tpl_dir):
        store.save_template(
            "skeleton",
            {"agents": [{"id": "claude", "runtime": "claude"}]},
        )
        loaded = store.load_template("skeleton")
        assert loaded["agents"][0]["system_prompt"] == ""

        resolved = store.resolve_agents(loaded)
        assert resolved[0]["role"] == "架构师"
        assert resolved[0]["system_prompt"].startswith("你是Claude")

    def test_explicit_prompt_wins_over_default(self, tpl_dir):
        store.save_template(
            "custom",
            {
                "agents": [
                    {
                        "id": "claude",
                        "runtime": "claude",
                        "role": "定制",
                        "system_prompt": "只回答一个词。",
                    }
                ]
            },
        )
        resolved = store.resolve_agents(store.load_template("custom"))
        assert resolved[0]["role"] == "定制"
        assert resolved[0]["system_prompt"] == "只回答一个词。"

    @pytest.mark.parametrize("bad", ["../evil", "a/b", "", "-x", "a" * 65])
    def test_rejects_unsafe_names(self, tpl_dir, bad):
        with pytest.raises(ValueError):
            store.validate_name(bad)

    def test_rejects_duplicate_member_ids(self, tpl_dir):
        with pytest.raises(ValueError):
            store.save_template(
                "dup",
                {
                    "agents": [
                        {"id": "a", "runtime": "claude"},
                        {"id": "a", "runtime": "kilo"},
                    ]
                },
            )

    def test_rejects_template_without_members(self, tpl_dir):
        with pytest.raises(ValueError):
            store.save_template("empty", {"agents": []})

    def test_unreadable_yaml_is_skipped(self, tpl_dir):
        (tpl_dir / "broken.yaml").write_text("agents: [unclosed", encoding="utf-8")
        store.save_template("ok", {"agents": [{"id": "x", "runtime": "claude"}]})
        assert [i["name"] for i in store.list_templates()] == ["ok"]

    def test_bundled_default_4_template_exists(self, monkeypatch):
        monkeypatch.delenv("AQM_TEMPLATES_DIR", raising=False)
        data = store.load_template("default-4")
        assert data is not None
        assert [a["runtime"] for a in data["agents"]] == [
            "claude", "opencode", "cline", "kilo",
        ]
        # Skeleton only: prompts stay empty so they resolve to defaults.
        assert all(a["system_prompt"] == "" for a in data["agents"])


class TestTaskSnapshot:

    def _agent(self, **kw):
        from aqm.core.agent import AgentDefinition

        return AgentDefinition(**kw)

    def test_start_pipeline_run_writes_snapshot(self, project, monkeypatch):
        """Creating a task freezes the pipeline members next to the task."""
        from aqm.core.project import save_pipeline, set_default_pipeline
        from aqm.web.api import tasks as tasks_api

        save_pipeline(
            project,
            "snap",
            """
name: snap
agents:
  - id: claude
    runtime: claude
    system_prompt: 只写架构。
    handoffs:
      - to: kilo
  - id: kilo
    runtime: kilo
    system_prompt: 只做审核。
    handoffs: []
""",
        )
        set_default_pipeline(project, "snap")

        class _NoThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                pass

        class _ThreadingShim:
            Thread = _NoThread

        monkeypatch.setattr(tasks_api, "threading", _ThreadingShim)

        task = tasks_api.start_pipeline_run(project, "写一个小工具")
        snapshot = project / ".aqm" / "tasks" / task.id / "team_snapshot.yaml"
        assert snapshot.exists()
        data = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
        by_id = {a["id"]: a for a in data["agents"]}
        assert set(by_id) == {"claude", "kilo"}
        assert by_id["claude"]["system_prompt"] == "只写架构。"
        assert by_id["kilo"]["system_prompt"] == "只做审核。"
        # Role is filled from the built-in defaults, not stored in the YAML.
        assert by_id["claude"]["role"] == "架构师"
        assert by_id["kilo"]["role"] == "审核"

    def test_snapshot_freezes_members(self, tmp_path):
        agents = {
            "claude": self._agent(id="claude", runtime="claude", system_prompt="P1"),
            "kilo": self._agent(id="kilo", runtime="kilo"),
        }
        path = store.write_task_snapshot(tmp_path / "T-1", agents)
        assert path is not None and path.exists()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        by_id = {a["id"]: a for a in data["agents"]}
        assert by_id["claude"]["system_prompt"] == "P1"
        assert by_id["claude"]["role"] == "架构师"
        # Empty prompt falls back to the runtime default.
        assert by_id["kilo"]["system_prompt"].startswith("你是kilo")
        assert by_id["kilo"]["role"] == "审核"

    def test_snapshot_skips_session_nodes(self, tmp_path):
        agents = {
            "s": self._agent(id="s", type="session", participants=["claude"]),
            "claude": self._agent(id="claude", runtime="claude"),
        }
        path = store.write_task_snapshot(tmp_path / "T-2", agents)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [a["id"] for a in data["agents"]] == ["claude"]

    def test_snapshot_none_when_no_agents(self, tmp_path):
        assert store.write_task_snapshot(tmp_path / "T-3", {}) is None


# ═══════════════════════════════════════════════════════════════════════
# Web API
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(project, tpl_dir):
    from fastapi.testclient import TestClient

    from aqm.web.app import create_app

    return TestClient(create_app(project))


class TestTeamTemplateApi:

    def test_list_includes_root_and_default(self, client, project):
        r = client.get("/api/team-templates")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["default"] == ""
        assert body["root"].endswith("templates")

    def test_crud_via_api(self, client):
        create = client.post(
            "/api/team-templates",
            json={
                "name": "api-team",
                "description": "from api",
                "agents": [
                    {"id": "claude", "runtime": "claude", "role": "架构师"},
                    {"id": "kilo", "runtime": "kilo", "role": "审核"},
                ],
            },
        )
        assert create.status_code == 200, create.text

        assert client.post(
            "/api/team-templates",
            json={"name": "api-team", "agents": [{"id": "a", "runtime": "claude"}]},
        ).status_code == 409

        got = client.get("/api/team-templates/api-team")
        assert got.status_code == 200
        assert got.json()["description"] == "from api"

        agents = client.get("/api/team-templates/api-team/agents").json()["agents"]
        assert agents[0]["role"] == "架构师"
        assert agents[0]["system_prompt"].startswith("你是Claude")

        upd = client.put(
            "/api/team-templates/api-team",
            json={
                "name": "api-team",
                "description": "changed",
                "agents": [{"id": "kilo", "runtime": "kilo", "system_prompt": "X"}],
            },
        )
        assert upd.status_code == 200
        assert client.get("/api/team-templates/api-team").json()["description"] == "changed"
        assert len(client.get("/api/team-templates").json()["items"]) == 1

        assert client.delete("/api/team-templates/api-team").status_code == 200
        assert client.get("/api/team-templates/api-team").status_code == 404
        assert client.delete("/api/team-templates/api-team").status_code == 404

    def test_update_missing_returns_404(self, client):
        r = client.put(
            "/api/team-templates/ghost",
            json={"name": "ghost", "agents": [{"id": "a", "runtime": "claude"}]},
        )
        assert r.status_code == 404

    def test_project_default_is_persisted(self, client, project):
        client.post(
            "/api/team-templates",
            json={"name": "picked", "agents": [{"id": "claude", "runtime": "claude"}]},
        )
        r = client.post("/api/team-templates/default", json={"name": "picked"})
        assert r.status_code == 200
        assert client.get("/api/team-templates").json()["default"] == "picked"

        from aqm.core.config import load_project_config

        assert load_project_config(project).team_template == "picked"

        # Clearing it works too.
        assert client.post("/api/team-templates/default", json={"name": ""}).status_code == 200
        assert load_project_config(project).team_template is None

    def test_set_default_rejects_unknown_template(self, client):
        r = client.post("/api/team-templates/default", json={"name": "nope"})
        assert r.status_code == 404

    def test_deleting_default_clears_project_pointer(self, client, project):
        from aqm.core.config import load_project_config

        client.post(
            "/api/team-templates",
            json={"name": "temp", "agents": [{"id": "claude", "runtime": "claude"}]},
        )
        client.post("/api/team-templates/default", json={"name": "temp"})
        assert load_project_config(project).team_template == "temp"

        assert client.delete("/api/team-templates/temp").status_code == 200
        assert load_project_config(project).team_template is None
        assert client.get("/api/team-templates").json()["default"] == ""

    def test_page_renders(self, client):
        r = client.get("/team-templates")
        assert r.status_code == 200
        assert "团队模板" in r.text
        assert "ttList" in r.text
