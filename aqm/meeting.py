"""Meeting engine — multi-agent collaborative discussion with shared file.

Manages meeting lifecycle: configuration, execution, real-time streaming,
and shared file management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class MeetingStatus(str, Enum):
    idle = "idle"
    running = "running"
    completed = "completed"
    stopped = "stopped"
    error = "error"


class AgentTurnStatus(str, Enum):
    waiting = "waiting"
    awaiting_input = "awaiting_input"
    running = "running"
    completed = "completed"
    error = "error"
    skipped = "skipped"


@dataclass
class MeetingAgent:
    id: str
    name: str
    runtime: str
    role: str = ""
    system_prompt: str = ""
    model: str = ""
    directory_access: str = ""
    status: str = AgentTurnStatus.waiting.value
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    output: str = ""
    thinking: str = ""
    actions: str = ""
    error: str = ""
    rounds_completed: list[int] = field(default_factory=list)
    resident: bool = False

    def __post_init__(self) -> None:
        if self.runtime == "claude":
            self.directory_access = "project_read_write"


@dataclass
class Meeting:
    id: str
    topic: str
    description: str = ""
    agents: list[MeetingAgent] = field(default_factory=list)
    shared_file: str = "meeting.md"
    status: str = MeetingStatus.idle.value
    current_agent_index: int = 0
    rounds: int = 1
    current_round: int = 1
    timeout_per_agent: int = 300
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: str = ""
    project_dir: Optional[str] = None
    # When true, a completed meeting asks its host member to turn the discussion
    # into REQUIREMENTS.md. Off by default so tests/embedded meetings never
    # spawn an extra CLI session unexpectedly.
    auto_requirements: bool = False
    _thread: Optional[threading.Thread] = None
    # Requirements handoff status (never persisted; the file itself is truth).
    _requirements_status: str = "idle"
    _requirements_error: str = ""
    _stop_event: threading.Event = field(default_factory=threading.Event)
    # Human-in-the-loop state (never persisted).
    _human_cond: threading.Condition = field(default_factory=threading.Condition)
    _human_queue: list[dict] = field(default_factory=list)
    _waiting_human: Optional[str] = None
    _waiting_human_name: str = ""
    # Monotonic run id. A thread only writes/saves/broadcasts while it still
    # owns the meeting, so a stop+continue cannot be corrupted by the old
    # thread finishing late.
    _run_token: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "description": self.description,
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "runtime": a.runtime,
                    "role": a.role,
                    "system_prompt": a.system_prompt,
                    "model": a.model,
                    "directory_access": a.directory_access,
                    "status": a.status,
                    "started_at": a.started_at,
                    "finished_at": a.finished_at,
                    "output_preview": a.output[:300] if a.output else "",
                    "thinking_length": len(a.thinking or ""),
                    "actions_length": len(a.actions or ""),
                    "error": a.error,
                    "rounds_completed": list(a.rounds_completed),
                    "resident": a.resident,
                }
                for a in self.agents
            ],
            "shared_file": self.shared_file,
            "status": self.status,
            "current_agent_index": self.current_agent_index,
            "rounds": self.rounds,
            "current_round": self.current_round,
            "timeout_per_agent": self.timeout_per_agent,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "project_dir": self.project_dir,
            "auto_requirements": self.auto_requirements,
            "requirements_status": self._requirements_status,
            "requirements_error": self._requirements_error,
            "waiting_for_human": self._waiting_human,
            "waiting_human_name": self._waiting_human_name,
            "has_human_agent": any(a.runtime == "human" for a in self.agents),
        }

    def to_persist_dict(self) -> dict:
        """Full serialization including agent outputs for disk persistence."""
        return {
            "id": self.id,
            "topic": self.topic,
            "description": self.description,
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "runtime": a.runtime,
                    "role": a.role,
                    "system_prompt": a.system_prompt,
                    "model": a.model,
                    "directory_access": a.directory_access,
                    "status": a.status,
                    "started_at": a.started_at,
                    "finished_at": a.finished_at,
                    "output": a.output,
                    "thinking": a.thinking,
                    "actions": a.actions,
                    "error": a.error,
                    "rounds_completed": list(a.rounds_completed),
                }
                for a in self.agents
            ],
            "shared_file": self.shared_file,
            "status": self.status,
            "current_agent_index": self.current_agent_index,
            "rounds": self.rounds,
            "current_round": self.current_round,
            "timeout_per_agent": self.timeout_per_agent,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "project_dir": self.project_dir,
            "auto_requirements": self.auto_requirements,
        }

    @classmethod
    def from_persist_dict(cls, data: dict) -> Meeting:
        """Deserialize from persisted dict on disk."""
        known_fields = set(MeetingAgent.__dataclass_fields__.keys())
        return cls(
            id=data["id"],
            topic=data["topic"],
            description=data.get("description", ""),
            agents=[
                MeetingAgent(**{k: v for k, v in a.items() if k in known_fields})
                for a in data.get("agents", [])
            ],
            shared_file=data.get("shared_file", "meeting.md"),
            status=data.get("status", "idle"),
            current_agent_index=data.get("current_agent_index", 0),
            rounds=data.get("rounds", 1),
            current_round=data.get("current_round", 1),
            timeout_per_agent=data.get("timeout_per_agent", 300),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error", ""),
            project_dir=data.get("project_dir"),
            auto_requirements=bool(data.get("auto_requirements", False)),
        )


# In-memory meeting store
_meetings: dict[str, Meeting] = {}
_meetings_lock = threading.Lock()

# SSE subscribers: meeting_id -> list of asyncio.Queue
_sse_subscribers: dict[str, list[asyncio.Queue]] = {}
_sse_lock = threading.Lock()

# Serializes read/replace/append on the shared discussion file so an in-place
# header edit can never race with a speech append.
_shared_lock = threading.RLock()


def _meetings_dir(project_root: Path) -> Path:
    return project_root / ".aqm" / "meetings"


def _save_meeting(meeting: Meeting, project_root: Path) -> None:
    """Save meeting to disk as .aqm/meetings/{id}/meta.json."""
    d = _meetings_dir(project_root) / meeting.id
    d.mkdir(parents=True, exist_ok=True)
    meta_path = d / "meta.json"
    payload = json.dumps(meeting.to_persist_dict(), ensure_ascii=False, indent=2)
    # Write-then-rename so a concurrent reader (Web API) never sees a
    # half-written file.
    tmp_path = meta_path.with_suffix(".json.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, meta_path)


def _load_meeting_from_disk(meeting_id: str, project_root: Path) -> Optional[Meeting]:
    meta_path = _meetings_dir(project_root) / meeting_id / "meta.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        meeting = Meeting.from_persist_dict(data)
        _migrate_legacy_shared_file(meeting, project_root)
        return meeting
    except Exception as e:
        logger.warning("Failed to load meeting %s: %s", meeting_id, e)
        return None


def _delete_meeting_from_disk(meeting_id: str, project_root: Path) -> None:
    import shutil
    d = _meetings_dir(project_root) / meeting_id
    if d.exists():
        shutil.rmtree(d)


def load_meetings_on_startup(project_root: Path) -> None:
    """Load all persisted meetings from disk into memory. Call on server/CLI startup."""
    d = _meetings_dir(project_root)
    if not d.exists():
        return
    for meeting_dir in d.iterdir():
        if not meeting_dir.is_dir():
            continue
        meeting_id = meeting_dir.name
        if meeting_id in _meetings:
            continue
        meta_path = meeting_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            meeting = Meeting.from_persist_dict(data)
            _migrate_legacy_shared_file(meeting, project_root)
            if meeting.status == MeetingStatus.running.value:
                meeting.status = MeetingStatus.stopped.value
            with _meetings_lock:
                _meetings[meeting.id] = meeting
        except Exception as e:
            logger.warning("Failed to load meeting %s: %s", meeting_dir.name, e)


def create_meeting(
    topic: str,
    description: str,
    agents: list[dict],
    shared_file: str = "meeting.md",
    rounds: int = 1,
    timeout_per_agent: int = 300,
    project_root: Optional[Path] = None,
) -> Meeting:
    meeting_id = str(uuid.uuid4())[:8]
    meeting = Meeting(
        id=meeting_id,
        topic=topic,
        description=description,
        agents=[MeetingAgent(
            id=a["id"],
            name=a["name"],
            runtime=a["runtime"],
            role=a.get("role", ""),
            system_prompt=a.get("system_prompt", ""),
            model=a.get("model", ""),
            directory_access=a.get("directory_access", ""),
        ) for a in agents],
        shared_file=shared_file,
        rounds=rounds,
        timeout_per_agent=timeout_per_agent,
        auto_requirements=True,
    )
    with _meetings_lock:
        _meetings[meeting_id] = meeting
    if project_root:
        meeting.project_dir = str(Path(project_root).resolve())
        _save_meeting(meeting, project_root)
    return meeting


def get_meeting(meeting_id: str, project_root: Optional[Path] = None) -> Optional[Meeting]:
    with _meetings_lock:
        m = _meetings.get(meeting_id)
    if m:
        return m
    if project_root:
        m = _load_meeting_from_disk(meeting_id, project_root)
        if m:
            with _meetings_lock:
                _meetings[meeting_id] = m
    return m


def _activity_key(m: Meeting) -> str:
    """Latest activity timestamp for a meeting (ISO strings, newest wins)."""
    ts = [m.created_at or ""]
    if m.started_at:
        ts.append(m.started_at)
    if m.finished_at:
        ts.append(m.finished_at)
    return max(ts)


def list_meetings(project_root: Optional[Path] = None) -> list[Meeting]:
    with _meetings_lock:
        meetings = list(_meetings.values())

    # Gather disk copies (current root + all recent projects), de-duplicated by id
    found: dict[str, Meeting] = {}

    def _scan(d: Path, loc: Path) -> None:
        if not d.exists():
            return
        for meeting_dir in d.iterdir():
            if not meeting_dir.is_dir():
                continue
            mid = meeting_dir.name
            m = _load_meeting_from_disk(mid, loc)
            if not m:
                continue
            if not m.project_dir:
                m.project_dir = str(loc)
            cur = found.get(mid)
            # Newest activity wins; deterministic so it cannot flip-flop between
            # duplicate copies that were (buggily) saved to more than one project.
            if cur is None or _activity_key(m) > _activity_key(cur):
                found[mid] = m

    if project_root:
        _scan(_meetings_dir(project_root), project_root)

    try:
        from aqm.core.config import get_recent_projects
        for rp in get_recent_projects():
            _scan(_meetings_dir(rp), rp)
    except Exception:
        pass

    # Merge with in-memory copies: newest wins, but never clobber a running one
    with _meetings_lock:
        for mid, m in found.items():
            cur = _meetings.get(mid)
            if cur is None:
                _meetings[mid] = m
                meetings.append(m)
            elif cur.status == MeetingStatus.running.value:
                continue  # preserve the live running meeting object
            elif _activity_key(m) > _activity_key(cur):
                _meetings[mid] = m
                meetings = [m if x is cur else x for x in meetings]

    return sorted(meetings, key=lambda m: m.created_at, reverse=True)


def delete_meeting(meeting_id: str, project_root: Optional[Path] = None) -> bool:
    with _meetings_lock:
        meeting = _meetings.pop(meeting_id, None)
    if meeting and meeting._thread and meeting._thread.is_alive():
        return False
    if meeting is not None and project_root:
        _delete_meeting_from_disk(meeting_id, project_root)
    return meeting is not None


def _same_agent_config(old: MeetingAgent, new: MeetingAgent) -> bool:
    """True when two agents describe the same member for resume purposes.

    The display name is intentionally ignored so renaming a member does not
    force it to speak again.
    """
    return (
        old.runtime == new.runtime
        and old.role == new.role
        and old.system_prompt == new.system_prompt
        and old.model == new.model
        and old.directory_access == new.directory_access
    )


def update_meeting(
    meeting_id: str,
    topic: str = None,
    description: str = None,
    agents: list[dict] = None,
    shared_file: str = None,
    rounds: int = None,
    timeout_per_agent: int = None,
    project_root: Optional[Path] = None,
) -> Optional[Meeting]:
    """Update meeting fields. If rounds changed, reset progress."""
    with _meetings_lock:
        meeting = _meetings.get(meeting_id)
    if meeting is None and project_root:
        meeting = _load_meeting_from_disk(meeting_id, project_root)
        if meeting:
            with _meetings_lock:
                _meetings[meeting_id] = meeting
    if meeting is None:
        return None

    if topic is not None:
        meeting.topic = topic
    if description is not None:
        meeting.description = description
    if shared_file is not None:
        meeting.shared_file = shared_file
    if timeout_per_agent is not None:
        meeting.timeout_per_agent = timeout_per_agent

    rounds_changed = rounds is not None and rounds != meeting.rounds
    if agents is not None:
        previous = {a.id: a for a in meeting.agents}
        rebuilt: list[MeetingAgent] = []
        for a in agents:
            agent = MeetingAgent(
                id=a["id"],
                name=a.get("name", a["id"]),
                runtime=a.get("runtime", "claude"),
                role=a.get("role", ""),
                system_prompt=a.get("system_prompt", ""),
                model=a.get("model", ""),
                directory_access=a.get("directory_access", ""),
            )
            old = previous.get(agent.id)
            # "Save and continue" must not replay rounds for members whose
            # settings did not change: keep their turn state so the resumed
            # run skips them instead of wiping prior speeches.
            if old is not None and not rounds_changed and _same_agent_config(old, agent):
                agent.status = old.status
                agent.started_at = old.started_at
                agent.finished_at = old.finished_at
                agent.output = old.output
                agent.thinking = old.thinking
                agent.actions = old.actions
                agent.error = old.error
                agent.rounds_completed = list(old.rounds_completed)
                agent.resident = old.resident
            rebuilt.append(agent)
        meeting.agents = rebuilt

    # If rounds changed, reset progress
    if rounds_changed:
        meeting.rounds = rounds
        meeting.current_round = 1
        meeting.current_agent_index = 0
        for a in meeting.agents:
            a.status = AgentTurnStatus.waiting.value
            a.started_at = None
            a.finished_at = None
            a.output = ""
            a.thinking = ""
            a.actions = ""
            a.error = ""
            a.rounds_completed = []
        # Remove old shared file
        shared_path = (
            get_shared_file_path(meeting, project_root) if project_root else None
        )
        if shared_path and shared_path.exists():
            shared_path.unlink()

    if project_root:
        meeting.project_dir = str(Path(project_root).resolve())
        # Reflect an edited topic/description in the existing shared file by
        # replacing its header block in place (never append a second one).
        if rounds_changed or get_shared_file_path(meeting, project_root).exists():
            _sync_shared_header(meeting, project_root)
        _save_meeting(meeting, project_root)
    return meeting


def broadcast_meeting_event(meeting_id: str, event_type: str, data: dict) -> None:
    msg = {"event": event_type, "data": json.dumps(data)}
    with _sse_lock:
        queues = _sse_subscribers.get(meeting_id, [])
        for q in queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


def subscribe_meeting(meeting_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    with _sse_lock:
        _sse_subscribers.setdefault(meeting_id, []).append(q)
    return q


def unsubscribe_meeting(meeting_id: str, q: asyncio.Queue) -> None:
    with _sse_lock:
        queues = _sse_subscribers.get(meeting_id, [])
        try:
            queues.remove(q)
        except ValueError:
            pass
        if not queues:
            _sse_subscribers.pop(meeting_id, None)


def _resolve_cmd(exe: str) -> list[str]:
    import shutil
    def _find(npm_root: str, pkg: str) -> str | None:
        for sub in ("bin", "bin/bin"):
            c = os.path.join(npm_root, "node_modules", pkg, sub, exe + ".exe")
            if os.path.exists(c):
                return c
        return None

    def _find_js(npm_root: str, pkg: str) -> str | None:
        c = os.path.join(npm_root, "node_modules", pkg, "bin", exe)
        if os.path.isfile(c):
            return c
        return None

    def _npm_root() -> str | None:
        r = os.environ.get("NPM_CONFIG_PREFIX")
        if r:
            return r
        appdata = os.environ.get("APPDATA") or os.path.expandvars("%APPDATA%")
        if appdata:
            return os.path.join(appdata, "npm")
        return None

    if sys.platform == "win32":
        import shutil
        r = shutil.which(exe + ".exe")
        if r:
            return [r]
        npm_root = _npm_root()
        if npm_root:
            for pkg in (exe, exe.replace("kilo", "@kilocode/cli"), exe.replace("opencode", "opencode-ai")):
                found = _find(npm_root, pkg)
                if found:
                    return [found]
            node_exe = shutil.which("node.exe") or shutil.which("node")
            if node_exe:
                for pkg in (exe, exe.replace("kilo", "@kilocode/cli"), exe.replace("opencode", "opencode-ai")):
                    found = _find_js(npm_root, pkg)
                    if found:
                        return [node_exe, found]
        for ext in (".cmd", ".bat"):
            r = shutil.which(exe + ext)
            if r:
                return [r]
        r = shutil.which(exe)
        if r:
            return [r]
    import shutil
    r = shutil.which(exe)
    return [r] if r else [exe]


def _build_cmd(runtime: str, prompt: str, system_prompt: str, model: str, project_root: Path) -> list[str]:
    if runtime == "claude":
        cmd = _resolve_cmd("claude") + ["--print", "-p", prompt, "--dangerously-skip-permissions"]
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        if model:
            cmd.extend(["--model", model])
    elif runtime == "opencode":
        # opencode run has no --dir flag; the working dir comes from cwd
        cmd = _resolve_cmd("opencode") + ["run", prompt, "--auto", "--format", "json"]
        if model:
            cmd.extend(["--model", model])
    elif runtime == "kilo":
        # --pure skips external plugins: faster cold start for one-shot runs
        cmd = _resolve_cmd("kilo") + ["run", prompt, "--auto", "--format", "json", "--dir", str(project_root), "--pure"]
        if model:
            cmd.extend(["--model", model])
    elif runtime == "cline":
        # cline parses [options] [command] [prompt]; keep options first and the
        # prompt last, and give --auto-approve an explicit boolean value.
        cmd = _resolve_cmd("cline") + [
            "--json", "--auto-approve", "true", "-y", "-c", str(project_root), prompt,
        ]
        if model:
            cmd.extend(["-m", model])
    elif runtime == "codex":
        cmd = _resolve_cmd("codex") + ["exec", prompt]
        if model:
            cmd.extend(["--model", model])
    elif runtime == "gemini":
        cmd = _resolve_cmd("gemini") + ["-p", prompt]
        if system_prompt:
            cmd.extend(["--system-instructions", system_prompt])
        if model:
            cmd.extend(["--model", model])
    else:
        cmd = _resolve_cmd(runtime) + [prompt]
    return cmd


def _run_agent_cli(
    agent: MeetingAgent,
    prompt: str,
    project_root: Path,
    timeout: int,
    stop_event: Optional[threading.Event] = None,
) -> str:
    cmd = _build_cmd(agent.runtime, prompt, agent.system_prompt, agent.model, project_root)

    if stop_event is None:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_root),
        )
        returncode, out, err = result.returncode, result.stdout, result.stderr
    else:
        # Poll so a stop request can interrupt the running CLI promptly.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(project_root),
        )
        deadline = time.monotonic() + timeout
        while True:
            if stop_event.is_set():
                proc.kill()
                proc.wait()
                raise InterruptedError("stopped by user")
            if proc.poll() is not None:
                break
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(0.2)
        out, err = proc.communicate()
        returncode = proc.returncode

    if returncode != 0:
        stderr = (err or "").strip()
        error_msg = stderr or f"process exited with code {returncode}"
        raise RuntimeError(f"{agent.runtime} CLI failed: {error_msg}")
    output = (out or "").strip()
    if agent.runtime in ("opencode", "kilo"):
        from aqm.runtime.json_stream import JsonStreamParser
        parser = JsonStreamParser()
        for line in output.splitlines():
            parser.feed_line(line)
        output = parser.collected_text
    elif agent.runtime == "cline":
        output = _extract_cline_text(output)
    return output


def _extract_cline_text(raw: str) -> str:
    """Extract the assistant's final answer from cline's ``--json`` event stream.

    cline reports the authoritative answer twice: once in the terminal ``done``
    agent event (``reason == "completed"``) and once in the trailing
    ``run_result`` record.  Both are preferred over the streamed
    ``content_start``/``content_end`` fragments, which also contain the
    preamble written before tool calls and would otherwise duplicate text.
    """
    run_result_text = ""
    done_text = ""
    fragments: list[str] = []
    saw_json = False

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        saw_json = True

        if record.get("type") == "run_result":
            text = record.get("text")
            if isinstance(text, str) and text.strip():
                run_result_text = text.strip()
            continue

        if record.get("type") != "agent_event":
            continue
        event = record.get("event")
        if not isinstance(event, dict):
            continue

        if event.get("type") == "done":
            if event.get("reason") == "completed":
                text = event.get("text")
                if isinstance(text, str) and text.strip():
                    done_text = text.strip()
            continue

        # Fallback only: a single text block per iteration, with ``accumulated``
        # carrying the cumulative value and ``content_end`` repeating it.
        if event.get("type") != "content_start" or event.get("contentType") != "text":
            continue
        text = event.get("accumulated")
        if not isinstance(text, str) or not text.strip():
            text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()
        if fragments:
            previous = fragments[-1]
            if text == previous:
                continue
            if text.startswith(previous):
                fragments[-1] = text
                continue
        fragments.append(text)

    for candidate in (run_result_text, done_text):
        if candidate:
            return _strip_cline_tool_prefix(candidate)
    if fragments:
        return _strip_cline_tool_prefix("\n".join(fragments).strip())
    if not saw_json:
        # Older cline builds may print plain text; keep it rather than dropping it.
        return raw.strip()
    return ""


def _strip_cline_tool_prefix(text: str) -> str:
    """Drop the wrapper cline adds around ``submit_and_exit`` tool results."""
    stripped = re.sub(
        r"^\s*Submission recorded(?:\s*\([^)]*\))?\s*:\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return stripped.strip() or text.strip()


_RUNTIME_TIMEOUTS = {
    "claude": 600,
    "opencode": 600,
    "kilo": 600,
    "cline": 600,
}


def _agent_timeout(meeting: Meeting, agent: MeetingAgent) -> int:
    """Resolve the *idle* timeout (seconds) for one agent turn.

    The value is not a wall-clock budget: every chunk the runtime emits resets
    it. It only fires when the model produces nothing for that long, so a long
    but still-streaming answer is never cut off mid-way.

    Each runtime has its own baseline; an explicit meeting-level value above
    that baseline still wins so it can be raised further by the user.
    """
    baseline = _RUNTIME_TIMEOUTS.get(agent.runtime, 300)
    try:
        configured = int(meeting.timeout_per_agent or 0)
    except (TypeError, ValueError):
        configured = 0
    return max(baseline, configured)


class _MeetingStreamer:
    """Throttle runtime chunk callbacks into SSE ``agent_stream`` events.

    Runtimes emit deltas many times per second; broadcasting each one would
    flood the SSE queue (which drops on overflow), so chunks are buffered and
    flushed at most a few times per second.
    """

    FLUSH_INTERVAL = 0.25

    def __init__(self, meeting: Meeting, agent: MeetingAgent, round_num: int) -> None:
        self.meeting = meeting
        self.agent = agent
        self.round = round_num
        self._lock = threading.Lock()
        self._buffers: dict[str, list[str]] = {
            "thinking": [],
            "text": [],
            "process": [],
            "action": [],
        }
        # Everything that was streamed for this turn. Used as a safety net so a
        # runtime that returns an empty final message does not lose the speech
        # the user just watched being generated.
        self._accum: dict[str, list[str]] = {
            "thinking": [],
            "text": [],
            "process": [],
            "action": [],
        }
        self._last_flush = 0.0

    def push(self, kind: str, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._accum.setdefault(kind, []).append(chunk)
            self._buffers.setdefault(kind, []).append(chunk)
            now = time.monotonic()
            if now - self._last_flush < self.FLUSH_INTERVAL:
                return
            self._last_flush = now
            payload = {
                key: "".join(parts)
                for key, parts in self._buffers.items()
                if parts
            }
            for key in payload:
                self._buffers[key] = []
        self._emit(payload)

    def thinking(self, chunk: str) -> None:
        self.push("thinking", chunk)

    def output(self, chunk: str) -> None:
        self.push("text", chunk)

    def action(self, chunk: str) -> None:
        self.push("action", chunk)

    def process(self, chunk: str) -> None:
        # Each narration block is a separate paragraph; keep them from being
        # glued together when several flush inside one throttle window.
        # Flush the answer text first so the browser sees the exact boundary
        # and can move this narration out of the speech body.
        self.flush()
        self.push("process", chunk.rstrip() + "\n\n")

    def accumulated(self, kind: str) -> str:
        with self._lock:
            return "".join(self._accum.get(kind, [])).strip()

    def flush(self) -> None:
        with self._lock:
            payload = {
                key: "".join(parts)
                for key, parts in self._buffers.items()
                if parts
            }
            for key in payload:
                self._buffers[key] = []
            # Let the next push bypass throttling: callers use flush() to
            # establish an ordering boundary (e.g. text before narration).
            self._last_flush = 0.0
        self._emit(payload)

    def _emit(self, payload: dict[str, str]) -> None:
        for kind, chunk in payload.items():
            broadcast_meeting_event(self.meeting.id, "agent_stream", {
                "meeting_id": self.meeting.id,
                "agent_id": self.agent.id,
                "agent_name": self.agent.name,
                "runtime": self.agent.runtime,
                "round": self.round,
                "kind": kind,
                "chunk": chunk,
            })


def _speaker_heading_tokens(meeting: Meeting) -> list[str]:
    tokens: list[str] = []
    for member in meeting.agents:
        for value in (member.name, member.id, member.runtime):
            if value:
                tokens.append(value)
    return list(dict.fromkeys(tokens))


def _is_speaker_heading_echo(meeting: Meeting, line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped:
        return False
    if not re.search(r"第\s*\d+\s*轮", stripped):
        return False
    return any(token in stripped for token in _speaker_heading_tokens(meeting))


def _strip_leading_speaker_heading(
    meeting: Meeting, agent: MeetingAgent, text: str
) -> str:
    """Remove a model-written speaker title from the start of a speech.

    Models sometimes continue the previous transcript entry and open their own
    answer with e.g. ``## 评审人（kilo）· 第 6 轮``.  The UI already renders the
    real speaker from event metadata, so a copied heading only makes the wrong
    member appear to be talking.  Strip only leading heading-like lines that
    contain a member name/runtime *and* a round marker, so genuine section
    headings such as ``### 一、结论`` are preserved.
    """
    if not text:
        return text
    lines = text.lstrip("\ufeff\n\r ").splitlines()
    if not lines:
        return text
    if not _is_speaker_heading_echo(meeting, lines[0]):
        return text

    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        if _is_speaker_heading_echo(meeting, lines[index]):
            index += 1
            continue
        break

    stripped = "\n".join(lines[index:]).strip()
    return stripped or text


class _SpeechPrefixFilter:
    """Drop a copied speaker title before it reaches the live stream.

    The final text is cleaned again in :func:`_strip_leading_speaker_heading`;
    this wrapper only prevents the wrong member's name from flashing in the
    live output while the first line is being generated.
    """

    def __init__(self, meeting: Meeting, agent: MeetingAgent, emit) -> None:
        self.meeting = meeting
        self.agent = agent
        self.emit = emit
        self._pending = ""
        self._decided = False

    def __call__(self, chunk: str) -> None:
        if not chunk:
            return
        if self._decided:
            self.emit(chunk)
            return
        self._pending += chunk
        if "\n" not in self._pending and len(self._pending) < 300:
            return
        self._release()

    def _release(self) -> None:
        self._decided = True
        text = self._pending
        self._pending = ""
        lines = text.splitlines(keepends=True)
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue
            if _is_speaker_heading_echo(self.meeting, line):
                index += 1
                continue
            break
        remaining = "".join(lines[index:])
        if remaining:
            self.emit(remaining)

    def flush(self) -> None:
        if not self._decided:
            self._release()
        elif self._pending:
            self.emit(self._pending)
            self._pending = ""


def _build_shared_header(
    meeting: Meeting, date_text: Optional[str] = None
) -> str:
    """Render the metadata block written at the top of the shared file."""
    stamp = date_text or datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"# {meeting.topic}\n\n"
    if meeting.description:
        header += f"**议题**: {meeting.description}\n\n"
    header += f"**日期**: {stamp}\n"
    header += (
        f"**参会**: {', '.join(a.name + '(' + a.runtime + ')' for a in meeting.agents)}\n\n"
    )
    header += "---\n\n"
    return header


def _split_shared_header(text: str) -> tuple[str, str]:
    """Split the file into (metadata block, discussion body)."""
    match = re.search(r"^## 第 \d+ 轮讨论\s*$", text, re.MULTILINE)
    if match is None:
        match = re.search(r"^### ", text, re.MULTILINE)
    if match is None:
        match = re.search(r"^## ", text, re.MULTILINE)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.start():]


def _sync_shared_header(meeting: Meeting, project_root: Path) -> None:
    """Create or **overwrite** the header without touching the discussion.

    Editing a meeting's topic/description used to leave the old header in the
    shared file (or, when the file was recreated, lose the whole discussion).
    This replaces just the leading block, in place, and keeps the original
    meeting date.
    """
    path = get_shared_file_path(meeting, project_root)
    with _shared_lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_build_shared_header(meeting), encoding="utf-8")
            return
        text = path.read_text(encoding="utf-8")
        old_header, body = _split_shared_header(text)
        date_match = re.search(r"^\*\*日期\*\*:\s*(.+)$", old_header, re.MULTILINE)
        date_text = date_match.group(1).strip() if date_match else None
        header = _build_shared_header(meeting, date_text=date_text)
        if not body:
            # Empty discussion: a file that starts with a title is ours to
            # regenerate; anything else is foreign content and is preserved.
            new_text = header if text.lstrip().startswith("# ") else header + text
        else:
            new_text = header + body
        if new_text == text:
            return
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        os.replace(tmp_path, path)


def _append_human_speech(
    meeting: Meeting,
    project_root: Path,
    speaker_name: str,
    round_num: int,
    text: str,
    interjection: bool = False,
) -> None:
    """Append a human message to the shared discussion file."""
    shared_path = get_shared_file_path(meeting, project_root)
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    with _shared_lock:
        existing = (
            shared_path.read_text(encoding="utf-8")
            if shared_path.exists()
            else ""
        )
        prefix = ""
        if not existing:
            prefix = f"# {meeting.topic}\n\n"
        if f"## 第 {round_num} 轮讨论" not in existing:
            prefix += f"\n## 第 {round_num} 轮讨论\n\n"
        suffix = " · 插话" if interjection else ""
        entry = (
            f"\n### {speaker_name} (human) - 第{round_num}轮{suffix}\n\n{text}\n\n"
        )
        with open(shared_path, "a", encoding="utf-8") as handle:
            handle.write(prefix + entry)


def _human_speaker(
    meeting: Meeting, agent_id: str = "", preferred_id: str = ""
) -> tuple[str, str]:
    """Resolve which human seat (or pseudo seat) is speaking."""
    agent: Optional[MeetingAgent] = None
    for candidate in (agent_id, preferred_id):
        if not candidate:
            continue
        match = next((a for a in meeting.agents if a.id == candidate), None)
        if match is not None and match.runtime == "human":
            agent = match
            break
    if agent is None:
        agent = next((a for a in meeting.agents if a.runtime == "human"), None)
    if agent is None:
        return "__human__", "人类参与者"
    return agent.id, agent.name or "人类参与者"


def _await_human_turn(
    meeting: Meeting,
    project_root: Path,
    agent: MeetingAgent,
    round_num: int,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    """Block until the human seat submits a message.  Returns True if spoken."""
    stop_event = stop_event or meeting._stop_event
    agent.status = AgentTurnStatus.awaiting_input.value
    agent.started_at = datetime.now(timezone.utc).isoformat()
    agent.error = ""
    _save_meeting(meeting, project_root)

    with meeting._human_cond:
        meeting._waiting_human = agent.id
        meeting._waiting_human_name = agent.name
        meeting._human_queue.clear()

    broadcast_meeting_event(meeting.id, "human_turn", {
        "meeting_id": meeting.id,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "round": round_num,
    })

    item: Optional[dict] = None
    try:
        with meeting._human_cond:
            while not stop_event.is_set():
                if meeting._human_queue:
                    item = meeting._human_queue.pop(0)
                    break
                meeting._human_cond.wait(timeout=0.5)
    finally:
        with meeting._human_cond:
            meeting._waiting_human = None
            meeting._waiting_human_name = ""

    agent.finished_at = datetime.now(timezone.utc).isoformat()
    if item is None:
        agent.status = AgentTurnStatus.skipped.value
        agent.error = "已停止"
        _save_meeting(meeting, project_root)
        return False
    if item.get("skip"):
        agent.status = AgentTurnStatus.skipped.value
        agent.error = "已跳过本轮"
        _save_meeting(meeting, project_root)
        broadcast_meeting_event(meeting.id, "human_skipped", {
            "agent_id": agent.id,
            "agent_name": agent.name,
            "round": round_num,
        })
        return False

    text = str(item.get("content") or "").strip()
    if not text:
        agent.status = AgentTurnStatus.skipped.value
        agent.error = "发言内容为空"
        _save_meeting(meeting, project_root)
        return False
    agent.output = text
    agent.status = AgentTurnStatus.completed.value
    agent.error = ""
    if round_num not in agent.rounds_completed:
        agent.rounds_completed.append(round_num)
    _save_meeting(meeting, project_root)
    return True


def submit_human_speech(
    meeting_id: str,
    project_root: Path,
    content: str = "",
    agent_id: str = "",
    name: str = "",
    skip: bool = False,
) -> dict:
    """Accept a human message at any time during a running meeting.

    The text is written to the shared file immediately, so every member whose
    turn has not started yet will read it.  When it is the human seat's turn,
    the message also releases the waiting turn.
    """
    meeting = get_meeting(meeting_id, project_root)
    if meeting is None:
        raise ValueError("会议未找到")
    if meeting.status != MeetingStatus.running.value:
        raise ValueError("会议未在运行中，请先继续会议")

    with meeting._human_cond:
        waiting = meeting._waiting_human
    speaker_id, speaker_name = _human_speaker(meeting, agent_id, waiting or "")
    if name.strip():
        speaker_name = name.strip()
    round_num = meeting.current_round
    text = (content or "").strip()

    with meeting._human_cond:
        is_waiting = waiting is not None and waiting == speaker_id
        if skip:
            if not is_waiting:
                raise ValueError("当前没有等待中的人类发言")
            meeting._human_queue.append({"skip": True})
            meeting._human_cond.notify_all()
        else:
            if not text:
                raise ValueError("发言内容不能为空")
            _append_human_speech(
                meeting,
                project_root,
                speaker_name,
                round_num,
                text,
                interjection=not is_waiting,
            )
            if is_waiting:
                meeting._human_queue.append({"skip": False, "content": text})
                meeting._human_cond.notify_all()

    payload = {
        "meeting_id": meeting.id,
        "agent_id": speaker_id,
        "agent_name": speaker_name,
        "round": round_num,
        "output": "" if skip else text,
        "waiting": is_waiting,
    }
    broadcast_meeting_event(
        meeting.id, "human_skipped" if skip else "human_message", payload
    )
    return payload


def _run_meeting(
    meeting: Meeting,
    project_root: Path,
    stop_event: Optional[threading.Event] = None,
    run_token: Optional[int] = None,
) -> None:
    from aqm.runtime.meeting_session import start_meeting_agent_session
    shared_path = get_shared_file_path(meeting, project_root)
    sessions: dict[int, Any] = {}
    shared_offsets: dict[int, int] = {}

    stop_event = stop_event or meeting._stop_event

    def stale() -> bool:
        """True when another run has taken over this meeting."""
        return run_token is not None and run_token != meeting._run_token

    if stale():
        return

    # On resume: reset running agents to waiting (they were interrupted)
    for agent in meeting.agents:
        if agent.status == AgentTurnStatus.running.value:
            agent.status = AgentTurnStatus.waiting.value

    is_resume = meeting.started_at is not None
    meeting.started_at = datetime.now(timezone.utc).isoformat()
    _save_meeting(meeting, project_root)

    broadcast_meeting_event(meeting.id, "meeting_started", {
        "meeting_id": meeting.id,
        "topic": meeting.topic,
        "agents": [a.name for a in meeting.agents],
        "rounds": meeting.rounds,
        "resumed": is_resume,
    })

    broadcast_meeting_event(meeting.id, "agents_starting", {
        "meeting_id": meeting.id,
        "agents": [a.name for a in meeting.agents],
    })

    # Create the file if needed, or overwrite only its metadata block so an
    # edited topic/description is reflected without duplicating the header.
    _sync_shared_header(meeting, project_root)

    try:
        if meeting.agents and not stop_event.is_set():
            with ThreadPoolExecutor(max_workers=min(len(meeting.agents), 8)) as executor:
                pending = {
                    executor.submit(
                        start_meeting_agent_session,
                        agent,
                        project_root,
                        stop_event,
                    ): i
                    for i, agent in enumerate(meeting.agents)
                    if agent.runtime != "human"
                }
                for future in as_completed(pending):
                    i = pending[future]
                    agent = meeting.agents[i]
                    try:
                        sessions[i] = future.result()
                        agent.resident = True
                        agent.error = ""
                        _save_meeting(meeting, project_root)
                        broadcast_meeting_event(meeting.id, "agent_ready", {
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "runtime": agent.runtime,
                        })
                    except InterruptedError:
                        agent.status = AgentTurnStatus.skipped.value
                        agent.error = "已停止"
                        _save_meeting(meeting, project_root)
                    except Exception as e:
                        agent.status = AgentTurnStatus.error.value
                        agent.error = str(e)
                        _save_meeting(meeting, project_root)
                        logger.error(
                            "Failed to start meeting agent '%s' (%s): %s",
                            agent.id,
                            agent.runtime,
                            e,
                        )
                        broadcast_meeting_event(meeting.id, "agent_error", {
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "error": agent.error,
                            "phase": "startup",
                        })

        for round_num in range(meeting.current_round, meeting.rounds + 1):
            if stop_event.is_set():
                meeting.status = MeetingStatus.stopped.value
                break
            if stale():
                break

            meeting.current_round = round_num
            _save_meeting(meeting, project_root)

            # Per-round accounting. Global agent.status is not reliable here:
            # it still holds the result of previous rounds, which used to make
            # the round summary show stale failures.
            round_errors: set[str] = set()
            round_skipped: set[str] = set()

            # Reset per-round statuses for agents who haven't finished this round yet
            for i, agent in enumerate(meeting.agents):
                if i in sessions and round_num not in agent.rounds_completed:
                    agent.status = AgentTurnStatus.waiting.value
                    agent.error = ""

            # Ensure this round's heading exists exactly once.
            with _shared_lock:
                file_text = (
                    shared_path.read_text(encoding="utf-8")
                    if shared_path.exists()
                    else ""
                )
                if f"## 第 {round_num} 轮讨论" not in file_text:
                    with open(shared_path, "a", encoding="utf-8") as f:
                        f.write(f"\n## 第 {round_num} 轮讨论\n\n")

            broadcast_meeting_event(meeting.id, "round_start", {
                "round": round_num,
            })

            for i, agent in enumerate(meeting.agents):
                if stop_event.is_set():
                    meeting.status = MeetingStatus.stopped.value
                    break
                if stale():
                    break

                meeting.current_agent_index = i

                # Skip agents who already spoke in this round (resume case)
                if round_num in agent.rounds_completed:
                    continue
                if agent.runtime == "human":
                    meeting.current_agent_index = i
                    if _await_human_turn(
                        meeting, project_root, agent, round_num, stop_event
                    ):
                        shared_offsets[i] = len(
                            shared_path.read_text(encoding="utf-8")
                        )
                    elif not stop_event.is_set():
                        round_skipped.add(agent.id)
                    continue
                if i not in sessions or not sessions[i].is_alive:
                    # The member's resident CLI died (timeout, crash, manual
                    # kill). Start a fresh one instead of silently failing the
                    # turn, otherwise every later round would be empty too.
                    try:
                        sessions[i] = start_meeting_agent_session(
                            agent, project_root, stop_event
                        )
                        agent.resident = True
                        agent.error = ""
                        broadcast_meeting_event(meeting.id, "agent_ready", {
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "runtime": agent.runtime,
                        })
                    except InterruptedError:
                        meeting.status = MeetingStatus.stopped.value
                        break
                    except Exception as e:
                        agent.status = AgentTurnStatus.error.value
                        agent.error = f"会话重启失败: {e}"
                        round_errors.add(agent.id)
                        _save_meeting(meeting, project_root)
                        broadcast_meeting_event(meeting.id, "agent_error", {
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "error": agent.error,
                            "round": round_num,
                        })
                        continue

                agent.status = AgentTurnStatus.running.value
                agent.started_at = datetime.now(timezone.utc).isoformat()
                agent.thinking = ""
                agent.actions = ""
                agent.error = ""
                _save_meeting(meeting, project_root)

                broadcast_meeting_event(meeting.id, "agent_start", {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "runtime": agent.runtime,
                    "role": agent.role,
                    "round": round_num,
                    "index": i,
                    "started_at": agent.started_at,
                })

                agent_timeout = _agent_timeout(meeting, agent)
                streamer = _MeetingStreamer(meeting, agent, round_num)

                try:
                    if not sessions[i].is_alive:
                        raise RuntimeError(
                            f"{agent.runtime} CLI session exited before this turn"
                        )

                    context = ""
                    if shared_path.exists():
                        text = shared_path.read_text(encoding="utf-8")
                        start = shared_offsets.get(i)
                        context = text if start is None or start > len(text) else text[start:]
                        if len(context) > 8000:
                            context = "...[前文省略]...\n" + context[-8000:]
                    empty_context = (
                        "（自上次发言后暂无新增记录）"
                        if i in shared_offsets
                        else "（无，你是第一个发言的）"
                    )

                    prompt = (
                        f"【项目背景】\n{meeting.description}\n\n"
                        f"【你的身份】你是「{agent.name}」（运行时 {agent.runtime}），"
                        f"角色：{agent.role or agent.name}。\n\n"
                        f"【你的角色】\n{agent.role or agent.name}\n\n"
                        f"【共享讨论记录】\n{context if context else empty_context}\n\n"
                        f"【当前轮次】第 {round_num} 轮\n"
                        f"【重要】历史记录里的其他成员都已经发过言，那些是他们的观点，"
                        f"不是你的。你是「{agent.name}」，不是任何其他成员。\n"
                        f"【任务】请根据你的角色和专长，基于共享讨论记录进行发言。"
                        f"请仔细阅读前序讨论，给出有深度的观点。\n"
                        f"【输出要求】只输出你自己的发言正文；不要复述系统提示、技能列表或工具说明；"
                        f"不要代替其他参会成员发言，不要生成或复述发给其他成员的提示词，"
                        f"也不要转述“某某的发言”。\n"
                        f"第一行不要写成员名、角色名或“第 N 轮”之类的标题，"
                        f"直接从你自己的观点写起。"
                    )

                    speech = _SpeechPrefixFilter(
                        meeting, agent, streamer.output
                    )
                    output = sessions[i].run(
                        prompt,
                        agent_timeout,
                        stop_event,
                        on_output=speech,
                        on_thinking=streamer.thinking,
                        on_action=streamer.action,
                        on_process=streamer.process,
                    )
                    speech.flush()
                    streamer.flush()
                    if not isinstance(output, str) or not output.strip():
                        # Some runtimes acknowledge the turn without returning
                        # the text they already streamed. Keep that speech
                        # instead of marking the member as empty and skipping.
                        output = streamer.accumulated("text")
                    if not isinstance(output, str) or not output.strip():
                        raise RuntimeError(
                            f"{agent.runtime} completed without producing a response"
                        )
                    output = _strip_leading_speaker_heading(meeting, agent, output)
                    agent.output = output
                    agent.thinking = (
                        getattr(sessions[i], "last_thinking", "") or ""
                    )
                    agent.actions = (
                        getattr(sessions[i], "last_actions", "") or ""
                    )
                    if not agent.thinking:
                        agent.thinking = streamer.accumulated("thinking") or (
                            streamer.accumulated("process")
                        )
                    if not agent.actions:
                        agent.actions = streamer.accumulated("action")
                    agent.status = AgentTurnStatus.completed.value
                    agent.error = ""
                    if round_num not in agent.rounds_completed:
                        agent.rounds_completed.append(round_num)

                    agent_header = f"\n### {agent.name} ({agent.runtime}) - 第{round_num}轮\n\n"
                    with _shared_lock:
                        with open(shared_path, "a", encoding="utf-8") as f:
                            f.write(agent_header + output + "\n\n")
                        shared_offsets[i] = len(
                            shared_path.read_text(encoding="utf-8")
                        )

                    _save_meeting(meeting, project_root)

                    broadcast_meeting_event(meeting.id, "agent_complete", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "runtime": agent.runtime,
                        "role": agent.role,
                        "round": round_num,
                        "index": i,
                        "output": output,
                        "output_length": len(output),
                        "thinking": agent.thinking,
                        "actions": agent.actions,
                    })

                except InterruptedError:
                    agent.status = AgentTurnStatus.skipped.value
                    agent.error = "已停止"
                    round_skipped.add(agent.id)
                    _save_meeting(meeting, project_root)
                    broadcast_meeting_event(meeting.id, "agent_error", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "error": "已停止",
                        "round": round_num,
                    })
                    break
                except subprocess.TimeoutExpired:
                    agent.status = AgentTurnStatus.error.value
                    agent.error = f"超时 ({agent_timeout}s)"
                    round_errors.add(agent.id)
                    _save_meeting(meeting, project_root)
                    broadcast_meeting_event(meeting.id, "agent_error", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "error": agent.error,
                        "round": round_num,
                    })
                except TimeoutError:
                    agent.status = AgentTurnStatus.error.value
                    agent.error = f"超时 ({agent_timeout}s)"
                    round_errors.add(agent.id)
                    _save_meeting(meeting, project_root)
                    broadcast_meeting_event(meeting.id, "agent_error", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "error": agent.error,
                        "round": round_num,
                    })
                except FileNotFoundError as e:
                    agent.status = AgentTurnStatus.error.value
                    agent.error = str(e)
                    round_errors.add(agent.id)
                    _save_meeting(meeting, project_root)
                    broadcast_meeting_event(meeting.id, "agent_error", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "error": agent.error,
                        "round": round_num,
                    })
                except Exception as e:
                    agent.status = AgentTurnStatus.error.value
                    agent.error = str(e)
                    round_errors.add(agent.id)
                    _save_meeting(meeting, project_root)
                    broadcast_meeting_event(meeting.id, "agent_error", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "error": agent.error,
                        "round": round_num,
                    })
                finally:
                    streamer.flush()
                    agent.finished_at = datetime.now(timezone.utc).isoformat()
                    if not agent.thinking:
                        agent.thinking = (
                            getattr(sessions[i], "last_thinking", "") or ""
                        ) or streamer.accumulated("thinking") or (
                            streamer.accumulated("process")
                        )
                    if not agent.actions:
                        agent.actions = (
                            getattr(sessions[i], "last_actions", "") or ""
                        ) or streamer.accumulated("action")
                    if not sessions[i].is_alive:
                        agent.resident = False

            completed_this_round = sum(
                round_num in agent.rounds_completed for agent in meeting.agents
            )
            interrupted = stop_event.is_set() or stale()
            if stale():
                break
            broadcast_meeting_event(meeting.id, "round_complete", {
                "round": round_num,
                "completed_agents": completed_this_round,
                "error_agents": len(round_errors),
                "skipped_agents": len(round_skipped),
                "interrupted": interrupted,
            })
            if interrupted:
                # Clicking stop is not a failed round. Do not overwrite the
                # user's "stopped" state with an error just because no member
                # got to speak before the interrupt.
                meeting.status = MeetingStatus.stopped.value
                break
            if completed_this_round == 0:
                # A human-only meeting where everyone skipped is a normal
                # finish, not a failure.
                if any(a.runtime != "human" for a in meeting.agents):
                    meeting.status = MeetingStatus.error.value
                    failed = [
                        f"{a.name}({a.runtime})"
                        + (f": {a.error}" if a.error else "")
                        for a in meeting.agents
                    ]
                    meeting.error = (
                        f"第 {round_num} 轮所有参会代理均未能发言，已提前停止后续轮次。"
                        + (" 失败详情 -> " + "；".join(failed) if failed else "")
                    )
                    logger.error(
                        "Meeting %s stopped after round %s had no successful agents",
                        meeting.id,
                        round_num,
                    )
                    break

        if stale():
            return
        if stop_event.is_set():
            meeting.status = MeetingStatus.stopped.value
        elif meeting.status != MeetingStatus.error.value:
            meeting.status = MeetingStatus.completed.value

    except Exception as e:
        if stale():
            return
        logger.error("Meeting %s failed: %s", meeting.id, e)
        meeting.status = MeetingStatus.error.value
        meeting.error = str(e)

    finally:
        for i, session in sessions.items():
            try:
                session.close()
            except Exception:
                logger.exception(
                    "Failed to close meeting agent '%s' (%s)",
                    meeting.agents[i].id,
                    meeting.agents[i].runtime,
                )
            if not stale():
                meeting.agents[i].resident = False
        if not stale():
            # Only the run that still owns the meeting may publish its result.
            meeting.finished_at = datetime.now(timezone.utc).isoformat()
            _save_meeting(meeting, project_root)
            broadcast_meeting_event(meeting.id, "meeting_complete", {
                "meeting_id": meeting.id,
                "status": meeting.status,
                "error": meeting.error,
                "shared_file": meeting.shared_file,
                "total_rounds": meeting.rounds,
                "completed_rounds": meeting.current_round,
            })
            if (
                meeting.status == MeetingStatus.completed.value
                and meeting.auto_requirements
            ):
                threading.Thread(
                    target=generate_requirements_bg,
                    args=(meeting.id, project_root),
                    daemon=True,
                ).start()


def start_meeting(meeting_id: str, project_root: Path) -> bool:
    meeting = get_meeting(meeting_id)
    if not meeting:
        return False
    if meeting.status == MeetingStatus.running.value:
        return False

    # If completed, restart from scratch
    if meeting.status == MeetingStatus.completed.value:
        meeting.current_round = 1
        meeting.current_agent_index = 0
        meeting.started_at = None
        meeting.finished_at = None
        meeting.error = ""
        for agent in meeting.agents:
            agent.status = AgentTurnStatus.waiting.value
            agent.started_at = None
            agent.finished_at = None
            agent.output = ""
            agent.thinking = ""
            agent.actions = ""
            agent.error = ""
            agent.rounds_completed = []
        shared_path = get_shared_file_path(meeting, project_root)
        if shared_path.exists():
            shared_path.unlink()

    # Each run gets its own stop event and token. A previous thread that is
    # still winding down can then never clear/observe the new run's state.
    meeting._stop_event = threading.Event()
    run_token = meeting._run_token = meeting._run_token + 1
    with meeting._human_cond:
        meeting._human_queue.clear()
        meeting._waiting_human = None
        meeting._waiting_human_name = ""
    meeting.status = MeetingStatus.running.value
    _save_meeting(meeting, project_root)
    meeting._thread = threading.Thread(
        target=_run_meeting,
        args=(meeting, project_root, meeting._stop_event, run_token),
        daemon=True,
    )
    meeting._thread.start()
    return True


def continue_meeting(
    meeting_id: str,
    project_root: Path,
    additional_rounds: int = 0,
) -> Optional[Meeting]:
    """Continue a completed/stopped/error meeting without wiping prior discussion.

    - For a completed meeting: advances to the next round and extends the total
      round count by ``additional_rounds`` (min 1).
    - For a stopped/error meeting: resumes from the current round; total rounds
      are unchanged.
    Returns the meeting, or None if not found / already running.
    """
    meeting = get_meeting(meeting_id, project_root)
    if not meeting or meeting.status == MeetingStatus.running.value:
        return None

    completed = meeting.status == MeetingStatus.completed.value
    if completed:
        # Move to the first round that hasn't fully run yet
        if all(
            meeting.current_round in a.rounds_completed
            for a in meeting.agents
            if a.rounds_completed
        ) or not meeting.agents:
            meeting.current_round += 1
        meeting.rounds = (
            max(meeting.rounds, meeting.current_round - 1)
            + max(1, int(additional_rounds or 0))
        )

    meeting.status = MeetingStatus.running.value
    meeting.error = ""
    meeting.finished_at = None
    for agent in meeting.agents:
        # Clear stale per-turn state so a resumed run never shows old outputs
        # or lingering errors for turns that have not run yet.
        if meeting.current_round not in agent.rounds_completed:
            agent.output = ""
            agent.thinking = ""
            agent.actions = ""
            agent.error = ""
            agent.status = AgentTurnStatus.waiting.value
            agent.started_at = None
            agent.finished_at = None
    meeting._stop_event = threading.Event()
    run_token = meeting._run_token = meeting._run_token + 1
    with meeting._human_cond:
        meeting._human_queue.clear()
        meeting._waiting_human = None
        meeting._waiting_human_name = ""
    _save_meeting(meeting, project_root)
    meeting._thread = threading.Thread(
        target=_run_meeting,
        args=(meeting, project_root, meeting._stop_event, run_token),
        daemon=True,
    )
    meeting._thread.start()
    return meeting


def stop_meeting(meeting_id: str, project_root: Optional[Path] = None) -> bool:
    meeting = get_meeting(meeting_id)
    if not meeting:
        return False
    if meeting.status != MeetingStatus.running.value:
        return False
    meeting._stop_event.set()
    with meeting._human_cond:
        meeting._human_cond.notify_all()
    # Save state immediately in case the thread hasn't flushed yet
    meeting.status = MeetingStatus.stopped.value
    meeting.finished_at = datetime.now(timezone.utc).isoformat()
    if project_root:
        _save_meeting(meeting, project_root)
    # Push the stop to the UI right away instead of waiting for the thread's
    # meeting_complete (which is delayed until the running subprocess exits).
    broadcast_meeting_event(meeting.id, "meeting_complete", {
        "meeting_id": meeting.id,
        "status": meeting.status,
        "shared_file": meeting.shared_file,
        "total_rounds": meeting.rounds,
        "completed_rounds": meeting.current_round,
    })
    return True


def get_shared_file_path(meeting: Meeting, project_root: Path) -> Path:
    """Resolve the discussion file used by one meeting.

    Every meeting keeps its own copy under ``.aqm/meetings/<id>/`` so two
    meetings that both use the default name ``meeting.md`` never share (and
    overwrite) the same file. An absolute path is respected as-is.
    """
    raw = (meeting.shared_file or "meeting.md").strip() or "meeting.md"
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    parts = [p for p in candidate.parts if p not in ("", ".", "..")]
    if not parts:
        parts = ["meeting.md"]
    return _meetings_dir(project_root) / meeting.id / Path(*parts)


def _legacy_shared_file_path(
    meeting: Meeting, project_root: Path
) -> Optional[Path]:
    """Pre-per-meeting location (project root) used by older versions."""
    raw = (meeting.shared_file or "meeting.md").strip() or "meeting.md"
    candidate = Path(raw)
    if candidate.is_absolute():
        return None
    return project_root / candidate


def _migrate_legacy_shared_file(meeting: Meeting, project_root: Path) -> None:
    """Copy an old root-level discussion file into this meeting's folder.

    The old layout stored every meeting's discussion in the same
    ``<project>/meeting.md``. Copying (not moving) leaves that file untouched
    while giving each meeting an isolated history from now on.
    """
    target = get_shared_file_path(meeting, project_root)
    legacy = _legacy_shared_file_path(meeting, project_root)
    if legacy is None or legacy == target:
        return
    if target.exists() or not legacy.is_file():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
        logger.warning(
            "Migrated shared file for meeting %s: %s -> %s",
            meeting.id,
            legacy,
            target,
        )
    except OSError as exc:  # pragma: no cover - migration best effort
        logger.warning(
            "Could not migrate shared file for meeting %s: %s", meeting.id, exc
        )


def get_shared_file_content(meeting: Meeting, project_root: Path) -> str:
    path = get_shared_file_path(meeting, project_root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def get_requirements_path(meeting: Meeting, project_root: Path) -> Path:
    """Deliverable produced by a meeting and consumed by the task pipeline."""
    return _meetings_dir(project_root) / meeting.id / "REQUIREMENTS.md"


def get_requirements_content(meeting: Meeting, project_root: Path) -> str:
    path = get_requirements_path(meeting, project_root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


REQUIREMENTS_TEMPLATE = """\
# 项目需求文档

