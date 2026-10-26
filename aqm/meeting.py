"""Meeting engine — multi-agent collaborative discussion with shared file.

Manages meeting lifecycle: configuration, execution, real-time streaming,
and shared file management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
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
    status: str = AgentTurnStatus.waiting.value
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    output: str = ""
    error: str = ""


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
    _thread: Optional[threading.Thread] = None
    _stop_event: threading.Event = field(default_factory=threading.Event)

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
                    "status": a.status,
                    "started_at": a.started_at,
                    "finished_at": a.finished_at,
                    "output_preview": a.output[:300] if a.output else "",
                    "error": a.error,
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
                    "status": a.status,
                    "started_at": a.started_at,
                    "finished_at": a.finished_at,
                    "output": a.output,
                    "error": a.error,
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
        )


# In-memory meeting store
_meetings: dict[str, Meeting] = {}
_meetings_lock = threading.Lock()

# SSE subscribers: meeting_id -> list of asyncio.Queue
_sse_subscribers: dict[str, list[asyncio.Queue]] = {}
_sse_lock = threading.Lock()


def _meetings_dir(project_root: Path) -> Path:
    return project_root / ".aqm" / "meetings"


def _save_meeting(meeting: Meeting, project_root: Path) -> None:
    """Save meeting to disk as .aqm/meetings/{id}/meta.json."""
    d = _meetings_dir(project_root) / meeting.id
    d.mkdir(parents=True, exist_ok=True)
    meta_path = d / "meta.json"
    meta_path.write_text(
        json.dumps(meeting.to_persist_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_meeting_from_disk(meeting_id: str, project_root: Path) -> Optional[Meeting]:
    meta_path = _meetings_dir(project_root) / meeting_id / "meta.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return Meeting.from_persist_dict(data)
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
        ) for a in agents],
        shared_file=shared_file,
        rounds=rounds,
        timeout_per_agent=timeout_per_agent,
    )
    with _meetings_lock:
        _meetings[meeting_id] = meeting
    if project_root:
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


def list_meetings(project_root: Optional[Path] = None) -> list[Meeting]:
    with _meetings_lock:
        meetings = list(_meetings.values())
    if project_root:
        d = _meetings_dir(project_root)
        if d.exists():
            for meeting_dir in d.iterdir():
                if not meeting_dir.is_dir():
                    continue
                mid = meeting_dir.name
                with _meetings_lock:
                    if mid in _meetings:
                        continue
                m = _load_meeting_from_disk(mid, project_root)
                if m:
                    with _meetings_lock:
                        _meetings[mid] = m
                    meetings.append(m)
    return sorted(meetings, key=lambda m: m.created_at, reverse=True)


def delete_meeting(meeting_id: str, project_root: Optional[Path] = None) -> bool:
    with _meetings_lock:
        meeting = _meetings.pop(meeting_id, None)
    if meeting and meeting._thread and meeting._thread.is_alive():
        return False
    if meeting is not None and project_root:
        _delete_meeting_from_disk(meeting_id, project_root)
    return meeting is not None


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
        cmd = _resolve_cmd("opencode") + ["run", prompt, "--auto", "--format", "json", "--dir", str(project_root)]
        if model:
            cmd.extend(["--model", model])
    elif runtime == "kilo":
        cmd = _resolve_cmd("kilo") + ["run", prompt, "--auto", "--format", "json", "--dir", str(project_root)]
        if model:
            cmd.extend(["--model", model])
    elif runtime == "cline":
        cmd = _resolve_cmd("cline") + [prompt, "--json", "--auto-approve", "--yolo", "-c", str(project_root)]
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


def _run_agent_cli(agent: MeetingAgent, prompt: str, project_root: Path, timeout: int) -> str:
    cmd = _build_cmd(agent.runtime, prompt, agent.system_prompt, agent.model, project_root)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(project_root),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        error_msg = stderr or f"process exited with code {result.returncode}"
        raise RuntimeError(f"{agent.runtime} CLI failed: {error_msg}")
    output = result.stdout.strip()
    if agent.runtime in ("opencode", "kilo"):
        from aqm.runtime.json_stream import JsonStreamParser
        parser = JsonStreamParser()
        for line in output.splitlines():
            parser.feed_line(line)
        output = parser.collected_text
    return output


def _run_meeting(meeting: Meeting, project_root: Path) -> None:
    shared_path = project_root / meeting.shared_file

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

    header = f"# {meeting.topic}\n\n"
    if meeting.description:
        header += f"**议题**: {meeting.description}\n\n"
    header += f"**日期**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    header += f"**参会**: {', '.join(a.name + '(' + a.runtime + ')' for a in meeting.agents)}\n\n"
    header += "---\n"

    try:
        for round_num in range(meeting.current_round, meeting.rounds + 1):
            if meeting._stop_event.is_set():
                meeting.status = MeetingStatus.stopped.value
                break

            meeting.current_round = round_num
            _save_meeting(meeting, project_root)

            # Write headers if needed
            if not shared_path.exists():
                with open(shared_path, "a", encoding="utf-8") as f:
                    f.write(header + f"\n## 第 {round_num} 轮讨论\n\n")
            else:
                file_text = shared_path.read_text(encoding="utf-8")
                if f"## 第 {round_num} 轮讨论" not in file_text:
                    with open(shared_path, "a", encoding="utf-8") as f:
                        f.write(f"\n## 第 {round_num} 轮讨论\n\n")

            broadcast_meeting_event(meeting.id, "round_start", {
                "round": round_num,
            })

            for i, agent in enumerate(meeting.agents):
                if meeting._stop_event.is_set():
                    meeting.status = MeetingStatus.stopped.value
                    break

                meeting.current_agent_index = i

                # Skip completed, error, or skipped agents on resume
                if agent.status in (
                    AgentTurnStatus.completed.value,
                    AgentTurnStatus.error.value,
                    AgentTurnStatus.skipped.value,
                ):
                    continue

                agent.status = AgentTurnStatus.running.value
                agent.started_at = datetime.now(timezone.utc).isoformat()
                _save_meeting(meeting, project_root)

                broadcast_meeting_event(meeting.id, "agent_start", {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "runtime": agent.runtime,
                    "role": agent.role,
                    "round": round_num,
                    "index": i,
                })

                try:
                    context = ""
                    if shared_path.exists():
                        text = shared_path.read_text(encoding="utf-8")
                        if len(text) > 8000:
                            context = "...[前文省略]...\n" + text[-8000:]
                        else:
                            context = text

                    prompt = (
                        f"【项目背景】\n{meeting.description}\n\n"
                        f"【共享讨论记录】\n{context if context else '（无，你是第一个发言的）'}\n\n"
                        f"【当前轮次】第 {round_num} 轮\n"
                        f"【任务】请根据你的角色和专长，基于共享讨论记录进行发言。"
                        f"请仔细阅读前序讨论，给出有深度的观点。"
                    )

                    output = _run_agent_cli(agent, prompt, project_root, meeting.timeout_per_agent)
                    agent.output = output
                    agent.status = AgentTurnStatus.completed.value

                    agent_header = f"\n### {agent.name} ({agent.runtime}) - 第{round_num}轮\n\n"
                    with open(shared_path, "a", encoding="utf-8") as f:
                        f.write(agent_header + output + "\n\n")

                    _save_meeting(meeting, project_root)

                    broadcast_meeting_event(meeting.id, "agent_complete", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "runtime": agent.runtime,
                        "role": agent.role,
                        "round": round_num,
                        "index": i,
                        "output_preview": output[:300],
                        "output_length": len(output),
                    })

                except subprocess.TimeoutExpired:
                    agent.status = AgentTurnStatus.error.value
                    agent.error = f"超时 ({meeting.timeout_per_agent}s)"
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
                    _save_meeting(meeting, project_root)
                    broadcast_meeting_event(meeting.id, "agent_error", {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "error": agent.error,
                        "round": round_num,
                    })
                finally:
                    agent.finished_at = datetime.now(timezone.utc).isoformat()

            broadcast_meeting_event(meeting.id, "round_complete", {
                "round": round_num,
                "completed_agents": sum(1 for a in meeting.agents if a.status == AgentTurnStatus.completed.value),
                "error_agents": sum(1 for a in meeting.agents if a.status == AgentTurnStatus.error.value),
            })

        if meeting._stop_event.is_set():
            meeting.status = MeetingStatus.stopped.value
        else:
            meeting.status = MeetingStatus.completed.value

    except Exception as e:
        logger.error("Meeting %s failed: %s", meeting.id, e)
        meeting.status = MeetingStatus.error.value
        meeting.error = str(e)

    finally:
        meeting.finished_at = datetime.now(timezone.utc).isoformat()
        _save_meeting(meeting, project_root)
        broadcast_meeting_event(meeting.id, "meeting_complete", {
            "meeting_id": meeting.id,
            "status": meeting.status,
            "shared_file": meeting.shared_file,
            "total_rounds": meeting.rounds,
            "completed_rounds": meeting.current_round,
        })


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
            agent.error = ""
        shared_path = project_root / meeting.shared_file
        if shared_path.exists():
            shared_path.unlink()

    meeting._stop_event.clear()
    meeting.status = MeetingStatus.running.value
    _save_meeting(meeting, project_root)
    meeting._thread = threading.Thread(
        target=_run_meeting,
        args=(meeting, project_root),
        daemon=True,
    )
    meeting._thread.start()
    return True


def stop_meeting(meeting_id: str, project_root: Optional[Path] = None) -> bool:
    meeting = get_meeting(meeting_id)
    if not meeting:
        return False
    if meeting.status != MeetingStatus.running.value:
        return False
    meeting._stop_event.set()
    # Save state immediately in case the thread hasn't flushed yet
    meeting.status = MeetingStatus.stopped.value
    meeting.finished_at = datetime.now(timezone.utc).isoformat()
    if project_root:
        _save_meeting(meeting, project_root)
    return True


def get_shared_file_path(meeting: Meeting, project_root: Path) -> Path:
    return project_root / meeting.shared_file


def get_shared_file_content(meeting: Meeting, project_root: Path) -> str:
    path = get_shared_file_path(meeting, project_root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


DEFAULT_AGENTS = [
    {
        "id": "claude",
        "name": "Claude",
        "runtime": "claude",
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
        "role": "审核",
        "system_prompt": (
            "你是kilo，一个负责代码审核和质量把控的审核官。\n"
            "你的专长：代码审核与质量把控、安全分析与风险评估、测试策略规划、架构合理性审查、性能瓶颈识别。\n"
            "你的定位：负责找出问题、识别风险、确保质量，关注边界情况、潜在缺陷、改进机会。\n"
            "你的风格：批判性思维、关注风险、追求完美。"
        ),
    },
]
