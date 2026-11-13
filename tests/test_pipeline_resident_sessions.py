from aqm.core.agent import AgentDefinition
from aqm.core.pipeline import Pipeline
from aqm.core.task import Task, TaskStatus
from aqm.queue.file import FileQueue
import aqm.runtime.meeting_session as meeting_session


class FakeAgentSession:
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.prompts = []
        self.closed = False

    @property
    def is_alive(self):
        return not self.closed

    def run(
        self,
        prompt,
        timeout,
        stop_event,
        on_output=None,
        on_thinking=None,
        on_action=None,
        on_process=None,
    ):
        self.prompts.append(prompt)
        output = f"{self.agent_id} output {len(self.prompts)}"
        if on_output:
            on_output(f"{self.agent_id} chunk ")
            on_output(f"{len(self.prompts)}")
        if on_thinking:
            on_thinking(f"{self.agent_id} thinking ")
        if on_action:
            on_action(f"{self.agent_id} action")
        if on_process:
            on_process(f"{self.agent_id} narration")
        return output

    def close(self):
        self.closed = True


def test_pipeline_starts_all_agents_once_and_reuses_them(monkeypatch, tmp_project):
    agents = {
        "planner": AgentDefinition(
            id="planner",
            runtime="claude",
            system_prompt="Plan: {{ input }}",
            handoffs=[{"to": "reviewer"}],
        ),
        "reviewer": AgentDefinition(
            id="reviewer",
            runtime="claude",
            system_prompt="Review: {{ input }}",
        ),
    }
    started = []
    sessions = {}

    def start(agent, project_root, stop_event):
        started.append(agent.id)
        session = FakeAgentSession(agent.id)
        sessions[agent.id] = session
        return session

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "resident-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    task = Task(description="resident test")
    queue.push(task, "planner")
    output = []

    result = pipeline.run_task(task, "planner", on_output=output.append)

    assert result.status == TaskStatus.completed
    assert result.metadata["project_dir"] == str(tmp_project.resolve())
    assert sorted(started) == ["planner", "reviewer"]
    assert len(sessions["planner"].prompts) == 1
    assert len(sessions["reviewer"].prompts) == 1
    assert all(session.closed for session in sessions.values())
    assert output == ["planner chunk ", "1", "reviewer chunk ", "1"]