> 来源会议：{topic}
> 生成时间：{created_at}

## 1. 背景与目标
## 2. 范围（包含 / 不包含）
## 3. 功能需求
## 4. 非功能需求
## 5. 验收标准
## 6. 未决问题
"""


def _requirements_agent(meeting: Meeting, agent_id: str = "") -> Optional[MeetingAgent]:
    """Pick the member that turns the discussion into REQUIREMENTS.md."""
    if agent_id:
        match = next((a for a in meeting.agents if a.id == agent_id), None)
        if match is not None and match.runtime != "human":
            return match
    for agent in meeting.agents:
        if agent.runtime == "human":
            continue
        role = (agent.role or "") + (agent.name or "")
        if any(word in role for word in ("主持", "会议主持", "host", "moderator")):
            return agent
    return next((a for a in meeting.agents if a.runtime != "human"), None)


def generate_requirements(
    meeting_id: str,
    project_root: Path,
    agent_id: str = "",
) -> str:
    """Ask the meeting host to distil the discussion into REQUIREMENTS.md."""
    from aqm.runtime.meeting_session import start_meeting_agent_session

    meeting = get_meeting(meeting_id, project_root)
    if meeting is None:
        raise ValueError("会议未找到")

    agent = _requirements_agent(meeting, agent_id)
    if agent is None:
        raise ValueError("会议没有可用于整理需求文档的成员")

    discussion = get_shared_file_content(meeting, project_root)
    if not discussion.strip():
        raise ValueError("会议讨论记录为空，无法生成需求文档")
    if len(discussion) > 60_000:
        discussion = (
            discussion[:10_000]
            + "\n\n…（中间讨论已省略）…\n\n"
            + discussion[-50_000:]
        )

    prompt = (
        f"你是本次会议的主持人（{agent.name}）。请把下面的会议讨论整理成一份"
        "结构化的项目需求文档。\n\n"
        f"会议主题：{meeting.topic}\n"
        f"会议目标：{meeting.description or '（未填写）'}\n\n"
        "输出要求：\n"
        "1. 只输出 Markdown 正文，不要解释你的整理过程，不要输出代码块围栏。\n"
        "2. 严格使用以下结构：\n"
        "   # 项目需求文档\n"
        "   ## 1. 背景与目标\n"
        "   ## 2. 范围（包含 / 不包含）\n"
        "   ## 3. 功能需求\n"
        "   ## 4. 非功能需求\n"
        "   ## 5. 验收标准\n"
        "   ## 6. 未决问题\n"
        "3. 功能需求按编号列出，每条都要可开发、可验收；"
        "无法从讨论中确定的内容放入“未决问题”。\n"
        "4. 保留讨论中已经达成的关键决策，不要自行发明需求。\n\n"
        "会议讨论记录：\n"
        "<<<\n"
        f"{discussion}\n"
        ">>>\n"
    )

    meeting._requirements_status = "running"
    meeting._requirements_error = ""
    broadcast_meeting_event(meeting.id, "requirements_started", {
        "meeting_id": meeting.id,
        "agent_id": agent.id,
        "agent_name": agent.name,
    })

    timeout = max(600, int(meeting.timeout_per_agent or 300))
    session = start_meeting_agent_session(agent, project_root)
    chunks: list[str] = []

    def _on_output(chunk: str) -> None:
        if chunk:
            chunks.append(chunk)
            broadcast_meeting_event(
                meeting.id, "requirements_progress", {"text": chunk}
            )

    try:
        text = session.run(
            prompt,
            timeout,
            threading.Event(),
            on_output=_on_output,
        )
    finally:
        try:
            session.close()
        except Exception:
            logger.exception("Failed to close requirements session")

    text = (text or "").strip() or "".join(chunks).strip()
    if text.startswith("```"):
        # Tolerate a fenced block even though the prompt forbids it.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("主持人未返回需求文档内容")

    path = get_requirements_path(meeting, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    meeting._requirements_status = "ready"
    broadcast_meeting_event(meeting.id, "requirements_ready", {
        "meeting_id": meeting.id,
        "path": str(path),
        "content": text,
    })
    return text


def generate_requirements_bg(
    meeting_id: str,
    project_root: Path,
    agent_id: str = "",
) -> None:
    meeting = get_meeting(meeting_id, project_root)
    try:
        generate_requirements(meeting_id, project_root, agent_id)
    except Exception as exc:
        logger.error("Requirements generation failed for %s: %s", meeting_id, exc)
        if meeting is not None:
            meeting._requirements_status = "error"
            meeting._requirements_error = str(exc)
        broadcast_meeting_event(meeting_id, "requirements_error", {
            "meeting_id": meeting_id,
            "error": str(exc),
        })


DEFAULT_AGENTS = [
    {
        "id": "claude",
        "name": "Claude",
        "runtime": "claude",
        "model": "sensenova-6.8-flash-lite",
        "role": "架构师",
        "system_prompt": (
            "你是Claude，一个负责系统架构设计的技术专家。\n"
            "你的专长：系统架构设计与技术选型、数据建模与API设计、可扩展性与性能规划、技术风险评估、跨平台方案规划。\n"
            "你的定位：从全局视角思考，关注架构合理性、技术可行性、长期可维护性。\n"
            "你的风格：严谨、有深度、关注系统性风险。"
        ),
    },
    {
        "id": "opencode",
        "name": "opencode",
        "runtime": "opencode",
        "model": "opencode/deepseek-v4.1-flash-free",
        "role": "代码实现",
        "system_prompt": (
            "你是opencode，一个负责核心代码实现的高级开发者。\n"
            "你的专长：核心算法实现、工具链集成、性能优化与工程实现、跨平台代码适配、AI模型调用与集成。\n"
            "你的定位：负责将设计转化为可运行的代码，关注实现细节、代码质量、运行效率。\n"
            "你的风格：务实、注重效率、关注代码可维护性。"
        ),
    },
    {
        "id": "cline",
        "name": "cline",
        "runtime": "cline",
        "model": "sensenova-6.8-flash-lite",
        "role": "UI设计",
        "system_prompt": (
            "你是cline，一个负责UI/UX设计的界面专家。\n"
            "你的专长：界面设计与交互设计、用户体验规划、可视化方案、跨平台UI适配、工具设计。\n"
            "你的定位：负责让产品好用、易用、美观，关注用户操作流程、交互效率、视觉体验。\n"
            "你的风格：注重细节、关注用户体验、追求简洁高效。"
        ),
    },
    {
        "id": "kilo",
        "name": "kilo",
        "runtime": "kilo",
        "model": "agnes/agnes-3.0-flash",
        "role": "审核",
        "system_prompt": (
            "你是kilo，一个负责代码审核和质量把控的审核官。\n"
            "你的专长：代码审核与质量把控、安全分析与风险评估、测试策略规划、架构合理性审查、性能瓶颈识别。\n"
            "你的定位：负责找出问题、识别风险、确保质量，关注边界情况、潜在缺陷、改进机会。\n"
            "你的风格：批判性思维、关注风险、追求完美。"
        ),
    },
]
