import json
import sys
import threading
import time

import aqm.meeting as meeting_mod
import aqm.runtime.claude_config as claude_config
import aqm.runtime.cline_config as cline_config
import aqm.runtime.meeting_session as meeting_session
from aqm.meeting import (
    Meeting,
    MeetingAgent,
    MeetingStatus,
    _run_meeting,
    submit_human_speech,
)
from aqm.runtime.meeting_session import (
    _ACPMeetingSession,
    _ClaudeStreamSession,
    _CodexAppServerSession,
    _JsonRpcProcess,
    _Terminal,
    _build_meeting_command,
)


def _python_process(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def test_acp_session_process_is_reused_between_prompts(tmp_path):
    script = r"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1}
    elif method == "session/new":
        result = {"sessionId": "resident-session"}
    elif method == "session/prompt":
        text = request["params"]["prompt"][0]["text"]
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "resident-session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }), flush=True)
        result = {"stopReason": "end_turn"}
    else:
        result = {}
    if "id" in request:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""
    session = _ACPMeetingSession(_python_process(script), tmp_path, "")
    process_id = session._rpc.process.pid
    try:
        assert session.run("first", 3, threading.Event()) == "first"
        assert session.run("second", 3, threading.Event()) == "second"
        assert session._rpc.process.pid == process_id
        assert session._rpc.process.poll() is None
    finally:
        session.close()


def test_claude_stream_session_process_is_reused_between_prompts(tmp_path):
    script = r"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    text = request["message"]["content"][0]["text"]
    print(json.dumps({"type": "result", "result": text}), flush=True)
"""
    session = _ClaudeStreamSession(_python_process(script), tmp_path)
    process_id = session.process.pid
    try:
        assert session.run("first", 3, threading.Event()) == "first"
        assert session.run("second", 3, threading.Event()) == "second"
        assert session.process.pid == process_id
        assert session.process.poll() is None
    finally:
        session.close()


def test_codex_app_server_thread_is_reused_between_turns(tmp_path):
    script = r"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"serverInfo": {"name": "fake-codex", "version": "1"}}
    elif method == "thread/start":
        result = {"thread": {"id": "resident-thread"}}
    elif method == "turn/start":
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "item/agentMessage/delta",
            "params": {"delta": request["params"]["input"][0]["text"]},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-id", "status": "completed"}},
        }), flush=True)
        result = {"turn": {"id": "turn-id", "status": "inProgress"}}
    else:
        result = {}
    if "id" in request:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""
    session = _CodexAppServerSession(_python_process(script), tmp_path, "", "")
    process_id = session._rpc.process.pid
    try:
        assert session.run("first", 3, threading.Event()) == "first"
        assert session.run("second", 3, threading.Event()) == "second"
        assert session._rpc.process.pid == process_id
        assert session._rpc.process.poll() is None
    finally:
        session.close()


def test_terminal_output_and_exit_status_are_reported(tmp_path):
    terminal = _Terminal(
        tmp_path,
        {
            "command": sys.executable,
            "args": ["-c", "print('terminal output')"],
            "cwd": str(tmp_path),
            "outputByteLimit": 1024,
        },
    )
    try:
        assert terminal.finished.wait(timeout=3)
        result = terminal.read_output()
        assert result["output"].replace("\r\n", "\n") == "terminal output\n"
        assert result["truncated"] is False
        assert result["exitStatus"] == {"exitCode": 0, "signal": None}
    finally:
        terminal.close()


def test_all_meeting_runtimes_use_a_persistent_cli_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    expected_modes = {
        "claude": ["--input-format", "stream-json"],
        "codex": ["app-server", "--listen", "stdio://"],
        "gemini": ["--acp"],
        "opencode": ["acp"],
        "kilo": ["acp", "--cwd", str(tmp_path)],
        "cline": ["--json"],
    }

    for runtime, mode_args in expected_modes.items():
        agent = MeetingAgent(
            id=runtime,
            name=runtime,
            runtime=runtime,
            model="test-model",
        )
        command = _build_meeting_command(agent, tmp_path)
        assert command[0] == runtime
        for arg in mode_args:
            assert arg in command


def test_cline_meeting_uses_one_shot_cli_session(monkeypatch, tmp_path):
    # cline's ACP mode cannot use locally configured providers/models, so the
    # meeting keeps cline on the CLI that honours providers.json.
    settings_path = tmp_path / "providers.json"
    settings_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cline_config, "cline_provider_settings_path", lambda: settings_path)
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    agent = MeetingAgent(
        id="cline",
        name="cline",
        runtime="cline",
        model="sensenova-6.8-flash-lite",
    )

    session = meeting_session.start_meeting_agent_session(agent, tmp_path)

    assert isinstance(session, meeting_session._ClineCliMeetingSession)
    assert session.command[0] == "cline"
    assert "--acp" not in session.command
    assert session.command[session.command.index("-m") + 1] == "sensenova-6.8-flash-lite"
    # Meetings must replace cline's coding-agent prompt so a turn is a speech,
    # not a multi-minute repository-inspection loop.
    assert any(arg.startswith("-s=") for arg in session.command)


def test_cline_reasoning_is_captured_for_the_ui():
    turn = meeting_session._ClineTurn(None)
    collect = meeting_session._ClineCliMeetingSession._collect_reasoning

    def event(payload):
        return json.dumps({"type": "agent_event", "event": payload})

    collect(turn, event({"type": "content_start", "contentType": "reasoning", "reasoning": "先分析"}))
    collect(turn, event({"type": "content_start", "contentType": "reasoning", "reasoning": "再结论"}))
    # A text block ends the reasoning phase.
    collect(turn, event({"type": "content_start", "contentType": "text", "text": "大家好"}))
    collect(turn, event({"type": "content_start", "contentType": "reasoning", "reasoning": "补充一段"}))
    collect(turn, "not json")

    assert turn.thinking == ["先分析再结论"]
    assert "".join(turn.reasoning_stream) == "补充一段"


def test_agent_thinking_is_persisted(tmp_path):
    agent = MeetingAgent(
        id="a", name="A", runtime="claude", output="speech", thinking="思考内容"
    )
    meeting = Meeting(id="t", topic="t", agents=[agent])
    restored = Meeting.from_persist_dict(meeting.to_persist_dict())
    assert restored.agents[0].thinking == "思考内容"


def test_meeting_streamer_batches_chunks(monkeypatch):
    events = []
    monkeypatch.setattr(
        meeting_mod,
        "broadcast_meeting_event",
        lambda mid, ev, data: events.append((ev, data)),
    )
    agent = MeetingAgent(id="a", name="A", runtime="claude")
    meeting = Meeting(id="s", topic="s", agents=[agent])

    streamer = meeting_mod._MeetingStreamer(meeting, agent, 1)
    streamer.thinking("想")
    streamer.thinking("法")
    streamer.output("你")
    streamer.output("好")
    streamer.process("先看看文件")
    streamer.flush()

    streams = [data for ev, data in events if ev == "agent_stream"]
    thinking = "".join(d["chunk"] for d in streams if d["kind"] == "thinking")
    text = "".join(d["chunk"] for d in streams if d["kind"] == "text")
    process = "".join(d["chunk"] for d in streams if d["kind"] == "process")
    assert thinking == "想法"
    assert text == "你好"
    assert process.strip() == "先看看文件"
    # The answer body must be streamed before the narration that has to be
    # moved out of it, otherwise the frontend would clear the wrong text.
    kinds = [d["kind"] for d in streams]
    assert kinds.index("text") < kinds.index("process")
    assert all(d["agent_id"] == "a" and d["round"] == 1 for d in streams)


def test_json_rpc_timeout_is_idle_based(tmp_path):
    rpc = object.__new__(_JsonRpcProcess)
    rpc._lock = threading.Lock()
    rpc._next_id = 1
    rpc._pending = {}
    rpc._closed = threading.Event()
    rpc._stderr = __import__("collections").deque()
    rpc.last_message_at = time.monotonic()
    rpc._write = lambda message: None

    outcome = {}

    def worker():
        try:
            outcome["value"] = rpc.request("session/prompt", {}, timeout=1)
        except Exception as exc:  # pragma: no cover - failure path
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    # Keep the process "active" for longer than the timeout: this must not
    # abort, because chunks are still arriving.
    for _ in range(8):
        time.sleep(0.2)
        rpc.last_message_at = time.monotonic()
    with rpc._lock:
        pending = rpc._pending.pop(1)
    event, result = pending
    result.update({"result": {"ok": True}})
    event.set()
    thread.join(2)

    assert outcome.get("value") == {"ok": True}
    assert "error" not in outcome


def test_json_rpc_timeout_fires_when_runtime_goes_silent(tmp_path):
    rpc = object.__new__(_JsonRpcProcess)
    rpc._lock = threading.Lock()
    rpc._next_id = 1
    rpc._pending = {}
    rpc._closed = threading.Event()
    rpc._stderr = __import__("collections").deque()
    rpc.last_message_at = time.monotonic()
    rpc._write = lambda message: None

    started = time.monotonic()
    try:
        rpc.request("session/prompt", {}, timeout=0.3)
    except TimeoutError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("silent runtime should time out")
    assert time.monotonic() - started < 2


def test_streamer_keeps_streamed_text_for_empty_results(monkeypatch):
    events = []
    monkeypatch.setattr(
        meeting_mod,
        "broadcast_meeting_event",
        lambda mid, ev, data: events.append((ev, data)),
    )
    agent = MeetingAgent(id="a", name="A", runtime="kilo")
    meeting = Meeting(id="s", topic="s", agents=[agent])
    streamer = meeting_mod._MeetingStreamer(meeting, agent, 1)
    streamer.output("已经")
    streamer.output("流出的发言")
    streamer.flush()

    assert streamer.accumulated("text") == "已经流出的发言"


def test_acp_tool_call_moves_prior_text_out_of_the_speech(monkeypatch):
    from aqm.runtime import meeting_session

    session = object.__new__(meeting_session._ACPMeetingSession)
    session._turn_lock = threading.Lock()
    session._segment_parts = ["我先看一下 README。"]
    session._narration_parts = []
    session._seen_tools = set()
    session._failed_tools = set()
    session._action_lines = []
    process_chunks = []
    action_chunks = []
    session._process_callback = process_chunks.append
    session._action_callback = action_chunks.append

    session._emit_tool_action({
        "toolCallId": "c1",
        "status": "completed",
        "kind": "read",
        "locations": [{"path": "README.md"}],
    })

    assert process_chunks == ["我先看一下 README。"]
    assert session._narration_parts == ["我先看一下 README。"]
    assert session._segment_parts == []
    assert action_chunks and "README.md" in action_chunks[0]

    # A repeated update for the same call must not duplicate anything.
    session._emit_tool_action({
        "toolCallId": "c1",
        "status": "completed",
        "kind": "read",
        "locations": [{"path": "README.md"}],
    })
    assert process_chunks == ["我先看一下 README。"]
    assert len(action_chunks) == 1


def test_claude_meeting_uses_fixed_project_read_write_permission(monkeypatch, tmp_path):
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    agent = MeetingAgent(id="claude", name="Claude", runtime="claude")
    command = _build_meeting_command(agent, tmp_path)

    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in command


def test_legacy_read_only_claude_agent_is_upgraded_to_project_read_write():
    agent = MeetingAgent(
        id="claude",
        name="Claude",
        runtime="claude",
        directory_access="project_read_only",
    )
    assert agent.directory_access == "project_read_write"


def test_claude_meeting_trusts_project_directory_by_default(monkeypatch, tmp_path):
    agent = MeetingAgent(id="claude", name="Claude", runtime="claude")
    command = _build_meeting_command(agent, tmp_path)
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"


def test_claude_meeting_uses_configured_model_without_explicit_override(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"model":"agnes-3.0-flash","env":{"ANTHROPIC_MODEL":"agnes-3.0-flash"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_config, "_claude_settings_path", lambda: settings_path)
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    agent = MeetingAgent(
        id="claude",
        name="Claude",
        runtime="claude",
        model="agnes-3.0-flash",
    )

    command = _build_meeting_command(agent, tmp_path)

    assert "--model" not in command


def test_claude_meeting_passes_nondefault_selected_model(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model":"agnes-3.0-flash"}', encoding="utf-8")
    monkeypatch.setattr(claude_config, "_claude_settings_path", lambda: settings_path)
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    agent = MeetingAgent(
        id="claude",
        name="Claude",
        runtime="claude",
        model="other-model",
    )

    command = _build_meeting_command(agent, tmp_path)

    assert command[command.index("--model") + 1] == "other-model"


def test_trusting_claude_project_updates_only_that_project(monkeypatch, tmp_path):
    project_root = tmp_path / "workspace"
    (project_root / ".aqm").mkdir(parents=True)
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        '{"projects":{"C:/other":{"hasTrustDialogAccepted":true,"otherSetting":"keep"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_config, "_claude_user_config_path", lambda: config_path)

    claude_config.trust_claude_project(project_root)

    settings = json.loads(config_path.read_text(encoding="utf-8"))
    assert settings["projects"]["C:/other"] == {
        "hasTrustDialogAccepted": True,
        "otherSetting": "keep",
    }
    assert settings["projects"][str(project_root.resolve())]["hasTrustDialogAccepted"] is True


def test_claude_session_trusts_project_when_permission_is_configured(monkeypatch, tmp_path):
    project_root = tmp_path / "workspace"
    agent = MeetingAgent(
        id="claude",
        name="Claude",
        runtime="claude",
        directory_access="project_read_write",
    )
    trusted = []
    session = object()
    monkeypatch.setattr(meeting_session, "trust_claude_project", trusted.append)
    monkeypatch.setattr(meeting_session, "_build_meeting_command", lambda *_: ["claude"])
    monkeypatch.setattr(meeting_session, "_ClaudeStreamSession", lambda *args, **kwargs: session)

    started = meeting_session.start_meeting_agent_session(agent, project_root)

    assert started is session
    assert trusted == [project_root]


def test_claude_directory_trust_rejects_non_aqm_directory(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    monkeypatch.setattr(claude_config, "_claude_user_config_path", lambda: config_path)

    try:
        claude_config.trust_claude_project(tmp_path)
    except ValueError as exc:
        assert "not an AQM project" in str(exc)
    else:
        raise AssertionError("Expected unrecognized project directory to be rejected")
    assert not config_path.exists()


def test_legacy_meeting_provider_id_does_not_override_local_selection(monkeypatch, tmp_path):
    settings_path = tmp_path / "providers.json"
    settings_path.write_text(
        '{"lastUsedProvider":"sensenova","providers":{"sensenova":{"settings":{}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(cline_config, "cline_provider_settings_path", lambda: settings_path)
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    meeting = Meeting.from_persist_dict({
        "id": "provider-persistence",
        "topic": "provider persistence",
        "agents": [{
            "id": "cline",
            "name": "cline",
            "runtime": "cline",
            "provider_id": "custom-provider",
        }],
    })

    command = _build_meeting_command(meeting.agents[0], tmp_path)

    assert command[command.index("--provider") + 1] == "sensenova"


def test_cline_meeting_uses_last_selected_local_provider(monkeypatch, tmp_path):
    settings_path = tmp_path / "providers.json"
    settings_path.write_text(
        '{"lastUsedProvider":"sensenova","providers":{"sensenova":{"settings":{}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(cline_config, "cline_provider_settings_path", lambda: settings_path)
    monkeypatch.setattr("aqm.meeting._resolve_cmd", lambda executable: [executable])
    agent = MeetingAgent(id="cline", name="cline", runtime="cline")

    command = _build_meeting_command(agent, tmp_path)

    assert command[command.index("--provider") + 1] == "sensenova"


def test_meeting_starts_all_sessions_once_and_reuses_them(monkeypatch, tmp_path):
    agents = [
        MeetingAgent(id=name, name=name, runtime="claude", role=name)
        for name in ("one", "two", "three")
    ]
    meeting = Meeting(
        id="resident-test",
        topic="resident test",
        agents=agents,
        rounds=2,
        status=MeetingStatus.running.value,
    )
    barrier = threading.Barrier(len(agents))
    started = []
    sessions = []

    class FakeSession:
        def __init__(self, agent):
            self.agent = agent
            self.prompts = []
            self.closed = False

        @property
        def is_alive(self):
            return not self.closed

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            self.prompts.append(prompt)
            return f"{self.agent.name} turn {len(self.prompts)}"

        def close(self):
            self.closed = True

    def start(agent, project_root, stop_event):
        barrier.wait(timeout=3)
        started.append(agent.id)
        session = FakeSession(agent)
        sessions.append(session)
        return session

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start, raising=False)
    _run_meeting(meeting, tmp_path)

    assert sorted(started) == ["one", "three", "two"]
    assert all(len(session.prompts) == 2 for session in sessions)
    assert all(session.closed for session in sessions)
    assert all(not agent.resident for agent in agents)
    assert meeting.status == MeetingStatus.completed.value
    assert all(agent.rounds_completed == [1, 2] for agent in agents)
    sessions_by_agent = {session.agent.id: session for session in sessions}
    assert "【你的角色】" in sessions_by_agent["one"].prompts[0]
    assert "two turn 1" in sessions_by_agent["one"].prompts[1]
    assert "three turn 1" in sessions_by_agent["one"].prompts[1]
    assert "one turn 1" not in sessions_by_agent["one"].prompts[1]


def test_meeting_stops_after_a_round_with_no_successful_agents(monkeypatch, tmp_path):
    agents = [
        MeetingAgent(id="one", name="one", runtime="claude"),
        MeetingAgent(id="two", name="two", runtime="cline"),
    ]
    meeting = Meeting(
        id="all-failed",
        topic="all failed",
        agents=agents,
        rounds=5,
    )
    run_counts = {}

    class FailedSession:
        def __init__(self, agent):
            self.agent = agent

        @property
        def is_alive(self):
            return True

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            run_counts[self.agent.id] = run_counts.get(self.agent.id, 0) + 1
            raise RuntimeError("model request failed")

        def close(self):
            pass

    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda agent, project_root, stop_event: FailedSession(agent),
    )

    _run_meeting(meeting, tmp_path)

    assert meeting.status == MeetingStatus.error.value
    assert meeting.current_round == 1
    assert run_counts == {"one": 1, "two": 1}
    assert "提前停止" in meeting.error


def test_empty_agent_output_counts_as_failure_and_stops_meeting(monkeypatch, tmp_path):
    meeting = Meeting(
        id="empty-output-test",
        topic="empty output test",
        agents=[MeetingAgent(id="cline", name="Cline", runtime="cline")],
        rounds=5,
    )
    run_count = []

    class EmptySession:
        is_alive = True

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            run_count.append(prompt)
            return ""

        def close(self):
            pass

    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda agent, project_root, stop_event: EmptySession(),
    )

    _run_meeting(meeting, tmp_path)

    assert meeting.status == MeetingStatus.error.value
    assert meeting.current_round == 1
    assert len(run_count) == 1
    assert meeting.agents[0].status == "error"
    assert "without producing a response" in meeting.agents[0].error


def test_dead_resident_session_is_not_called_again(monkeypatch, tmp_path):
    meeting = Meeting(
        id="dead-session-test",
        topic="dead session test",
        agents=[MeetingAgent(id="claude", name="Claude", runtime="claude")],
        rounds=3,
    )
    run_count = []

    class ExitedSession:
        is_alive = False

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            run_count.append(prompt)
            return "should not run"

        def close(self):
            pass

    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda agent, project_root, stop_event: ExitedSession(),
    )

    _run_meeting(meeting, tmp_path)

    assert meeting.status == MeetingStatus.error.value
    assert meeting.current_round == 1
    assert run_count == []
    assert "session exited before this turn" in meeting.agents[0].error


def test_stop_during_round_does_not_mark_meeting_failed(monkeypatch, tmp_path):
    agents = [
        MeetingAgent(id="one", name="one", runtime="claude"),
        MeetingAgent(id="two", name="two", runtime="cline"),
    ]
    meeting = Meeting(
        id="stop-mid-round",
        topic="stop mid round",
        agents=agents,
        rounds=3,
        status=MeetingStatus.running.value,
    )
    events = []
    monkeypatch.setattr(
        meeting_mod,
        "broadcast_meeting_event",
        lambda mid, ev, data: events.append((ev, data)),
    )

    class StoppedSession:
        is_alive = True

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            stop_event.set()
            raise InterruptedError("stopped by user")

        def close(self):
            pass

    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda agent, project_root, stop_event: StoppedSession(),
    )

    _run_meeting(meeting, tmp_path)

    assert meeting.status == MeetingStatus.stopped.value
    assert "提前停止" not in meeting.error
    round_events = [d for ev, d in events if ev == "round_complete"]
    assert round_events
    assert round_events[-1]["interrupted"] is True
    assert round_events[-1]["completed_agents"] == 0


def test_round_summary_does_not_reuse_previous_round_errors(monkeypatch, tmp_path):
    agents = [
        MeetingAgent(id="ok", name="ok", runtime="claude"),
        MeetingAgent(id="bad", name="bad", runtime="kilo"),
    ]
    meeting = Meeting(
        id="round-errors",
        topic="round errors",
        agents=agents,
        rounds=2,
        status=MeetingStatus.running.value,
    )
    events = []
    monkeypatch.setattr(
        meeting_mod,
        "broadcast_meeting_event",
        lambda mid, ev, data: events.append((ev, data)),
    )

    class SometimesFailingSession:
        def __init__(self, agent):
            self.agent = agent
            self.calls = 0

        @property
        def is_alive(self):
            return True

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            self.calls += 1
            if self.agent.id == "bad" and self.calls == 1:
                raise RuntimeError("boom")
            return f"{self.agent.name} ok"

        def close(self):
            pass

    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda agent, project_root, stop_event: SometimesFailingSession(agent),
    )

    _run_meeting(meeting, tmp_path)

    summaries = [d for ev, d in events if ev == "round_complete"]
    assert [s["round"] for s in summaries] == [1, 2]
    assert summaries[0]["completed_agents"] == 1
    assert summaries[0]["error_agents"] == 1
    # The round-1 failure must not leak into the round-2 summary.
    assert summaries[1]["completed_agents"] == 2
    assert summaries[1]["error_agents"] == 0


def test_stale_run_does_not_touch_the_meeting(monkeypatch, tmp_path):
    meeting = Meeting(
        id="stale-run",
        topic="stale run",
        agents=[MeetingAgent(id="one", name="one", runtime="claude")],
        rounds=1,
        status=MeetingStatus.completed.value,
    )
    meeting._run_token = 5
    started = []
    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda *args, **kwargs: started.append(1),
    )

    _run_meeting(meeting, tmp_path, threading.Event(), run_token=4)

    assert started == []
    assert meeting.status == MeetingStatus.completed.value


def test_editing_a_meeting_overwrites_the_shared_header_in_place(tmp_path):
    meeting = Meeting(
        id="header-edit",
        topic="旧主题",
        description="旧描述",
        agents=[MeetingAgent(id="a", name="A", runtime="claude")],
        shared_file="meeting.md",
    )
    meeting_mod._sync_shared_header(meeting, tmp_path)
    shared = meeting_mod.get_shared_file_path(meeting, tmp_path)
    assert ".aqm" in shared.parts and "header-edit" in shared.parts
    with open(shared, "a", encoding="utf-8") as handle:
        handle.write("\n## 第 1 轮讨论\n\n### A (claude) - 第1轮\n\n已有发言正文\n\n")

    meeting_mod._meetings[meeting.id] = meeting
    try:
        meeting_mod.update_meeting(
            meeting.id,
            topic="新主题",
            description="新描述",
            project_root=tmp_path,
        )
    finally:
        meeting_mod._meetings.pop(meeting.id, None)

    text = shared.read_text(encoding="utf-8")
    assert text.count("# 新主题") == 1
    assert "旧主题" not in text
    assert "新描述" in text
    assert "旧描述" not in text
    # The discussion body is preserved, and no second header is appended.
    assert "已有发言正文" in text
    assert text.count("## 第 1 轮讨论") == 1


def test_shared_header_is_stable_when_synced_repeatedly(tmp_path):
    meeting = Meeting(
        id="header-stable",
        topic="主题",
        description="描述",
        agents=[MeetingAgent(id="a", name="A", runtime="claude")],
        shared_file="meeting.md",
    )
    meeting_mod._sync_shared_header(meeting, tmp_path)
    shared = meeting_mod.get_shared_file_path(meeting, tmp_path)
    with open(shared, "a", encoding="utf-8") as handle:
        handle.write("\n## 第 1 轮讨论\n\n正文\n\n")
    first = shared.read_text(encoding="utf-8")
    meeting_mod._sync_shared_header(meeting, tmp_path)
    normalized = shared.read_text(encoding="utf-8")
    assert normalized != ""
    # A second sync is a no-op: the header is replaced, never appended.
    meeting_mod._sync_shared_header(meeting, tmp_path)
    assert shared.read_text(encoding="utf-8") == normalized
    assert normalized.count("# 主题") == 1
    assert "正文" in normalized


def test_two_meetings_use_isolated_shared_files(tmp_path):
    first = Meeting(id="meeting-a", topic="A", shared_file="meeting.md")
    second = Meeting(id="meeting-b", topic="B", shared_file="meeting.md")

    path_a = meeting_mod.get_shared_file_path(first, tmp_path)
    path_b = meeting_mod.get_shared_file_path(second, tmp_path)

    assert path_a != path_b
    assert path_a.parent.name == "meeting-a"
    assert path_b.parent.name == "meeting-b"
    assert path_a.name == "meeting.md"


def test_absolute_shared_file_path_is_respected(tmp_path):
    absolute = tmp_path / "custom" / "discussion.md"
    meeting = Meeting(
        id="absolute-shared",
        topic="abs",
        shared_file=str(absolute),
    )
    assert meeting_mod.get_shared_file_path(meeting, tmp_path) == absolute


def test_legacy_root_shared_file_is_migrated_on_load(tmp_path):
    legacy = tmp_path / "meeting.md"
    legacy.write_text("# 旧会议\n\n## 第 1 轮讨论\n\n旧内容\n", encoding="utf-8")
    meeting_dir = tmp_path / ".aqm" / "meetings" / "legacy-meeting"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": "legacy-meeting",
                "topic": "旧会议",
                "shared_file": "meeting.md",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    meeting = meeting_mod._load_meeting_from_disk("legacy-meeting", tmp_path)

    assert meeting is not None
    target = meeting_mod.get_shared_file_path(meeting, tmp_path)
    assert target.exists()
    assert "旧内容" in target.read_text(encoding="utf-8")
    # The legacy file is copied, not moved: other old meetings may still use it.
    assert legacy.exists()


def test_human_host_speaks_before_agents(monkeypatch, tmp_path):
    agents = [
        MeetingAgent(id="host", name="我", runtime="human", role="主持人"),
        MeetingAgent(id="worker", name="Worker", runtime="claude"),
    ]
    meeting = Meeting(
        id="human-host",
        topic="human host",
        agents=agents,
        rounds=1,
        status=MeetingStatus.running.value,
    )
    prompts = []
    started = []

    class FakeSession:
        is_alive = True

        def run(self, prompt, timeout, stop_event, on_output=None, on_thinking=None, on_action=None, on_process=None):
            prompts.append(prompt)
            return "agent reply"

        def close(self):
            pass

    def start(agent, project_root, stop_event):
        started.append(agent.id)
        return FakeSession()

    monkeypatch.setattr(
        meeting_session, "start_meeting_agent_session", start, raising=False
    )
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    thread = threading.Thread(
        target=_run_meeting, args=(meeting, tmp_path), daemon=True
    )
    try:
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and meeting._waiting_human != "host":
            time.sleep(0.02)
        assert meeting._waiting_human == "host"
        assert started == ["worker"]

        result = submit_human_speech(
            meeting.id, tmp_path, "请先讨论A，再讨论B"
        )
        assert result["agent_id"] == "host"
        assert result["waiting"] is True
        thread.join(10)
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)

    assert not thread.is_alive()
    assert meeting.status == MeetingStatus.completed.value
    assert agents[0].status == "completed"
    assert agents[0].rounds_completed == [1]
    assert agents[1].rounds_completed == [1]
    assert prompts and "请先讨论A，再讨论B" in prompts[0]
    shared = meeting_mod.get_shared_file_path(meeting, tmp_path).read_text(encoding="utf-8")
    assert "请先讨论A，再讨论B" in shared
    assert "### 我 (human) - 第1轮" in shared


def test_human_can_interject_without_a_human_seat(tmp_path):
    meeting = Meeting(
        id="interject",
        topic="interject",
        agents=[MeetingAgent(id="worker", name="Worker", runtime="claude")],
        rounds=1,
        status=MeetingStatus.running.value,
    )
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    try:
        result = submit_human_speech(
            meeting.id, tmp_path, "补充一个问题：成本如何？"
        )
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)

    assert result["agent_id"] == "__human__"
    assert result["agent_name"] == "人类参与者"
    assert result["waiting"] is False
    shared = meeting_mod.get_shared_file_path(meeting, tmp_path).read_text(encoding="utf-8")
    assert "人类参与者 (human)" in shared
    assert "补充一个问题：成本如何？" in shared


def test_human_turn_can_be_skipped(tmp_path):
    meeting = Meeting(
        id="skip-human",
        topic="skip human",
        agents=[MeetingAgent(id="host", name="我", runtime="human")],
        rounds=1,
        status=MeetingStatus.running.value,
    )
    meeting._waiting_human = "host"
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    try:
        result = submit_human_speech(meeting.id, tmp_path, skip=True)
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)

    assert result["output"] == ""
    assert result["waiting"] is True
    assert meeting._human_queue == [{"skip": True}]


def test_human_speech_requires_a_running_meeting(tmp_path):
    meeting = Meeting(
        id="not-running",
        topic="not running",
        agents=[MeetingAgent(id="host", name="我", runtime="human")],
        rounds=1,
        status=MeetingStatus.completed.value,
    )
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    try:
        try:
            submit_human_speech(meeting.id, tmp_path, "hello")
        except ValueError as exc:
            assert "未在运行中" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)


def test_update_meeting_keeps_unchanged_members_for_resume(tmp_path):
    kept = MeetingAgent(
        id="claude",
        name="Claude",
        runtime="claude",
        model="m",
        output="旧发言",
        thinking="旧思考",
        rounds_completed=[1],
        status="completed",
    )
    changed = MeetingAgent(
        id="cline",
        name="Cline",
        runtime="cline",
        model="old-model",
        output="旧 cline",
        rounds_completed=[1],
        status="completed",
    )
    meeting = Meeting(
        id="upd-keep",
        topic="t",
        agents=[kept, changed],
        rounds=3,
        current_round=2,
    )
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    try:
        updated = meeting_mod.update_meeting(
            meeting.id,
            agents=[
                {
                    "id": "claude",
                    "name": "Claude",
                    "runtime": "claude",
                    "model": "m",
                },
                {
                    "id": "cline",
                    "name": "Cline",
                    "runtime": "cline",
                    "model": "new-model",
                },
            ],
            rounds=3,
            project_root=tmp_path,
        )
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)

    by_id = {a.id: a for a in updated.agents}
    assert by_id["claude"].output == "旧发言"
    assert by_id["claude"].thinking == "旧思考"
    assert by_id["claude"].rounds_completed == [1]
    # A member whose model changed must speak again.
    assert by_id["cline"].output == ""
    assert by_id["cline"].rounds_completed == []
    assert updated.current_round == 2


def test_update_meeting_resets_progress_when_round_count_changes(tmp_path):
    agent = MeetingAgent(
        id="claude",
        name="Claude",
        runtime="claude",
        output="旧发言",
        thinking="旧思考",
        rounds_completed=[1],
        status="completed",
    )
    meeting = Meeting(
        id="upd-rounds",
        topic="t",
        agents=[agent],
        rounds=2,
        current_round=2,
    )
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    try:
        updated = meeting_mod.update_meeting(
            meeting.id,
            agents=[
                {"id": "claude", "name": "Claude", "runtime": "claude"},
            ],
            rounds=4,
            project_root=tmp_path,
        )
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)

    assert updated.rounds == 4
    assert updated.current_round == 1
    assert updated.agents[0].output == ""
    assert updated.agents[0].thinking == ""
    assert updated.agents[0].rounds_completed == []


def test_strip_leading_speaker_heading_removes_other_member_title():
    meeting = Meeting(
        id="m",
        topic="t",
        agents=[
            MeetingAgent(id="opencode", name="代码工程师", runtime="opencode"),
            MeetingAgent(id="kilo", name="风险评审人", runtime="kilo"),
            MeetingAgent(id="cline", name="统计分析师", runtime="cline"),
        ],
    )
    agent = meeting.agents[0]
    text = "## 评审人（kilo）· 第 6 轮\n\n### 一、样本偏差复核\n\n正文内容。"
    cleaned = meeting_mod._strip_leading_speaker_heading(meeting, agent, text)
    assert cleaned.startswith("### 一、样本偏差复核")
    assert "评审人" not in cleaned.splitlines()[0]


def test_strip_leading_speaker_heading_keeps_real_section_heading():
    meeting = Meeting(
        id="m",
        topic="t",
        agents=[MeetingAgent(id="opencode", name="代码工程师", runtime="opencode")],
    )
    text = "### 一、结论\n\n这是正文。"
    assert (
        meeting_mod._strip_leading_speaker_heading(
            meeting, meeting.agents[0], text
        )
        == text
    )


def test_strip_leading_speaker_heading_removes_self_intro_title():
    meeting = Meeting(
        id="m",
        topic="t",
        agents=[
            MeetingAgent(id="opencode", name="代码工程师", runtime="opencode"),
            MeetingAgent(id="kilo", name="风险评审人", runtime="kilo"),
        ],
    )
    agent = meeting.agents[1]
    cleaned = meeting_mod._strip_leading_speaker_heading(
        meeting, agent, "评审人（kilo）· 第6轮终审\n\n正文。"
    )
    assert cleaned == "正文。"


def test_speech_prefix_filter_holds_and_drops_copied_heading():
    meeting = Meeting(
        id="m",
        topic="t",
        agents=[
            MeetingAgent(id="opencode", name="代码工程师", runtime="opencode"),
            MeetingAgent(id="kilo", name="风险评审人", runtime="kilo"),
        ],
    )
    emitted = []
    flt = meeting_mod._SpeechPrefixFilter(
        meeting, meeting.agents[0], emitted.append
    )
    flt("## 评审人（kilo）· 第 6 轮")
    assert emitted == []
    flt("\n\n正文开始")
    flt("，继续。")
    flt.flush()
    assert emitted == ["正文开始", "，继续。"]


def test_speech_prefix_filter_passes_plain_speech():
    meeting = Meeting(
        id="m",
        topic="t",
        agents=[MeetingAgent(id="opencode", name="代码工程师", runtime="opencode")],
    )
    emitted = []
    flt = meeting_mod._SpeechPrefixFilter(
        meeting, meeting.agents[0], emitted.append
    )
    flt("直接发言")
    assert emitted == []
    flt("，没有标题\n")
    flt.flush()
    assert emitted == ["直接发言，没有标题\n"]


def test_generate_requirements_writes_meeting_deliverable(tmp_path, monkeypatch):
    meeting = Meeting(
        id="req-meeting",
        topic="需求收敛",
        description="做一个任务系统",
        agents=[
            MeetingAgent(id="host", name="主持", runtime="claude", role="主持人"),
            MeetingAgent(id="coder", name="开发", runtime="cline", role="实现"),
        ],
        rounds=1,
        status=MeetingStatus.completed.value,
        auto_requirements=True,
    )
    with meeting_mod._meetings_lock:
        meeting_mod._meetings[meeting.id] = meeting
    try:
        meeting_mod._sync_shared_header(meeting, tmp_path)
        shared = meeting_mod.get_shared_file_path(meeting, tmp_path)
        with open(shared, "a", encoding="utf-8") as fh:
            fh.write("\n## 讨论\n- 需要任务队列\n- 需要人类插话\n")

        seen = {}

        class FakeSession:
            def run(self, prompt, timeout, stop_event, on_output=None, **kwargs):
                seen["prompt"] = prompt
                if on_output:
                    on_output("# 项目需求文档")
                return "# 项目需求文档\n\n## 1. 背景与目标\n任务系统\n"

            def close(self):
                seen["closed"] = True

        monkeypatch.setattr(
            meeting_session,
            "start_meeting_agent_session",
            lambda agent, root: FakeSession(),
        )

        text = meeting_mod.generate_requirements(meeting.id, tmp_path)

        path = meeting_mod.get_requirements_path(meeting, tmp_path)
        assert path.exists()
        assert "任务系统" in path.read_text(encoding="utf-8")
        assert text.startswith("# 项目需求文档")
        assert "需要任务队列" in seen["prompt"]
        assert seen["closed"] is True
        assert meeting._requirements_status == "ready"
    finally:
        with meeting_mod._meetings_lock:
            meeting_mod._meetings.pop(meeting.id, None)


def test_auto_requirements_flag_persists(tmp_path):
    meeting = Meeting(
        id="req-persist",
        topic="t",
        agents=[],
        auto_requirements=True,
    )
    meeting_mod._save_meeting(meeting, tmp_path)
    loaded = meeting_mod._load_meeting_from_disk(meeting.id, tmp_path)
    assert loaded is not None
    assert loaded.auto_requirements is True

    legacy = Meeting(id="legacy", topic="t", agents=[])
    meeting_mod._save_meeting(legacy, tmp_path)
    loaded_legacy = meeting_mod._load_meeting_from_disk(legacy.id, tmp_path)
    assert loaded_legacy is not None
    assert loaded_legacy.auto_requirements is False