def test_paused_task_keeps_its_stage_checkpoint(monkeypatch, tmp_project):
    """Pausing at a stage boundary must not lose the resume checkpoint.

    A paused task blocks before the stage starts.  If the server is restarted
    while it is paused, auto-resume needs ``_checkpoint_stage`` to know which
    stage to continue; without it the task replays the previous stage.
    """
    import threading
    import time

    from aqm.core.pipeline import clear_pause, pause_task, resume_paused_task

    agents = {
        "writer": AgentDefinition(
            id="writer", runtime="claude", system_prompt="Write: {{ input }}",
        ),
    }
    monkeypatch.setattr(
        meeting_session,
        "start_meeting_agent_session",
        lambda agent, project_root, stop_event: FakeAgentSession(agent.id),
    )
    queue = FileQueue(tmp_project / ".aqm" / "paused-checkpoint-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    task = Task(description="paused checkpoint")
    queue.push(task, "writer")
    finished: dict[str, object] = {}

    pause_task(task.id)
    try:
        thread = threading.Thread(
            target=lambda: finished.setdefault(
                "task", pipeline.run_task(task, "writer")
            ),
            daemon=True,
        )
        thread.start()

        deadline = time.time() + 5
        live = None
        while time.time() < deadline:
            live = queue.get(task.id)
            if live and live.metadata.get("_checkpoint_stage") == 1:
                break
            time.sleep(0.02)

        assert live is not None
        assert live.metadata.get("_checkpoint_stage") == 1
        assert live.metadata.get("_checkpoint_agent_id") == "writer"
        assert thread.is_alive(), "the task must stay blocked while paused"

        resume_paused_task(task.id)
        thread.join(5)
        assert not thread.is_alive()
        assert finished["task"].status == TaskStatus.completed
    finally:
        clear_pause(task.id)


def test_human_interjection_reaches_next_agent(monkeypatch, tmp_project):
    from aqm.core.pipeline import submit_interjection

    agents = {
        "planner": AgentDefinition(
            id="planner",
            runtime="claude",
            system_prompt="Plan: {{ input }}",
            handoffs=[{"to": "reviewer"}],
        ),
        "reviewer": AgentDefinition(
            id="reviewer",
            runtime="claude",
            system_prompt="Review: {{ input }}",
        ),
    }
    sessions = {}

    class InterjectingSession(FakeAgentSession):
        def run(self, prompt, timeout, stop_event, **kwargs):
            self.prompts.append(prompt)
            if self.agent_id == "planner":
                # Simulate the user typing while the first stage runs.
                submit_interjection(task.id, "请补充错误处理")
            return f"{self.agent_id} output"

    def start(agent, project_root, stop_event):
        session = InterjectingSession(agent.id)
        sessions[agent.id] = session
        return session

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "interjection-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    task = Task(description="interjection test")
    queue.push(task, "planner")
    applied = []

    result = pipeline.run_task(
        task,
        "planner",
        on_interjection=lambda t, notes, agent_id: applied.append((agent_id, notes)),
    )

    assert result.status == TaskStatus.completed
    assert "请补充错误处理" not in sessions["planner"].prompts[0]
    assert "请补充错误处理" in sessions["reviewer"].prompts[0]
    assert applied == [("reviewer", ["请补充错误处理"])]
    assert task.metadata.get("interjections") == ["请补充错误处理"]
    assert task.metadata.get("pending_interjections") is None
    log = task.metadata.get("interjection_log") or []
    assert [entry["agent_id"] for entry in log] == ["reviewer"]
    assert log[0]["text"] == "请补充错误处理"
    assert log[0]["at"]


def test_resident_start_failure_marks_task_failed(monkeypatch, tmp_project):
    agents = {
        "writer": AgentDefinition(id="writer", runtime="claude"),
    }

    def start(agent, project_root, stop_event):
        raise RuntimeError("CLI is unavailable")

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "resident-failure-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    task = Task(description="resident startup failure")
    queue.push(task, "writer")

    result = pipeline.run_task(task, "writer")

    assert result.status == TaskStatus.failed
    assert "CLI is unavailable" in result.metadata["error"]
    assert queue.get(task.id).status == TaskStatus.failed

def test_dead_resident_session_is_recreated_before_the_turn(monkeypatch, tmp_project):
    """A CLI process that died between stages must not fail the stage.

    kilo/opencode ACP processes can exit while idle (or be killed by an idle
    timeout).  The old code raised "Resident session ... exited", which hid
    the real cause and required a manual restart.  The pipeline now recreates
    the session and runs the turn normally.
    """
    agents = {
        "writer": AgentDefinition(
            id="writer", runtime="kilo", system_prompt="Write: {{ input }}",
        ),
    }
    started = []

    class DeadSession:
        is_alive = False

        def run(self, *args, **kwargs):  # pragma: no cover - must not be used
            raise AssertionError("dead session must be replaced before running")

        def close(self):
            pass

        def exit_detail(self):
            return "CLI process exited with code 1"

    def start(agent, project_root, stop_event):
        started.append(agent.id)
        if len(started) == 1:
            return DeadSession()
        return FakeAgentSession(agent.id)

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "recreate-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    task = Task(description="recreate resident session")
    queue.push(task, "writer")

    result = pipeline.run_task(task, "writer")

    assert result.status == TaskStatus.completed
    assert started == ["writer", "writer"]
    assert result.stages[0].output_text == "writer output 1"


def test_dead_session_recreation_failure_reports_the_reason(monkeypatch, tmp_project):
    agents = {"writer": AgentDefinition(id="writer", runtime="kilo")}
    calls = []

    class DeadSession:
        is_alive = False

        def run(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("unused")

        def close(self):
            pass

        def exit_detail(self):
            return "bad credentials"

    def start(agent, project_root, stop_event):
        calls.append(1)
        if len(calls) == 1:
            return DeadSession()
        raise RuntimeError("cannot start kilo")

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "recreate-fail-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    task = Task(description="recreate failure")
    queue.push(task, "writer")

    result = pipeline.run_task(task, "writer")

    assert result.status == TaskStatus.failed
    assert "could not be recreated" in result.stages[-1].output_text
    assert "cannot start kilo" in result.stages[-1].output_text


def test_human_interjection_interrupts_current_agent_and_answers_again(
    monkeypatch, tmp_project
):
    from aqm.core.interrupt import AgentInterrupted
    from aqm.core.pipeline import submit_interjection

    agents = {
        "planner": AgentDefinition(
            id="planner",
            runtime="claude",
            system_prompt="Plan: {{ input }}",
            handoffs=[{"to": "reviewer"}],
        ),
        "reviewer": AgentDefinition(
            id="reviewer",
            runtime="claude",
            system_prompt="Review: {{ input }}",
        ),
    }
    prompts = {"planner": [], "reviewer": []}
    task = Task(description="live interjection test")

    class InterruptingSession:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.prompts = prompts[agent_id]
            self.closed = False

        @property
        def is_alive(self):
            return not self.closed

        def run(
            self,
            prompt,
            timeout,
            stop_event,
            on_output=None,
            on_thinking=None,
            on_action=None,
            on_process=None,
            interrupt_event=None,
        ):
            self.prompts.append(prompt)
            if self.agent_id == "planner" and len(self.prompts) == 1:
                submit_interjection(task.id, "先检查中文再继续")
                raise AgentInterrupted("human interjection")
            return f"{self.agent_id} output"

        def close(self):
            self.closed = True

    def start(agent, project_root, stop_event):
        return InterruptingSession(agent.id)

    monkeypatch.setattr(meeting_session, "start_meeting_agent_session", start)
    queue = FileQueue(tmp_project / ".aqm" / "live-interjection-queue")
    pipeline = Pipeline(agents, queue, tmp_project, resident_agents=True)
    queue.push(task, "planner")
    applied = []
    interrupted = []

    result = pipeline.run_task(
        task,
        "planner",
        on_interjection=lambda t, notes, agent_id: applied.append((agent_id, notes)),
        on_stage_interrupted=lambda t, agent_id, stage_number, notes: interrupted.append(
            (agent_id, stage_number, notes)
        ),
    )

    assert result.status == TaskStatus.completed
    assert len(prompts["planner"]) == 2
    assert "先检查中文再继续" not in prompts["planner"][0]
    assert "先检查中文再继续" in prompts["planner"][1]
    assert "重新给出本轮完整答复" in prompts["planner"][1]
    assert applied == [("planner", ["先检查中文再继续"])]
    assert interrupted == [("planner", 1, ["先检查中文再继续"])]
    log = task.metadata["interjection_log"]
    assert log[0]["agent_id"] == "planner"
    assert log[0]["stage_number"] == 1
    assert task.metadata["interjections"] == ["先检查中文再继续"]
