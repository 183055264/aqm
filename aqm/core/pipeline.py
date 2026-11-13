"""Pipeline — agent orchestration execution loop.

Tasks move between agents through queues,
pass through gates where approve/reject decisions are made,
and are forwarded to the next agent based on handoff conditions.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed

from aqm.core.agent import AgentDefinition, Handoff, load_agents
from aqm.core.chunks import (
    Chunk,
    ChunkManager,
    ChunkStatus,
    chunk_instructions,
    content_hash,
    parse_chunk_directives,
    remaining_instruction,
    resolve_chunk_settings,
    sanitize_chunk_directives,
)
from aqm.core.context import build_payload, build_prompt
from aqm.core.context_file import ContextFile
from aqm.core.interrupt import AgentInterrupted
from aqm.core.gate import (
    AbstractGate,
    GateResult,
    HumanGate,
    LLMGate,
)
from aqm.core.project import get_tasks_dir
from aqm.core.task import StageRecord, Task, TaskStatus
from aqm.queue.base import AbstractQueue
from aqm.runtime.base import AbstractRuntime
from aqm.runtime.claude import ClaudeCodeRuntime
from aqm.runtime.gemini import GeminiRuntime
from aqm.runtime.codex import CodexRuntime
from aqm.runtime.opencode import OpencodeRuntime
from aqm.runtime.kilo import KiloRuntime
from aqm.runtime.cline import ClineRuntime

from aqm.core.config import ProjectConfig

logger = logging.getLogger(__name__)

MAX_STAGES = 20  # Fallback; overridden by config.pipeline.max_stages

# Thread-safe set of task IDs that have been requested to cancel.
# Checked each iteration of the pipeline loop.
_cancelled_tasks: set[str] = set()
_cancel_lock = threading.Lock()
_active_stop_events: dict[str, threading.Event] = {}

# Paused tasks: the pipeline blocks at the next stage/turn boundary until the
# event is cleared.  The running member is not killed, so a pause takes effect
# when its current answer finishes.
_paused_tasks: dict[str, threading.Event] = {}
_pause_lock = threading.Lock()

# Human interjections are live directives: when the user types while a member
# is answering, that member's turn is interrupted, the note is appended, and
# the same member answers again.  Notes that arrive between stages are applied
# at the next stage boundary instead.
_interjections: dict[str, list[str]] = {}
_interjection_events: dict[str, threading.Event] = {}
_interjection_lock = threading.Lock()


def interjection_event(task_id: str) -> threading.Event:
    """Return the per-task event that interrupts the running member turn."""
    with _interjection_lock:
        return _interjection_events.setdefault(task_id, threading.Event())


def submit_interjection(task_id: str, text: str) -> int:
    """Queue a human note and wake the running member, if any.

    Returns the number of notes waiting to be drained.
    """
    text = (text or "").strip()
    if not text:
        return 0
    with _interjection_lock:
        bucket = _interjections.setdefault(task_id, [])
        bucket.append(text)
        _interjection_events.setdefault(task_id, threading.Event()).set()
        return len(bucket)


def drain_interjections(task_id: str) -> list[str]:
    with _interjection_lock:
        event = _interjection_events.get(task_id)
        if event is not None:
            event.clear()
        return _interjections.pop(task_id, [])


def clear_interjections(task_id: str) -> None:
    with _interjection_lock:
        _interjections.pop(task_id, None)
        event = _interjection_events.pop(task_id, None)
        if event is not None:
            event.clear()


def release_interjection_signal(task_id: str) -> None:
    """Drop the interrupt signal but keep undrained notes for a future resume."""
    with _interjection_lock:
        event = _interjection_events.pop(task_id, None)
        if event is not None:
            event.clear()


# ── Human directives ────────────────────────────────────────────────────
# Interjections are not one-shot: the user expects a correction ("use Chinese
# from now on") to keep applying to later stages and even after a restart.
# They are appended *after* the rendered agent prompt, because CLI runtimes
# (claude/kilo/opencode) inject their own English system prompt and often
# ignore instructions buried in the middle of a long user message.
DIRECTIVE_HEADER = (
    "\n\n"
    "════════ 人类指令（最高优先级）════════\n"
    "以下是人类在本次任务中直接下达的要求，从现在起持续有效。"
    "当它们与上文的任务说明、输出格式、示例或工具说明冲突时，一律以人类指令为准：\n"
)
DIRECTIVE_FOOTER = "\n════════ 人类指令结束 ════════"

_LANGUAGE_TOKEN = re.compile(
    r"(?:(?:使用|用|以|改成|切换成)\s*|\bin\s+)"
    r"(简体中文|繁体中文|繁體中文|中文|汉语|英文|英语|english|chinese)",
    re.IGNORECASE,
)
_REPLY_WORD = re.compile(
    r"回复|回答|答复|输出|回应|说话|表述|reply|respond|answer|output",
    re.IGNORECASE,
)
# Pure nudges ("继续", "go on") carry no instruction; repeating them in every
# later stage prompt only adds noise.
_NUDGE_ONLY = re.compile(
    r"^(?:继续|继续啊|快点|快些|加油|go\s+on|continue|keep\s+going)[!！。.，,\s]*$",
    re.IGNORECASE,
)


def is_pure_nudge(text: str) -> bool:
    """True when a note only urges the agents on and carries no instruction."""
    return bool(_NUDGE_ONLY.match((text or "").strip()))


def _accepts_keyword(func, name: str) -> bool:
    """True when *func* can be called with keyword argument *name*."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def detect_language_directive(notes: list[str]) -> str | None:
    """Return the reply language requested by human notes, if any."""
    for note in reversed(notes):
        if not note or not _REPLY_WORD.search(note):
            continue
        match = _LANGUAGE_TOKEN.search(note)
        if not match:
            continue
        token = match.group(1).lower()
        if token in ("繁体中文", "繁體中文"):
            return "繁体中文"
        if token in ("英文", "英语", "english"):
            return "英文"
        return "简体中文"
    return None


def language_directive_line(language: str) -> str:
    """Render the canonical language rule appended to every stage prompt."""
    return (
        f"- 回复语言：{language}。你的所有输出——包括思考过程、工具调用前后的说明、"
        f"进度汇报和最终答复——都必须使用{language}；不要中英文混杂"
        f"（代码、命令、路径、专有名词除外）。"
    )


def _normalize_deliverable_naming(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in ("chinese", "zh", "cn", "中文", "简体中文", "繁体中文", "繁體中文"):
        return "chinese"
    if value in ("english", "en", "英文", "英语", "英語"):
        return "english"
    return "auto"


def deliverable_naming_directive_line(naming: str) -> str:
    """Render the file-naming rule for final deliverables.

    Temporary/intermediate files are intentionally left unconstrained: build
    scripts, caches and scratch outputs are easier to keep in ASCII, while the
    documents a human opens should read naturally in the project language.
    """
    naming = _normalize_deliverable_naming(naming)
    if naming == "chinese":
        return (
            "- 交付物命名：最终交付给人类的文档、报告必须使用中文文件名"
            "（例如「项目报告.md」「交付说明.md」「测试报告.md」），"
            "不要使用 PROJECT_REPORT.md / FINAL_REPORT.md 之类纯英文名；"
            "临时脚本、日志、缓存、中间产物可以使用英文文件名。"
        )
    if naming == "english":
        return (
            "- Deliverable naming: use English filenames for final documents and "
            "reports (e.g. project-report.md); temporary scripts, logs and caches "
            "may use any language."
        )
    return ""


def build_directive_block(
    notes: list[str],
    language: str | None = None,
    deliverable_naming: str | None = None,
) -> str:
    """Build the trailing high-priority directive block for a prompt."""
    cleaned: list[str] = []
    for note in notes:
        text = (note or "").strip()
        if not text or _NUDGE_ONLY.match(text):
            continue
        if text not in cleaned:
            cleaned.append(text)
    # Keep prompt size bounded; the most recent directives matter most.
    cleaned = cleaned[-25:]
    lang = detect_language_directive(cleaned) or language
    naming = _normalize_deliverable_naming(deliverable_naming)
    if naming == "auto":
        # Follow the reply language unless the project pins a convention.
        if lang and "中文" in lang:
            naming = "chinese"
        elif lang and "英文" in lang:
            naming = "english"
        else:
            naming = ""
    naming_line = deliverable_naming_directive_line(naming)
    if not cleaned and not lang and not naming_line:
        return ""
    lines = [f"- {text}" for text in cleaned]
    if lang:
        lines.append(language_directive_line(lang))
    if naming_line:
        lines.append(naming_line)
    return DIRECTIVE_HEADER + "\n".join(lines) + DIRECTIVE_FOOTER


def cancel_task(task_id: str) -> None:
    """Request cancellation of a running task."""
    with _cancel_lock:
        _cancelled_tasks.add(task_id)
        stop_event = _active_stop_events.get(task_id)
    # A paused task must wake up so the loop can notice the cancellation.
    clear_pause(task_id)
    if stop_event:
        stop_event.set()


def is_cancelled(task_id: str) -> bool:
    """Check if a task has been requested to cancel."""
    with _cancel_lock:
        return task_id in _cancelled_tasks


def pause_task(task_id: str) -> None:
    """Pause a running task at its next stage/turn boundary."""
    with _pause_lock:
        _paused_tasks.setdefault(task_id, threading.Event()).set()


def resume_paused_task(task_id: str) -> None:
    """Release a pause so the pipeline can continue."""
    with _pause_lock:
        event = _paused_tasks.get(task_id)
    if event is not None:
        event.clear()


def is_paused(task_id: str) -> bool:
    with _pause_lock:
        event = _paused_tasks.get(task_id)
    return bool(event is not None and event.is_set())


def clear_pause(task_id: str) -> None:
    with _pause_lock:
        event = _paused_tasks.pop(task_id, None)
    if event is not None:
        event.clear()


def wait_while_paused(task_id: str, poll: float = 0.5) -> bool:
    """Block while *task_id* is paused.

    Returns ``False`` when the task was cancelled while waiting (or paused),
    so the caller can stop instead of resuming.
    """
    while is_paused(task_id):
        if is_cancelled(task_id):
            return False
        _time.sleep(poll)
    return not is_cancelled(task_id)


class Pipeline:
    """Agent pipeline orchestrator."""

    def __init__(
        self,
        agents: dict[str, AgentDefinition],
        queue: AbstractQueue,
        project_root: Path,
        anthropic_client=None,
        config: ProjectConfig | None = None,
        resident_agents: bool = False,
        on_resident_event=None,
    ) -> None:
        self.agents = agents
        self.queue = queue
        self.project_root = project_root
        self._anthropic_client = anthropic_client
        self._runtimes: dict[str, AbstractRuntime] = {}
        self.config = config or ProjectConfig()
        self.resident_agents = resident_agents
        # Default resident-session callback.  Restart/resume paths call
        # run_task() internally, so keeping the callback on the pipeline lets
        # those paths report "starting/ready" member states too.
        self.on_resident_event = on_resident_event
        self._resident_sessions: dict[str, dict[str, Any]] = {}
        self._resident_stop_events: dict[str, threading.Event] = {}

    @property
    def anthropic_client(self):
        """Lazy-initialize Anthropic client (used only for LLM gates)."""
        if self._anthropic_client is None:
            import anthropic

            self._anthropic_client = anthropic.Anthropic()
        return self._anthropic_client

    def _apply_directives(self, prompt: str, task: Task) -> str:
        """Append durable human directives (and reply language) to a prompt."""
        notes = list(task.metadata.get("interjections") or [])
        block = build_directive_block(
            notes,
            self.config.language,
            getattr(self.config, "deliverable_naming", "auto"),
        )
        return prompt + block if block else prompt

    def _get_runtime(self, agent: AgentDefinition) -> AbstractRuntime:
        """Return a runtime instance matching the agent's runtime type.

        For ``claude`` runtime, auto-selects between text-only and Claude Code
        mode: if the agent has ``mcp`` servers or ``cli_flags``, it
        runs in full Claude Code mode (tool access); otherwise text-only.
        """
        rt = agent.runtime

        t = self.config.timeouts
        if rt == "claude":
            if "claude" not in self._runtimes:
                self._runtimes["claude"] = ClaudeCodeRuntime(self.project_root, timeout=t.claude)
            return self._runtimes["claude"]

        if rt not in self._runtimes:
            if rt == "gemini":
                self._runtimes[rt] = GeminiRuntime(self.project_root, timeout=t.gemini)
            elif rt == "codex":
                self._runtimes[rt] = CodexRuntime(self.project_root, timeout=t.codex)
            elif rt == "opencode":
                self._runtimes[rt] = OpencodeRuntime(self.project_root, timeout=getattr(t, 'opencode', 600))
            elif rt == "kilo":
                self._runtimes[rt] = KiloRuntime(self.project_root, timeout=getattr(t, 'kilo', 600))
            elif rt == "cline":
                self._runtimes[rt] = ClineRuntime(self.project_root, timeout=getattr(t, 'cline', 600))
            else:
                raise ValueError(f"Unknown runtime: {rt}")
        return self._runtimes[rt]

    def _get_gate(self, agent: AgentDefinition) -> Optional[AbstractGate]:
        """Return a gate instance matching the agent's gate configuration."""
        if not agent.gate:
            return None
        if agent.gate.type == "llm":
            return LLMGate(agent.gate, self.anthropic_client, gate_defaults=self.config.gate)
        elif agent.gate.type == "human":
            return HumanGate()
        else:
            raise ValueError(f"Unknown gate type: {agent.gate.type}")

    def _get_context_file(self, task: Task) -> ContextFile:
        """Return the context file manager for the task."""
        tasks_dir = get_tasks_dir(self.project_root)
        task_dir = tasks_dir / task.id
        if not task.context_dir:
            task.context_dir = str(task_dir)
        return ContextFile(task_dir)

    # ------------------------------------------------------------------
    # Handoff condition evaluation
    # ------------------------------------------------------------------

    def _evaluate_condition(
        self,
        condition: str,
        gate_result: Optional[GateResult],
        agent_output: str,
    ) -> bool:
        """Evaluate a handoff condition."""
        if condition == "always":
            return True
        if condition == "on_approve":
            return gate_result is not None and gate_result.decision == "approved"
        if condition == "on_reject":
            return gate_result is not None and gate_result.decision == "rejected"
        if condition == "on_pass":
            return gate_result is None or gate_result.decision == "approved"
        if condition == "auto":
            # Always "matches" — actual target is resolved from agent output.
            return True

        # Expression conditions (e.g., "severity == critical")
        try:
            eq_match = re.match(
                r"(\w+)\s*==\s*[\"']?(\w+)[\"']?", condition
            )
            if eq_match:
                key, value = eq_match.groups()
                return bool(re.search(
                    r'\b' + re.escape(value) + r'\b',
                    agent_output, re.IGNORECASE,
                ))

            in_match = re.match(
                r"(\w+)\s+in\s+\[([^\]]+)\]", condition
            )
            if in_match:
                key, values_str = in_match.groups()
                values = [
                    v.strip().strip("\"'")
                    for v in values_str.split(",")
                ]
                return any(
                    re.search(r'\b' + re.escape(v) + r'\b', agent_output, re.IGNORECASE)
                    for v in values
                )
        except Exception:
            logger.warning(f"Condition evaluation failed: {condition}")

        return False

    def _parse_auto_handoff_targets(self, agent_output: str) -> list[str]:
        """Extract agent IDs from ``HANDOFF: id1, id2`` directives in agent output.

        The agent can include one or more lines like::

            HANDOFF: developer
            HANDOFF: developer, qa

        Returns a deduplicated list of agent IDs in order of appearance.
        """
        targets: list[str] = []
        for m in re.finditer(r"HANDOFF:\s*(.+)", agent_output, re.IGNORECASE):
            for part in m.group(1).split(","):
                t = part.strip()
                if t and t not in targets:
                    targets.append(t)
        return targets

    def _resolve_handoffs(
        self,
        agent: AgentDefinition,
        gate_result: Optional[GateResult],
        agent_output: str,
        input_text: str,
    ) -> list[tuple[str, str]]:
        """Evaluate handoff conditions and return a list of (target_agent_id, payload).

        Supports:
        - **auto**: targets parsed from ``HANDOFF: <id>`` in agent output
        - **fan-out**: comma-separated ``to`` field (e.g. ``"qa, docs"``)
        - **multi-match**: all matching handoff rules contribute targets
        """
        results: list[tuple[str, str]] = []
        seen: set[str] = set()

        for handoff in agent.handoffs:
            if not self._evaluate_condition(
                handoff.condition, gate_result, agent_output
            ):
                continue

            payload = build_payload(
                handoff.payload,
                output=agent_output,
                input_text=input_text,
                reject_reason=(
                    gate_result.reason if gate_result else ""
                ),
                gate_result=(
                    gate_result.decision if gate_result else ""
                ),
            )

            if handoff.condition == "auto":
                # Agent decides: parse HANDOFF directives from output
                auto_targets = self._parse_auto_handoff_targets(agent_output)
                if not auto_targets:
                    logger.warning(
                        "[Pipeline] condition=auto but no HANDOFF directive "
                        "found in agent output; skipping handoff."
                    )
                    continue
                for target in auto_targets:
                    if target in self.agents and target not in seen:
                        results.append((target, payload))
                        seen.add(target)
                    elif target not in self.agents:
                        logger.warning(
                            f"[Pipeline] HANDOFF target '{target}' "
                            f"does not exist; skipping."
                        )
            else:
                # Static or expression condition — expand comma-separated targets
                targets = [t.strip() for t in handoff.to.split(",")]
                for target in targets:
                    if target not in seen:
                        results.append((target, payload))
                        seen.add(target)

        return results

    def run_task(
        self,
        task: Task,
        start_agent_id: str,
        input_text: str | None = None,
        on_stage_complete=None,
        on_stage_start=None,
        on_output=None,
        on_thinking=None,
        on_human_input_request=None,
        on_tool=None,
        on_resident_event=None,
        on_interjection=None,
        on_stage_interrupted=None,
        on_chunk_event=None,
    ) -> Task:
        task.metadata.setdefault("project_dir", str(self.project_root.resolve()))
        # Remember when this task started writing files, so the completion
        # summary can list the generated artifacts.
        task.metadata.setdefault(
            "run_started_at", datetime.now(timezone.utc).isoformat()
        )
        resident_cb = on_resident_event or self.on_resident_event
        owner = task.id not in self._resident_sessions
        stop_event = threading.Event()
        if owner and self.resident_agents:
            with _cancel_lock:
                _active_stop_events[task.id] = stop_event
            if is_cancelled(task.id):
                stop_event.set()
            task.status = TaskStatus.in_progress
            self.queue.update(task)
            try:
                self._start_resident_sessions(task, stop_event, resident_cb)
            except Exception as exc:
                self._close_resident_sessions(task.id)
                if is_cancelled(task.id):
                    task.status = TaskStatus.cancelled
                    task.metadata["cancel_reason"] = "Cancelled during agent startup"
                    with _cancel_lock:
                        _cancelled_tasks.discard(task.id)
                else:
                    task.status = TaskStatus.failed
                    task.metadata["error"] = f"Could not start resident agent sessions: {exc}"
                self.queue.update(task)
                if resident_cb:
                    resident_cb(
                        "failed" if task.status == TaskStatus.failed else "cancelled",
                        {"error": str(exc)},
                    )
                with _cancel_lock:
                    _active_stop_events.pop(task.id, None)
                return task
        elif not owner:
            stop_event = self._resident_stop_events[task.id]

        if owner:
            self._resident_stop_events[task.id] = stop_event
        try:
            return self._run_task_impl(
                task,
                start_agent_id,
                input_text=input_text,
                on_stage_complete=on_stage_complete,
                on_stage_start=on_stage_start,
                on_output=on_output,
                on_thinking=on_thinking,
                on_human_input_request=on_human_input_request,
                on_tool=on_tool,
                on_interjection=on_interjection,
                on_stage_interrupted=on_stage_interrupted,
                on_chunk_event=on_chunk_event,
            )
        finally:
            if owner:
                self._close_resident_sessions(task.id)
                self._resident_stop_events.pop(task.id, None)
                with _cancel_lock:
                    _active_stop_events.pop(task.id, None)
                # Keep the pause alive while waiting at a human gate: approval
                # resumes the pipeline and it must still block until unpaused.
                if task.status in (
                    TaskStatus.completed,
                    TaskStatus.failed,
                    TaskStatus.cancelled,
                ):
                    clear_pause(task.id)
                    task.metadata.pop("paused", None)
                release_interjection_signal(task.id)

    def _start_resident_sessions(self, task: Task, stop_event, on_resident_event=None) -> None:
        from aqm.runtime.meeting_session import start_meeting_agent_session

        agents = [agent for agent in self.agents.values() if agent.type == "agent"]
        if not agents:
            return
        if on_resident_event:
            on_resident_event("starting", {"agents": [agent.id for agent in agents]})
        sessions: dict[str, Any] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(agents)) as executor:
            futures = {
                executor.submit(
                    start_meeting_agent_session,
                    agent.model_copy(update={"system_prompt": ""}),
                    self.project_root,
                    stop_event,
                ): agent
                for agent in agents
            }
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    session = future.result()
                    sessions[agent.id] = session
                    if on_resident_event:
                        on_resident_event("ready", {
                            "agent_id": agent.id,
                            "name": agent.name or agent.id,
                            "runtime": agent.runtime,
                        })
                except Exception as exc:
                    errors.append(f"{agent.id} ({agent.runtime}): {exc}")
                    stop_event.set()
        if errors:
            for session in sessions.values():
                session.close()
            raise RuntimeError("; ".join(errors))
        self._resident_sessions[task.id] = sessions

    def _close_resident_sessions(self, task_id: str) -> None:
        sessions = self._resident_sessions.pop(task_id, {})
        for agent_id, session in sessions.items():
            try:
                session.close()
            except Exception:
                logger.exception("Could not close resident session for agent '%s'", agent_id)

    def _restart_resident_session(self, task: Task, agent_id: str) -> None:
        """Replace one member's session after its turn was interrupted.

        The old process may still be mid-tool-call after a human interjection,
        so it cannot be reused for the follow-up answer.  The other members'
        sessions are untouched.
        """
        sessions = self._resident_sessions.get(task.id)
        if sessions is None:
            return
        old = sessions.pop(agent_id, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                logger.exception(
                    "Could not close interrupted session for agent '%s'", agent_id
                )
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        from aqm.runtime.meeting_session import start_meeting_agent_session

        stop_event = self._resident_stop_events.get(task.id)
        if stop_event is None:
            stop_event = threading.Event()
            self._resident_stop_events[task.id] = stop_event
        # A failed restart leaves no entry behind: the next turn falls back to a
        # one-shot runtime call instead of reusing a dead session.
        sessions[agent_id] = start_meeting_agent_session(
            agent.model_copy(update={"system_prompt": ""}),
            self.project_root,
            stop_event,
        )

    def _execute_agent(
        self,
        prompt: str,
        agent: AgentDefinition,
        task: Task,
        on_output=None,
        on_thinking=None,
        on_tool=None,
        interruptible: bool = True,
    ) -> str:
        sessions = self._resident_sessions.get(task.id)
        session = sessions.get(agent.id) if sessions else None
        if session is not None and not session.is_alive:
            # The member's CLI process died (idle timeout, crash, runtime
            # restart).  A retry used to fail with a bare "session exited" that
            # hid the real cause; recreate the session and let the retry run
            # instead.
            detail = ""
            try:
                detail = session.exit_detail()
            except Exception:  # pragma: no cover - defensive
                detail = ""
            logger.warning(
                "[Pipeline] resident session for agent '%s' exited%s; recreating",
                agent.id,
                f" ({detail})" if detail else "",
            )
            try:
                self._restart_resident_session(task, agent.id)
            except Exception as exc:
                logger.exception(
                    "[Pipeline] could not recreate resident session for agent '%s'",
                    agent.id,
                )
                raise RuntimeError(
                    f"Resident session for agent '{agent.id}' exited and could "
                    f"not be recreated: {exc}"
                ) from exc
            sessions = self._resident_sessions.get(task.id) or {}
            session = sessions.get(agent.id)
            if session is None:
                raise RuntimeError(
                    f"Resident session for agent '{agent.id}' exited and could "
                    "not be recreated"
                )
        if session is not None:
            timeout = getattr(self.config.timeouts, agent.runtime or "", 600)

            def _action(line: str) -> None:
                if on_tool:
                    on_tool("action", {"text": line})

            def _process(chunk: str) -> None:
                if on_tool:
                    on_tool("process", {"text": chunk})

            kwargs = {
                "on_output": on_output,
                "on_thinking": on_thinking,
                "on_action": _action,
                "on_process": _process,
            }
            if _accepts_keyword(session.run, "interrupt_event"):
                kwargs["interrupt_event"] = (
                    interjection_event(task.id) if interruptible else None
                )
            return session.run(
                prompt,
                timeout,
                self._resident_stop_events[task.id],
                **kwargs,
            )
        return self._get_runtime(agent).run(
            prompt,
            agent,
            task,
            on_output=on_output,
            on_thinking=on_thinking,
            on_tool=on_tool,
        )

    # ── Work blocks (chunks) ──────────────────────────────────────────

    def _chunk_settings(self, agent: AgentDefinition | None = None) -> dict:
        """Resolve project settings with optional per-agent overrides."""
        raw = dict(self.config.chunks or {})
        if agent is not None and getattr(agent, "chunks", None) is not None:
            override = agent.chunks
            data = (
                override.model_dump()
                if hasattr(override, "model_dump")
                else dict(override)
            )
            for key in (
                "enabled", "auto_split", "auto_instructions", "enforce",
                "retry_limit", "owner_filter", "auto_claim", "max_chunks",
            ):
                if key in data and data[key] is not None:
                    raw[key] = data[key]
        return resolve_chunk_settings(raw)

    def _chunk_manager(self, task: Task) -> ChunkManager:
        return ChunkManager(get_tasks_dir(self.project_root) / task.id)

    @staticmethod
    def _chunk_emit(callback, event: str, data: dict) -> None:
        if not callback:
            return
        try:
            callback(event, data)
        except Exception:  # noqa: BLE001 - UI callbacks must never break a run
            logger.exception("[Chunk] callback failed for event %s", event)

    def _update_chunk_metadata(self, task: Task, mgr: ChunkManager) -> dict:
        progress = mgr.progress()
        task.metadata["chunks_total"] = progress["total"]
        task.metadata["chunks_done"] = progress["done"]
        task.metadata["chunks_percent"] = progress["percent"]
        return progress

    def _release_chunks_on_finish(self, task: Task) -> None:
        """Clear stale in-progress blocks when the pipeline stops.

        A run can end without the member emitting ``CHUNK_DONE`` (interrupted
        turn, gateway/approval stop, cancellation).  Keeping the block marked
        "in progress" after nothing is running makes the work-block panel lie;
        return those blocks to pending so the state matches reality.
        """
        try:
            mgr = self._chunk_manager(task)
            released = mgr.release_in_progress()
            if released:
                self._update_chunk_metadata(task, mgr)
                logger.info(
                    "[Chunk] %s released %d stale in-progress block(s) on finish",
                    task.id, len(released),
                )
        except Exception:  # noqa: BLE001 - bookkeeping must not break a finish
            logger.debug("[Chunk] release on finish failed for %s", task.id)

    def _ensure_chunks(self, task: Task, on_chunk_event=None) -> bool:
        """Create the chunk list for *task* if it does not exist yet.

        Order: existing list → YAML ``initial`` → automatic split from the
        requirements document.  Returns True when chunks are available.
        """
        settings = resolve_chunk_settings(self.config.chunks or {})
        if not settings.get("enabled", True):
            return False

        mgr = self._chunk_manager(task)
        if mgr.load().chunks:
            self._update_chunk_metadata(task, mgr)
            return True

        # 1) Seed from pipeline YAML.
        initial: list[dict] = []
        for agent in self.agents.values():
            cfg = getattr(agent, "chunks", None)
            if cfg is None:
                continue
            raw_initial = getattr(cfg, "initial", None) or []
            for item in raw_initial:
                if isinstance(item, dict):
                    initial.append({**item, "owner": item.get("owner") or agent.id})
                else:
                    initial.append({"description": str(item), "owner": agent.id})
        project_initial = (self.config.chunks or {}).get("initial") or []
        for item in project_initial:
            initial.append(item if isinstance(item, dict) else {"description": str(item)})
        if initial:
            mgr.init_from_config(initial)
            progress = self._update_chunk_metadata(task, mgr)
            self._chunk_emit(on_chunk_event, "chunk_update", {
                "action": "seed",
                "progress": progress,
            })
            return True

        if not settings.get("auto_split", True):
            return False
        # A pre-run split already decided this task cannot be split (short
        # description or model failure); do not pay for a second attempt.
        if str(task.metadata.get("_chunk_split") or "") in ("failed", "skipped"):
            return False

        # 2) Automatic split from the requirements document.
        created = self.split_task_chunks(
            task,
            replace=False,
            keep_done=False,
            on_chunk_event=on_chunk_event,
        )
        if not created:
            if settings.get("auto_split_required"):
                raise RuntimeError(
                    "Automatic chunk split failed (auto_split_required=true)"
                )
            return False
        self._update_chunk_metadata(task, mgr)
        return True

    def _plan_chunks_for_task(self, task: Task, on_chunk_event=None):
        """Run the model planner. Returns a dict or None when not applicable."""
        from aqm.core.chunk_planner import (
            find_requirements,
            plan_chunks,
            truncate_document,
        )

        settings = resolve_chunk_settings(self.config.chunks or {})
        text, source, source_hash = find_requirements(task, self.project_root)
        minimum = int(settings.get("min_description_chars", 800))
        if len(text.strip()) < minimum:
            logger.info(
                "[Chunk] %s has no requirements document long enough to split "
                "(%d < %d chars)", task.id, len(text.strip()), minimum,
            )
            task.metadata["_chunk_split"] = "skipped"
            return None

        document = truncate_document(
            text, int(settings.get("requirements_max_chars", 24000))
        )
        splitter = settings.get("splitter") or {}
        entry_agent = self.agents.get(getattr(task, "current_agent_id", "")) or next(
            iter(self.agents.values()), None
        )
        runtime_name = str(
            splitter.get("runtime") or getattr(entry_agent, "runtime", "") or "claude"
        )
        model = str(splitter.get("model") or "")
        max_chunks = int(settings.get("max_chunks", 30))

        task.metadata["_chunk_split"] = "running"
        task.metadata["_chunk_source"] = source
        self._chunk_emit(on_chunk_event, "chunk_split_started", {
            "source": source,
            "runtime": runtime_name,
            "model": model,
            "max_chunks": max_chunks,
        })

        try:
            runtime_probe = AgentDefinition(
                id="_chunk_planner_probe",
                runtime=runtime_name,
                system_prompt="",
            )
            runtime = self._get_runtime(runtime_probe)
            planned = plan_chunks(
                project_root=self.project_root,
                task=task,
                requirements=document,
                agents=self.agents,
                runtime=runtime,
                runtime_name=runtime_name,
                model=model,
                max_chunks=max_chunks,
                language=self.config.language or "",
            )
        except Exception as exc:  # noqa: BLE001 - planning is best effort
            logger.warning("[Chunk] %s automatic split failed: %s", task.id, exc)
            planned = []

        if not planned:
            task.metadata["_chunk_split"] = "failed"
            self._chunk_emit(on_chunk_event, "chunk_split_failed", {
                "source": source,
                "error": "模型没有返回可用的工作块",
            })
            return None

        return {
            "planned": planned,
            "source": source,
            "source_hash": source_hash,
            "runtime_name": runtime_name,
            "model": model,
        }

    def split_task_chunks(
        self,
        task: Task,
        *,
        replace: bool = True,
        keep_done: bool = True,
        on_chunk_event=None,
        force: bool = False,
    ) -> list[Chunk]:
        """(Re)generate chunks for *task* from its requirements document."""
        mgr = self._chunk_manager(task)
        if not force and mgr.load().chunks and not replace:
            return mgr.load().chunks

        result = self._plan_chunks_for_task(task, on_chunk_event)
        if result is None:
            return []

        planned = result["planned"]
        if replace:
            created = mgr.merge_auto(
                [item.model_dump() for item in planned], keep_done=keep_done
            )
        else:
            created = [
                mgr.add(
                    item.title,
                    created_by="chunk_planner",
                    owner=item.owner,
                    phase=item.phase,
                    source="auto",
                    note=item.detail,
                )
                for item in planned
            ]
        mgr.set_generation_meta(
            generated_from=result["source"],
            source_hash=result["source_hash"],
            splitter_model=result["model"] or result["runtime_name"],
        )
        progress = self._update_chunk_metadata(task, mgr)
        task.metadata["_chunk_split"] = "done"
        self._chunk_emit(on_chunk_event, "chunk_split_done", {
            "source": result["source"],
            "count": len(created),
            "chunks": [c.model_dump(mode="json") for c in created],
            "progress": progress,
        })
        logger.info(
            "[Chunk] %s split requirements into %d chunks (%s)",
            task.id, len(created), result["source"],
        )
        return created


    def _chunk_focus_block(
        self, agent: AgentDefinition, mgr: ChunkManager, task: Task
    ) -> str:
        """High-priority instruction for a chunk the human asked to run now."""
        focus = task.metadata.get("_chunk_focus")
        if not isinstance(focus, dict):
            return ""
        chunk_id = str(focus.get("chunk_id") or "")
        if not chunk_id:
            return ""
        chunk = mgr.get(chunk_id)
        if chunk is None or chunk.status == ChunkStatus.done:
            task.metadata.pop("_chunk_focus", None)
            return ""
        target = str(focus.get("agent_id") or "")
        if target and target != agent.id:
            return ""
        description = chunk.description.replace("\n", " ").strip()
        return (
            "\n\n════════ 人类指定优先处理 ════════\n"
            f"请优先完成工作块 {chunk_id}：{description}\n"
            f"完成后立即输出 CHUNK_DONE: {chunk_id}，再继续其它工作块。\n"
            "════════ 优先处理说明结束 ════════"
        )

    def _chunk_prompt_suffix(
        self, agent: AgentDefinition, mgr: ChunkManager, settings: dict,
        include_summary: bool = True,
        task: Task | None = None,
    ) -> str:
        """Prompt block listing this member's open chunks and the protocol."""
        text = ""
        if include_summary:
            owner = agent.id if settings.get("owner_filter", True) else None
            summary = mgr.summary(owner=owner)
            text += f"\n\n【工作块清单】\n{summary}"
        if settings.get("auto_instructions", True):
            text += chunk_instructions()
        if task is not None:
            text += self._chunk_focus_block(agent, mgr, task)
        return text

    def _chunk_claim(
        self, agent: AgentDefinition, mgr: ChunkManager, settings: dict,
        on_chunk_event=None,
    ) -> Chunk | None:
        if not settings.get("auto_claim", True):
            return None
        # A new stage is starting: anything still marked in-progress belongs to
        # an interrupted previous turn/run, not to a live worker.  Release it
        # first so at most one block is ever "in progress" per task.
        for stale in mgr.release_in_progress():
            self._chunk_emit(on_chunk_event, "chunk_update", {
                "action": "release",
                "agent_id": agent.id,
                "chunk_id": stale.id,
                "chunk": stale.model_dump(mode="json"),
                "reason": "上一阶段未完成，已释放为待处理",
                "progress": mgr.progress(),
            })
        chunk = mgr.claim_next(agent.id)
        if chunk is not None:
            self._chunk_emit(on_chunk_event, "chunk_update", {
                "action": "start",
                "agent_id": agent.id,
                "chunk_id": chunk.id,
                "chunk": chunk.model_dump(mode="json"),
                "progress": mgr.progress(),
            })
        return chunk

    def _chunk_pending_for(
        self, agent: AgentDefinition, mgr: ChunkManager, settings: dict
    ) -> list[Chunk]:
        """Chunks that still block this stage from finishing."""
        chunks = mgr.load().chunks
        if settings.get("owner_filter", True):
            return [
                c for c in chunks
                if c.owner == agent.id and c.status.value != "done"
            ]
        return [c for c in chunks if c.status.value != "done"]

    def _take_pending_notes(self, task: Task) -> list[str]:
        """Drain notes submitted from the web UI (live buffer + persisted)."""
        notes = drain_interjections(task.id)
        persisted = task.metadata.pop("pending_interjections", []) or []
        if persisted:
            merged = list(persisted)
            for note in notes:
                if note not in merged:
                    merged.append(note)
            notes = merged
        return notes

    def _apply_human_notes(
        self,
        task: Task,
        notes: list[str],
        agent_id: str,
        stage_number: int,
        on_interjection=None,
    ) -> list[str]:
        """Persist notes as durable directives and announce them once."""
        if not notes:
            return []
        durable = task.metadata.setdefault("interjections", [])
        added = [note for note in notes if note not in durable]
        if added:
            durable.extend(added)
        log = task.metadata.setdefault("interjection_log", [])
        applied_at = datetime.now(timezone.utc).isoformat()
        for note in notes:
            log.append({
                "text": note,
                "agent_id": agent_id,
                "stage_number": stage_number,
                "at": applied_at,
            })
        self.queue.update(task)
        logger.info(
            "[Pipeline] %s applied %d interjection(s) to agent '%s' (stage %s)",
            task.id, len(notes), agent_id, stage_number,
        )
        if on_interjection:
            on_interjection(task, notes, agent_id)
        return notes

    @staticmethod
    def _interjection_instruction(notes: list[str]) -> str:
        lines = "\n".join(f"- {note}" for note in notes if note.strip())
        return (
            "\n\n════════ 人类在你回答期间插话（立即执行）════════\n"
            f"{lines}\n"
            "上一轮回答已被打断。请把上面的插话当作对当前任务的追加指令，"
            "立即按新要求重新给出本轮完整答复；已经完成的工作不要重复铺垫，"
            "直接给出修正后的最终结果。\n"
            "════════ 插话结束 ════════"
        )

    def _run_task_impl(
        self,
        task: Task,
        start_agent_id: str,
        input_text: str | None = None,
        on_stage_complete=None,
        on_stage_start=None,
        on_output=None,
        on_thinking=None,
        on_human_input_request=None,
        on_tool=None,
        on_interjection=None,
        on_stage_interrupted=None,
        on_chunk_event=None,
    ) -> Task:
        """Run a task through the pipeline.

        Args:
            task: The task to run
            start_agent_id: Starting agent ID
            input_text: Input to pass to the first agent (uses task.description if None)
            on_stage_complete: Stage completion callback (task, stage_record)
            on_stage_start: Stage start callback (task, agent_id, stage_number)
            on_output: Output line callback (line_text) for streaming
            on_thinking: Thinking line callback (line_text) for streaming thinking
            on_human_input_request: Callback (task, agent_id, questions) when human input needed
            on_tool: Tool use event callback (event_type, data_dict) for streaming tool use
            on_interjection: Callback (task, notes, agent_id) when human notes are applied
            on_stage_interrupted: Callback (task, agent_id, stage_number, notes)
                when a live note makes the running member answer again

        Returns:
            The Task in completed (or gate-awaiting) state
        """
        current_input = input_text or task.description
        current_agent_id = start_agent_id

        ctx_file = self._get_context_file(task)
        has_chunks = self._ensure_chunks(task, on_chunk_event)
        chunk_mgr = self._chunk_manager(task) if has_chunks else None

        # Track per-agent gate rejection counts to prevent infinite reject loops.
        # Counts are reset when a different agent runs (so re-visiting an agent
        # after other work starts a fresh rejection budget).
        reject_counts: dict[str, int] = {}
        _prev_agent_id: str | None = None

        max_stages = self.config.pipeline.get("max_stages", MAX_STAGES)
        while task.next_stage_number <= max_stages:
            # Persist the checkpoint *before* the pause gate: a task paused at a
            # stage boundary blocks here, and if the server is restarted while
            # paused, auto-resume must still know which stage was about to run.
            task.metadata["_checkpoint_stage"] = task.next_stage_number
            task.metadata["_checkpoint_agent_id"] = current_agent_id
            self.queue.update(task)

            # Check for cancellation (a paused task wakes up when cancelled)
            if is_cancelled(task.id) or not wait_while_paused(task.id):
                with _cancel_lock:
                    _cancelled_tasks.discard(task.id)
                clear_pause(task.id)
                task.status = TaskStatus.cancelled
                task.metadata["cancel_reason"] = "Cancelled by user"
                task.metadata.pop("paused", None)
                self._release_chunks_on_finish(task)
                self.queue.update(task)
                logger.info(f"[Pipeline] {task.id} cancelled by user")
                return task

            # The persisted flag is the source of truth for the UI; once the
            # user clicks 继续, drop it from our copy before the next update so
            # a blocked run cannot resurrect the stale pause.
            if not is_paused(task.id):
                task.metadata.pop("paused", None)

            # Notes submitted between stages take effect at this boundary.
            notes = self._take_pending_notes(task)
            if notes:
                self._apply_human_notes(
                    task,
                    notes,
                    current_agent_id,
                    task.next_stage_number,
                    on_interjection=on_interjection,
                )

            if current_agent_id not in self.agents:
                raise ValueError(
                    f"Agent '{current_agent_id}' is not defined."
                )

            agent = self.agents[current_agent_id]
            # Reset reject counter when agent changes (fresh budget on re-visit)
            if current_agent_id != _prev_agent_id:
                reject_counts.pop(current_agent_id, None)
                _prev_agent_id = current_agent_id
            task.current_agent_id = current_agent_id
            task.status = TaskStatus.in_progress
            # Remember where we are: a server restart, or a manual restart of a
            # stalled task, should resume this stage instead of replaying the
            # previous one (the completed stage list lags by one).
            task.metadata["_checkpoint_stage"] = task.next_stage_number
            task.metadata["_checkpoint_agent_id"] = current_agent_id
            # Exposed to the console so a page reloaded mid-stage can render the
            # member's block and start its timer from the real start time.
            task.metadata["_stage_started_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self.queue.update(task)

            # ── Session node: delegate to conversation loop ──
            if agent.type == "session":
                logger.info(
                    f"[Pipeline] {task.id} -> session '{agent.id}' "
                    f"(participants: {agent.participants})"
                )
                ctx_file.snapshot_before_stage(task.next_stage_number)
                try:
                    output = self._run_session(
                        session=agent,
                        task=task,
                        input_text=current_input,
                        ctx_file=ctx_file,
                        on_turn_start=on_stage_start,
                        on_turn_complete=on_stage_complete,
                        on_output=on_output,
                        on_thinking=on_thinking,
                        on_tool=on_tool,
                        on_chunk_event=on_chunk_event,
                    )
                    # Record the session as a single stage
                    stage = StageRecord(
                        stage_number=task.next_stage_number,
                        agent_id=agent.id,
                        task_name="session",
                        input_text=current_input,
                        output_text=output,
                        finished_at=datetime.now(timezone.utc),
                    )
                    task.add_stage(stage)
                    self.queue.update(task)
                    ctx_file.append_stage(
                        stage_number=stage.stage_number,
                        agent_id=agent.id,
                        task_name="session",
                        status="completed",
                        input_text=current_input,
                        output_text=output,
                    )
                    if on_stage_complete:
                        on_stage_complete(task, stage)
                except Exception as e:
                    stage = StageRecord(
                        stage_number=task.next_stage_number,
                        agent_id=agent.id,
                        task_name="session",
                        input_text=current_input,
                        output_text=f"ERROR: {e}",
                        finished_at=datetime.now(timezone.utc),
                    )
                    task.add_stage(stage)
                    task.status = TaskStatus.failed
                    self.queue.update(task)
                    logger.error(
                        f"[Pipeline] {task.id} session '{agent.id}' failed: {e}"
                    )
                    return task

                # Check if session was cancelled
                if is_cancelled(task.id):
                    with _cancel_lock:
                        _cancelled_tasks.discard(task.id)
                    task.status = TaskStatus.cancelled
                    task.metadata["cancel_reason"] = "Cancelled during session"
                    self._release_chunks_on_finish(task)
                    self.queue.update(task)
                    return task

                # Session nodes skip gate evaluation; proceed to handoffs
                gate_result: Optional[GateResult] = None
                # Jump to handoff resolution below
                # (output variable is set from session result)
                handoff_targets = self._resolve_handoffs(
                    agent, gate_result, output, current_input
                )

                if not handoff_targets:
                    task.status = TaskStatus.completed
                    self._release_chunks_on_finish(task)
                    self.queue.update(task)
                    ctx_file.cleanup_snapshots()
                    logger.info(f"[Pipeline] {task.id} completed")
                    return task

                if len(handoff_targets) > 1:
                    for extra_agent_id, extra_payload in handoff_targets[1:]:
                        child = Task(
                            description=f"[fan-out from {task.id}] {task.description}",
                            metadata={
                                "parent_task_id": task.id,
                                "pipeline": task.metadata.get("pipeline"),
                                "_pipeline_params": task.metadata.get("_pipeline_params", {}),
                            },
                        )
                        self.queue.push(child, extra_agent_id)
                        self.run_task(
                            child, extra_agent_id,
                            input_text=extra_payload,
                            on_stage_complete=on_stage_complete,
                            on_stage_start=on_stage_start,
                            on_output=on_output,
                            on_thinking=on_thinking,
                            on_tool=on_tool,
                        )

                next_agent_id, next_payload = handoff_targets[0]
                ctx_file.save_payload(next_payload)
                current_input = next_payload
                current_agent_id = next_agent_id
                continue

            # ── Regular agent execution ──
            ctx_file.snapshot_before_stage(task.next_stage_number)
            logger.info(
                f"[Pipeline] {task.id} -> agent '{agent.id}' "
                f"(stage {task.next_stage_number})"
            )

            # ── Human input: "before" mode ──
            hi_cfg = agent.human_input
            if hi_cfg and hi_cfg.enabled and hi_cfg.mode in ("before", "both"):
                # Check if we already have a human response for this stage
                hi_key = f"_human_input_{agent.id}_{task.next_stage_number}"
                if hi_key not in task.metadata:
                    question = hi_cfg.prompt or (
                        f"Agent '{agent.name or agent.id}' needs your input before proceeding.\n\n"
                        f"Current task: {current_input[:500]}"
                    )
                    task.status = TaskStatus.awaiting_human_input
                    task.metadata["_human_input_pending"] = {
                        "agent_id": agent.id,
                        "stage_number": task.next_stage_number,
                        "questions": [question],
                        "mode": "before",
                    }
                    self.queue.update(task)

                    if on_human_input_request:
                        on_human_input_request(task, agent.id, [question])

                    logger.info(
                        f"[Pipeline] {task.id} awaiting human input "
                        f"(agent={agent.id}, mode=before)"
                    )
                    return task
                else:
                    # Human already responded — inject into input
                    human_response = task.metadata.pop(hi_key, "")
                    if human_response:
                        current_input = (
                            f"{current_input}\n\n"
                            f"--- User Input ---\n{human_response}"
                        )

            # Build prompt (respect agent's context_strategy + context_window)
            agent_chunk_settings = self._chunk_settings(agent)
            stage_has_chunks = bool(has_chunks and agent.type == "agent")
            if stage_has_chunks:
                self._chunk_claim(
                    agent, chunk_mgr, agent_chunk_settings, on_chunk_event
                )
            chunk_owner = (
                agent.id if agent_chunk_settings.get("owner_filter", True) else None
            )
            chunk_text = (
                chunk_mgr.summary(owner=chunk_owner) if stage_has_chunks else ""
            )
            context_text = ctx_file.read_for_strategy(
                agent.id, agent.context_strategy, agent.context_window,
            )
            prompt = build_prompt(
                system_prompt_template=agent.system_prompt,
                input_text=current_input,
                context=context_text,
                chunks=chunk_text,
            )
            if stage_has_chunks:
                prompt += self._chunk_prompt_suffix(
                    agent, chunk_mgr, agent_chunk_settings,
                    include_summary="{{ chunks" not in agent.system_prompt,
                    task=task,
                )
            prompt = self._apply_directives(prompt, task)

            # Run agent
            stage = StageRecord(
                stage_number=task.next_stage_number,
                agent_id=agent.id,
                task_name=(
                    agent.handoffs[0].task if agent.handoffs else "execute"
                ),
                input_text=current_input,
            )

            if on_stage_start:
                on_stage_start(task, agent.id, task.next_stage_number)

            # --- Retry-aware execution block ---
            from aqm.runtime.base import RuntimeExecutionError

            retry_cfg = agent.retry
            max_attempts = 1 + (retry_cfg.max_retries if retry_cfg else 0)
            last_error: Exception | None = None
            interjection_rounds = 0
            max_interjection_rounds = 8
            chunk_retries = 0

            while True:
                interrupted = False
                for attempt in range(1, max_attempts + 1):
                    try:
                        # On retry with fallback context strategy, rebuild prompt
                        if attempt > 1 and retry_cfg and retry_cfg.fallback_context_strategy:
                            context_text = ctx_file.read_for_strategy(
                                agent.id,
                                retry_cfg.fallback_context_strategy,
                                agent.context_window,
                            )
                            prompt = build_prompt(
                                system_prompt_template=agent.system_prompt,
                                input_text=current_input,
                                context=context_text,
                            )
                            prompt = self._apply_directives(prompt, task)
                            logger.info(
                                "[Pipeline] Retrying agent %s (attempt %d/%d, "
                                "fallback context: %s)",
                                agent.id, attempt, max_attempts,
                                retry_cfg.fallback_context_strategy,
                            )
                        elif attempt > 1:
                            logger.info(
                                "[Pipeline] Retrying agent %s (attempt %d/%d)",
                                agent.id, attempt, max_attempts,
                            )

                        output = self._execute_agent(
                            prompt,
                            agent,
                            task,
                            on_output=on_output,
                            on_thinking=on_thinking,
                            on_tool=on_tool,
                            interruptible=interjection_rounds < max_interjection_rounds,
                        )
                        stage.output_text = output
                        stage.finished_at = datetime.now(timezone.utc)
                        if attempt > 1:
                            stage.retry_count = attempt - 1
                            stage.retry_reason = str(last_error) if last_error else None
                        last_error = None
                        break  # success
                    except AgentInterrupted:
                        # The user typed while this member was answering.
                        # Restart the same turn with the note appended.
                        if is_cancelled(task.id):
                            with _cancel_lock:
                                _cancelled_tasks.discard(task.id)
                            task.status = TaskStatus.cancelled
                            task.metadata["cancel_reason"] = "Cancelled by user"
                            self._release_chunks_on_finish(task)
                            self.queue.update(task)
                            logger.info(f"[Pipeline] {task.id} cancelled by user")
                            return task

                        notes = self._take_pending_notes(task)
                        interjection_rounds += 1
                        if not notes:
                            # Signal without a note (race with another
                            # drain): rerun the same prompt, bounded.
                            if interjection_rounds > max_interjection_rounds:
                                raise RuntimeError(
                                    "Agent was interrupted repeatedly without "
                                    "a human note being available"
                                )
                            interrupted = True
                            break

                        self._apply_human_notes(
                            task,
                            notes,
                            agent.id,
                            stage.stage_number,
                            on_interjection=on_interjection,
                        )
                        if on_stage_interrupted:
                            on_stage_interrupted(
                                task, agent.id, stage.stage_number, notes
                            )
                        try:
                            self._restart_resident_session(task, agent.id)
                        except Exception:
                            logger.exception(
                                "[Pipeline] %s could not restart interrupted "
                                "session for agent '%s'",
                                task.id, agent.id,
                            )

                        logger.info(
                            "[Pipeline] %s agent '%s' interrupted by human note; "
                            "answering again (round %d)",
                            task.id, agent.id, interjection_rounds,
                        )
                        # Rebuild with the durable directive block and an
                        # explicit "answer again" instruction.
                        context_text = ctx_file.read_for_strategy(
                            agent.id, agent.context_strategy, agent.context_window,
                        )
                        prompt = build_prompt(
                            system_prompt_template=agent.system_prompt,
                            input_text=current_input,
                            context=context_text,
                            chunks=chunk_text,
                        )
                        if stage_has_chunks:
                            prompt += self._chunk_prompt_suffix(
                                agent, chunk_mgr, agent_chunk_settings,
                                include_summary="{{ chunks" not in agent.system_prompt,
                                task=task,
                            )
                        prompt = self._apply_directives(prompt, task)
                        prompt += self._interjection_instruction(notes)
                        interrupted = True
                        break
                    except Exception as e:
                        last_error = e
                        if attempt < max_attempts:
                            # Log and backoff before next attempt
                            error_cat = ""
                            if isinstance(e, RuntimeExecutionError):
                                error_cat = f" [{e.error_category}]"
                            logger.warning(
                                "[Pipeline] %s agent '%s' attempt %d/%d failed%s: %s",
                                task.id, agent.id, attempt, max_attempts,
                                error_cat, e,
                            )
                            if retry_cfg and retry_cfg.backoff > 0:
                                _time.sleep(retry_cfg.backoff)
                            continue

                        # All attempts exhausted — record failure
                        if isinstance(e, RuntimeExecutionError):
                            error_cat = e.error_category
                            error_detail = str(e) or (
                                f"{agent.runtime or 'runtime'} execution failed "
                                f"(agent={agent.id}, category={error_cat})"
                            )
                            if e.partial_output:
                                stage.output_text = (
                                    f"PARTIAL OUTPUT:\n{e.partial_output}\n\n"
                                    f"ERROR: {error_detail} [category={error_cat}]"
                                )
                                task.metadata["_partial_output"] = e.partial_output
                            else:
                                stage.output_text = (
                                    f"ERROR: {error_detail} [category={error_cat}]"
                                )
                        else:
                            error_detail = str(e) or repr(e)
                            stage.output_text = f"ERROR: {error_detail}"

                        stage.retry_count = attempt - 1
                        stage.retry_reason = str(e) if attempt > 1 else None
                        stage.finished_at = datetime.now(timezone.utc)
                        task.add_stage(stage)
                        task.status = TaskStatus.failed
                        self._release_chunks_on_finish(task)
                        # Save checkpoint for restart
                        task.metadata["_checkpoint_stage"] = stage.stage_number
                        task.metadata["_checkpoint_agent_id"] = agent.id
                        task.metadata["_checkpoint_input"] = current_input
                        self.queue.update(task)

                        ctx_file.append_stage(
                            stage_number=stage.stage_number,
                            agent_id=agent.id,
                            task_name=stage.task_name,
                            status="failed",
                            input_text=current_input,
                            output_text=stage.output_text,
                        )
                        logger.error(
                            "[Pipeline] %s agent '%s' execution failed "
                            "(after %d attempt(s)): %s",
                            task.id, agent.id, attempt, e,
                        )
                        return task

                if not interrupted:
                    if stage_has_chunks:
                        chunk_mgr.log_directives(agent.id, stage.stage_number, output)
                        actions = parse_chunk_directives(output, chunk_mgr, agent.id)
                        for action in actions:
                            self._chunk_emit(
                                on_chunk_event, "chunk_update", action
                            )
                        output = sanitize_chunk_directives(output)
                        stage.output_text = output
                        self._update_chunk_metadata(task, chunk_mgr)

                        # Drop the "优先处理" hint once that block is done (or
                        # removed), so later stages do not keep prioritising it.
                        focus = task.metadata.get("_chunk_focus")
                        if isinstance(focus, dict):
                            focused = chunk_mgr.get(str(focus.get("chunk_id") or ""))
                            if focused is None or focused.status == ChunkStatus.done:
                                task.metadata.pop("_chunk_focus", None)

                        pending = self._chunk_pending_for(
                            agent, chunk_mgr, agent_chunk_settings
                        )
                        mode = agent_chunk_settings.get("enforce", "retry")
                        limit = int(agent_chunk_settings.get("retry_limit", 2))
                        if pending and mode == "fail":
                            stage.finished_at = datetime.now(timezone.utc)
                            task.add_stage(stage)
                            task.status = TaskStatus.failed
                            self._release_chunks_on_finish(task)
                            task.metadata["error"] = (
                                f"Agent '{agent.id}' left {len(pending)} work "
                                "block(s) unfinished: "
                                + ", ".join(c.id for c in pending)
                            )
                            self.queue.update(task)
                            ctx_file.append_stage(
                                stage_number=stage.stage_number,
                                agent_id=agent.id,
                                task_name=stage.task_name,
                                status="failed",
                                input_text=current_input,
                                output_text=stage.output_text,
                            )
                            logger.error(
                                "[Pipeline] %s agent '%s' failed chunk "
                                "enforcement: %d unfinished",
                                task.id, agent.id, len(pending),
                            )
                            return task
                        if pending and mode == "retry" and chunk_retries < limit:
                            chunk_retries += 1
                            self._chunk_emit(on_chunk_event, "chunk_warning", {
                                "agent_id": agent.id,
                                "stage_number": stage.stage_number,
                                "remaining": [c.id for c in pending],
                                "retry": chunk_retries,
                                "limit": limit,
                            })
                            context_text = ctx_file.read_for_strategy(
                                agent.id, agent.context_strategy,
                                agent.context_window,
                            )
                            prompt = build_prompt(
                                system_prompt_template=agent.system_prompt,
                                input_text=current_input,
                                context=context_text,
                                chunks=chunk_text,
                            )
                            if stage_has_chunks:
                                prompt += self._chunk_prompt_suffix(
                                    agent, chunk_mgr, agent_chunk_settings,
                                    include_summary="{{ chunks" not in agent.system_prompt,
                                    task=task,
                                )
                            prompt = self._apply_directives(prompt, task)
                            prompt += remaining_instruction(pending)
                            logger.info(
                                "[Pipeline] %s agent '%s' retrying for %d "
                                "unfinished chunk(s) (%d/%d)",
                                task.id, agent.id, len(pending),
                                chunk_retries, limit,
                            )
                            continue
                        if pending and mode in ("warn", "retry"):
                            self._chunk_emit(on_chunk_event, "chunk_warning", {
                                "agent_id": agent.id,
                                "stage_number": stage.stage_number,
                                "remaining": [c.id for c in pending],
                                "retry": chunk_retries,
                                "limit": limit,
                                "final": True,
                            })
                    break

            # If we broke out of the loop with last_error still set, shouldn't
            # happen, but guard defensively.
            if last_error is not None:
                raise last_error  # pragma: no cover

            # ── Human input: "on_demand" mode ──
            if hi_cfg and hi_cfg.enabled and hi_cfg.mode in ("on_demand", "both"):
                hi_questions = self._parse_human_input_requests(output)
                if hi_questions:
                    hi_key = f"_human_input_od_{agent.id}_{stage.stage_number}"
                    if hi_key not in task.metadata:
                        # Pause pipeline — record stage first
                        task.add_stage(stage)
                        self.queue.update(task)

                        task.status = TaskStatus.awaiting_human_input
                        task.metadata["_human_input_pending"] = {
                            "agent_id": agent.id,
                            "stage_number": stage.stage_number,
                            "questions": hi_questions,
                            "mode": "on_demand",
                        }
                        self.queue.update(task)

                        ctx_file.append_stage(
                            stage_number=stage.stage_number,
                            agent_id=agent.id,
                            task_name=stage.task_name,
                            status="awaiting_human_input",
                            input_text=current_input,
                            output_text=output,
                        )

                        if on_human_input_request:
                            on_human_input_request(task, agent.id, hi_questions)

                        logger.info(
                            f"[Pipeline] {task.id} awaiting human input "
                            f"(agent={agent.id}, mode=on_demand, "
                            f"questions={len(hi_questions)})"
                        )
                        return task

            # Gate evaluation
            gate_result: Optional[GateResult] = None
            gate = self._get_gate(agent)

            if gate is not None:
                gate_result = gate.evaluate(task, output)

                if gate_result is None:
                    # HumanGate — pause the pipeline
                    stage.gate_result = None
                    task.add_stage(stage)
                    task.status = TaskStatus.awaiting_gate
                    self.queue.update(task)

                    ctx_file.append_stage(
                        stage_number=stage.stage_number,
                        agent_id=agent.id,
                        task_name=stage.task_name,
                        status="awaiting_gate",
                        input_text=current_input,
                        output_text=output,
                    )

                    logger.info(
                        f"[Pipeline] {task.id} waiting for human gate "
                        f"(agent={agent.id})"
                    )
                    return task

                stage.gate_result = gate_result.decision  # type: ignore
                stage.reject_reason = gate_result.reason

                # Track reject counts per agent to prevent infinite loops
                if gate_result.decision == "rejected":
                    reject_counts[agent.id] = reject_counts.get(agent.id, 0) + 1
                    max_retries = agent.gate.max_retries if agent.gate else 3
                    if reject_counts[agent.id] > max_retries:
                        stage.output_text = output
                        stage.finished_at = datetime.now(timezone.utc)
                        task.add_stage(stage)
                        task.status = TaskStatus.failed
                        self._release_chunks_on_finish(task)
                        task.metadata["error"] = (
                            f"Agent '{agent.id}' exceeded max gate retries "
                            f"({max_retries}). Gate kept rejecting output."
                        )
                        self.queue.update(task)
                        logger.error(
                            f"[Pipeline] {task.id} agent '{agent.id}' exceeded "
                            f"max gate retries ({max_retries})"
                        )
                        return task

            # Record stage
            task.add_stage(stage)
            self.queue.update(task)

            ctx_file.append_stage(
                stage_number=stage.stage_number,
                agent_id=agent.id,
                task_name=stage.task_name,
                status=(
                    gate_result.decision if gate_result else "completed"
                ),
                input_text=current_input,
                output_text=output,
                reject_reason=(
                    gate_result.reason if gate_result else None
                ),
            )

            # Write to agent's private context file
            ctx_file.append_agent_context(
                agent_id=agent.id,
                stage_number=stage.stage_number,
                input_text=current_input,
                output_text=output,
            )

            if on_stage_complete:
                on_stage_complete(task, stage)

            # Handoff
            handoff_targets = self._resolve_handoffs(
                agent, gate_result, output, current_input
            )

            if not handoff_targets:
                task.status = TaskStatus.completed
                self._release_chunks_on_finish(task)
                self.queue.update(task)
                ctx_file.cleanup_snapshots()
                logger.info(f"[Pipeline] {task.id} completed")
                return task

            # Fan-out: first target continues in this task; additional
            # targets spawn independent child tasks.
            if len(handoff_targets) > 1:
                for extra_agent_id, extra_payload in handoff_targets[1:]:
                    child = Task(
                        description=f"[fan-out from {task.id}] {task.description}",
                        metadata={
                            "parent_task_id": task.id,
                            "pipeline": task.metadata.get("pipeline"),
                            "_pipeline_params": task.metadata.get("_pipeline_params", {}),
                        },
                    )
                    self.queue.push(child, extra_agent_id)
                    logger.info(
                        f"[Pipeline] {task.id} fan-out -> "
                        f"child {child.id} -> agent '{extra_agent_id}'"
                    )
                    # Run child task asynchronously (push to queue, run inline)
                    self.run_task(
                        child,
                        extra_agent_id,
                        input_text=extra_payload,
                        on_stage_complete=on_stage_complete,
                        on_stage_start=on_stage_start,
                        on_output=on_output,
                        on_thinking=on_thinking,
                        on_tool=on_tool,
                    )

            next_agent_id, next_payload = handoff_targets[0]

            ctx_file.save_payload(next_payload)

            current_input = next_payload
            current_agent_id = next_agent_id

        # Exceeded max_stages
        task.status = TaskStatus.failed
        self._release_chunks_on_finish(task)
        task.metadata["error"] = (
            f"Exceeded maximum number of stages ({max_stages})."
        )
        self.queue.update(task)
        logger.error(f"[Pipeline] {task.id} exceeded maximum stages")
        return task

    @staticmethod
    def _parse_human_input_requests(agent_output: str) -> list[str]:
        """Extract ``HUMAN_INPUT: <question>`` directives from agent output."""
        questions: list[str] = []
        for m in re.finditer(r"HUMAN_INPUT:[ \t]*([^\n]+)", agent_output, re.IGNORECASE):
            q = m.group(1).strip()
            if q:
                questions.append(q)
        return questions

    def resume_human_input(
        self,
        task_id: str,
        response: str,
        on_stage_complete=None,
        on_stage_start=None,
        on_output=None,
        on_thinking=None,
        on_human_input_request=None,
        on_tool=None,
        on_interjection=None,
        on_stage_interrupted=None,
        on_chunk_event=None,
    ) -> Task:
        """Resume the pipeline after a human input response.

        Records the response in context files and resumes the pipeline
        from the agent that requested input.
        """
        task = self.queue.get(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        if task.status != TaskStatus.awaiting_human_input:
            raise ValueError(
                f"Task '{task_id}' is not awaiting human input. "
                f"(current: {task.status.value})"
            )

        pending = task.metadata.pop("_human_input_pending", {})
        agent_id = pending.get("agent_id", task.current_agent_id)
        mode = pending.get("mode", "on_demand")
        questions = pending.get("questions", [])

        # Record in context files
        ctx_file = self._get_context_file(task)
        question_text = "\n".join(f"- {q}" for q in questions)
        ctx_file.append_human_input(
            agent_id=agent_id,
            question=question_text,
            response=response,
        )

        if mode == "before":
            # Store response and re-run the same agent
            stage_number = pending.get("stage_number", task.next_stage_number)
            hi_key = f"_human_input_{agent_id}_{stage_number}"
            task.metadata[hi_key] = response
            task.status = TaskStatus.in_progress
            self.queue.update(task)

            # Determine input — use the last payload or description
            payload_path = ctx_file.task_dir / "current_payload.md"
            if payload_path.exists():
                input_text = payload_path.read_text(encoding="utf-8")
            else:
                input_text = task.description

            return self.run_task(
                task,
                agent_id,
                input_text=input_text,
                on_stage_complete=on_stage_complete,
                on_stage_start=on_stage_start,
                on_output=on_output,
                on_thinking=on_thinking,
                on_human_input_request=on_human_input_request,
                on_tool=on_tool,
                on_interjection=on_interjection,
                on_stage_interrupted=on_stage_interrupted,
                on_chunk_event=on_chunk_event,
            )
        else:
            # on_demand: re-run the agent with human response appended
            hi_key = f"_human_input_od_{agent_id}_{pending.get('stage_number', 0)}"
            task.metadata[hi_key] = response
            task.status = TaskStatus.in_progress
            self.queue.update(task)

            # Build new input with human response
            latest = task.latest_stage
            agent_output = latest.output_text if latest else ""
            new_input = (
                f"{latest.input_text if latest else task.description}\n\n"
                f"--- Agent's Questions ---\n{question_text}\n\n"
                f"--- User Response ---\n{response}\n\n"
                f"--- Previous Agent Output ---\n{agent_output}\n\n"
                f"Continue based on the user's response."
            )

            # Find next agent in handoffs or re-run same agent
            agent = self.agents.get(agent_id)
            if agent and agent.handoffs:
                # Continue to handoff targets
                handoff_targets = self._resolve_handoffs(
                    agent, None, agent_output, latest.input_text if latest else "",
                )
                if handoff_targets:
                    next_agent_id, _ = handoff_targets[0]
                    ctx_file.save_payload(new_input)
                    return self.run_task(
                        task, next_agent_id,
                        input_text=new_input,
                        on_stage_complete=on_stage_complete,
                        on_stage_start=on_stage_start,
                        on_output=on_output,
                        on_thinking=on_thinking,
                        on_human_input_request=on_human_input_request,
                        on_tool=on_tool,
                        on_interjection=on_interjection,
                        on_stage_interrupted=on_stage_interrupted,
                        on_chunk_event=on_chunk_event,
                    )

            # No handoffs — re-run same agent
            return self.run_task(
                task, agent_id,
                input_text=new_input,
                on_stage_complete=on_stage_complete,
                on_stage_start=on_stage_start,
                on_output=on_output,
                on_thinking=on_thinking,
                on_human_input_request=on_human_input_request,
                on_tool=on_tool,
                on_interjection=on_interjection,
                on_stage_interrupted=on_stage_interrupted,
                on_chunk_event=on_chunk_event,
            )

    def resume_task(
        self,
        task_id: str,
        decision: str,
        reason: str = "",
        target_agent_id: str | None = None,
        on_stage_complete=None,
        on_stage_start=None,
        on_output=None,
        on_thinking=None,
        on_tool=None,
        on_interjection=None,
        on_stage_interrupted=None,
        on_chunk_event=None,
    ) -> Task:
        """Resume the pipeline after a human gate.

        Args:
            task_id: Task ID
            decision: "approved" or "rejected"
            reason: Reason for the decision
            target_agent_id: Optional explicit handoff target chosen by the
                human reviewer.  Takes precedence over the pipeline's
                ``on_reject`` handoff configuration.

        Returns:
            The resumed Task
        """
        task = self.queue.get(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")
        if task.status not in (TaskStatus.awaiting_gate, TaskStatus.approved, TaskStatus.rejected):
            raise ValueError(
                f"Task '{task_id}' is not in a resumable state. "
                f"(current: {task.status.value})"
            )

        latest = task.latest_stage
        if latest is None:
            raise ValueError("No stage records found.")

        # Record gate result
        latest.gate_result = decision  # type: ignore
        latest.reject_reason = reason
        latest.finished_at = datetime.now(timezone.utc)
        self.queue.update(task)

        # Update context file
        ctx_file = self._get_context_file(task)

        agent = self.agents[latest.agent_id]
        gate_result = GateResult(decision=decision, reason=reason)

        # Determine handoff
        handoff_targets = self._resolve_handoffs(
            agent,
            gate_result,
            latest.output_text,
            latest.input_text,
        )

        if target_agent_id and target_agent_id not in self.agents:
            raise ValueError(
                f"Agent '{target_agent_id}' is not defined in this pipeline."
            )
        if not target_agent_id and decision == "rejected":
            # A reviewer usually says "让工程师重做" in the reason.  Match
            # member names/ids so the rejection lands on the right desk even
            # when the pipeline has no on_reject rule.
            target_agent_id = self._infer_target_from_reason(reason)

        if target_agent_id:
            configured = next(
                (payload for aid, payload in handoff_targets
                 if aid == target_agent_id),
                None,
            )
            next_payload = configured or self._build_reject_payload(
                agent, latest, reason, target_agent_id
            )
            task.metadata["last_reject_target"] = target_agent_id
            self.queue.update(task)
            next_agent_id = target_agent_id
        elif handoff_targets:
            next_agent_id, next_payload = handoff_targets[0]
        elif decision == "rejected":
            # No on_reject handoff is configured: send the work back to the
            # same agent instead of silently completing the task.
            next_agent_id = latest.agent_id
            next_payload = self._build_reject_payload(
                agent, latest, reason, next_agent_id
            )
            task.metadata["last_reject_target"] = next_agent_id
            self.queue.update(task)
        else:
            task.status = TaskStatus.completed
            self._release_chunks_on_finish(task)
            self.queue.update(task)
            return task

        ctx_file.save_payload(next_payload)

        return self.run_task(
            task,
            next_agent_id,
            input_text=next_payload,
            on_stage_complete=on_stage_complete,
            on_stage_start=on_stage_start,
            on_output=on_output,
            on_thinking=on_thinking,
            on_tool=on_tool,
            on_interjection=on_interjection,
            on_stage_interrupted=on_stage_interrupted,
            on_chunk_event=on_chunk_event,
        )

    def _infer_target_from_reason(self, reason: str) -> str | None:
        """Guess the intended member from the rejection text, if unambiguous."""
        text = (reason or "").strip()
        if not text:
            return None
        matches: list[str] = []
        for agent_id, agent in self.agents.items():
            names = {agent_id}
            if agent.name:
                names.add(agent.name.strip())
            for name in names:
                if len(name) < 2:
                    continue
                if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    pattern = (
                        r"(?<![A-Za-z0-9_-])"
                        + re.escape(name)
                        + r"(?![A-Za-z0-9_-])"
                    )
                    hit = re.search(pattern, text, re.IGNORECASE)
                else:
                    hit = re.search(re.escape(name), text)
                if hit and agent_id not in matches:
                    matches.append(agent_id)
                    break
        if len(matches) == 1:
            return matches[0]
        return None

    def _build_reject_payload(
        self,
        gate_agent: AgentDefinition,
        latest: StageRecord,
        reason: str,
        target_agent_id: str,
    ) -> str:
        """Build the input handed to the member chosen by a human reject."""
        target = self.agents.get(target_agent_id)
        target_name = (
            (target.name or target.id) if target is not None else target_agent_id
        )
        gate_name = gate_agent.name or gate_agent.id
        parts = [
            f"人工审批未通过（审批节点：{gate_name}），现转交给「{target_name}」处理。",
            "",
            "拒绝原因（必须优先处理）：",
            (reason or "").strip() or "（未填写）",
            "",
            "请根据拒绝原因修订或继续推进；不要忽略其中点名的问题。",
        ]
        if latest.output_text:
            parts += [
                "",
                f"上一版输出（来自 {gate_name}）：",
                latest.output_text,
            ]
        if latest.input_text:
            previous_input = latest.input_text
            if len(previous_input) > 6000:
                previous_input = (
                    previous_input[:6000].rstrip()
                    + "\n…（已截断，完整内容见任务上下文文件）"
                )
            parts += ["", f"当时的输入（来自 {gate_name} 之前）：", previous_input]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Task restart (checkpoint recovery)
    # ------------------------------------------------------------------

    def restart_task(
        self,
        task_id: str,
        from_stage: int | None = None,
        keep_pause: bool = False,
        on_stage_complete=None,
        on_stage_start=None,
        on_output=None,
        on_thinking=None,
        on_human_input_request=None,
        on_tool=None,
        on_interjection=None,
        on_stage_interrupted=None,
        on_chunk_event=None,
    ) -> Task:
        """Restart a failed/completed/stalled/cancelled task from a specific stage.

        Args:
            task_id: Task ID to restart.
            from_stage: Stage number to restart from.  If *None*, failed
                tasks restart from the failed stage; other statuses
                restart from the last stage.

        Returns:
            The restarted Task.
        """
        task = self.queue.get(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found.")

        restartable = {
            TaskStatus.pending,
            TaskStatus.failed,
            TaskStatus.completed,
            TaskStatus.stalled,
            TaskStatus.cancelled,
        }
        if task.status not in restartable:
            raise ValueError(
                f"Task '{task_id}' cannot be restarted "
                f"(current status: {task.status.value}). "
                f"Cancel it first if it is still running."
            )

        # Determine from_stage
        num_stages = len(task.stages)
        if from_stage is None:
            if num_stages == 0:
                # Nothing was recorded yet (the previous run died before stage 1
                # was saved), so there is nothing to resume — start over.
                from_stage = 1
            elif task.status in (TaskStatus.failed, TaskStatus.stalled):
                checkpoint = task.metadata.get("_checkpoint_stage")
                from_stage = num_stages
                if (
                    isinstance(checkpoint, int)
                    and 1 <= checkpoint <= num_stages + 1
                ):
                    from_stage = checkpoint
            else:
                from_stage = num_stages  # re-run last stage
        if from_stage < 1 or from_stage > num_stages + 1:
            raise ValueError(
                f"from_stage must be between 1 and {num_stages + 1}, "
                f"got {from_stage}."
            )

        # Restore context snapshot
        ctx_file = self._get_context_file(task)
        restored = ctx_file.restore_snapshot(from_stage)
        if not restored:
            logger.warning(
                "[Pipeline] No snapshot found for stage %d of %s — "
                "context files may be stale",
                from_stage,
                task_id,
            )

        # Truncate stages
        removed = task.truncate_stages(from_stage)
        logger.info(
            "[Pipeline] %s restarting from stage %d (removed %d stage(s))",
            task_id,
            from_stage,
            len(removed),
        )

        # Determine which agent/input to use at the restart point
        if not task.stages:
            # Restarting from stage 1 — use first agent
            first_agent_id = next(iter(self.agents))
            restart_agent_id = first_agent_id
            restart_input = task.description
        else:
            # Re-resolve handoff from the last remaining stage
            last_stage = task.stages[-1]
            last_agent = self.agents.get(last_stage.agent_id)
            if last_agent is None:
                raise ValueError(
                    f"Agent '{last_stage.agent_id}' not found in pipeline."
                )
            gate_result = (
                GateResult(
                    decision=last_stage.gate_result,
                    reason=last_stage.reject_reason or "",
                )
                if last_stage.gate_result
                else None
            )
            handoff_targets = self._resolve_handoffs(
                last_agent,
                gate_result,
                last_stage.output_text,
                last_stage.input_text,
            )
            if handoff_targets:
                restart_agent_id, restart_input = handoff_targets[0]
            else:
                # No handoff — re-run same agent with same input
                restart_agent_id = last_stage.agent_id
                restart_input = last_stage.input_text

        # Clean up checkpoint metadata
        for key in (
            "_checkpoint_stage",
            "_checkpoint_agent_id",
            "_checkpoint_input",
            "_partial_output",
            "error",
        ):
            task.metadata.pop(key, None)
        # A full restart (no surviving stages) starts a fresh artifact window;
        # partial restarts keep the original window because earlier stages'
        # deliverables still belong to this task.
        if from_stage <= 1:
            task.metadata.pop("run_started_at", None)

        # A restart is normally an explicit "run now" action: drop any
        # lingering pause (e.g. the task was paused, then failed, and the user
        # clicked 重启).  Startup auto-resume keeps the pause instead.
        if not keep_pause:
            clear_pause(task_id)
            task.metadata.pop("paused", None)
        task.status = TaskStatus.in_progress
        self.queue.update(task)

        return self.run_task(
            task,
            restart_agent_id,
            input_text=restart_input,
            on_stage_complete=on_stage_complete,
            on_stage_start=on_stage_start,
            on_output=on_output,
            on_thinking=on_thinking,
            on_human_input_request=on_human_input_request,
            on_tool=on_tool,
            on_interjection=on_interjection,
            on_stage_interrupted=on_stage_interrupted,
            on_chunk_event=on_chunk_event,
        )

    # ------------------------------------------------------------------
    # Conversational session execution
    # ------------------------------------------------------------------

    def _run_session(
        self,
        session: AgentDefinition,
        task: Task,
        input_text: str,
        ctx_file: ContextFile,
        on_turn_start=None,
        on_turn_complete=None,
        on_output=None,
        on_thinking=None,
        on_tool=None,
        on_chunk_event=None,
    ) -> str:
        """Run a conversational session among participant agents.

        Agents take turns in rounds.  Each agent sees the growing
        transcript via ``{{ transcript }}`` in its system prompt.
        The loop ends when consensus is detected or ``max_rounds``
        is reached.

        Returns the final session output (summary or last transcript).
        """
        from aqm.core.agent import ConsensusConfig
        from aqm.core.chunks import ChunkManager, parse_chunk_directives

        participants = [self.agents[pid] for pid in session.participants]
        consensus_cfg = session.consensus or ConsensusConfig()
        keyword = consensus_cfg.keyword.upper()
        require = consensus_cfg.require  # "all" | "majority"
        max_rounds = session.max_rounds

        # Track which agents have agreed
        agreements: dict[str, bool] = {a.id: False for a in participants}

        # Initialise transcript
        ctx_file.init_transcript(
            topic=input_text,
            participants=[a.id for a in participants],
        )

        # Initialise chunks (if configured)
        chunk_mgr = ChunkManager(ctx_file.task_dir)
        has_chunks = session.chunks is not None or bool(chunk_mgr.load().chunks)
        if has_chunks and session.chunks and session.chunks.initial:
            chunk_mgr.init_from_config(session.chunks.initial)

        logger.info(
            "[Pipeline] Session '%s' started (%d participants, max %d rounds)",
            session.id,
            len(participants),
            max_rounds,
        )

        final_output = ""

        for round_num in range(1, max_rounds + 1):
            if is_cancelled(task.id):
                break

            # Determine turn order for this round
            if session.turn_order == "moderator" and session.summary_agent:
                # Moderator goes first, then the rest
                order = []
                for a in participants:
                    if a.id == session.summary_agent:
                        order.insert(0, a)
                    else:
                        order.append(a)
            else:
                order = list(participants)

            for turn_idx, agent in enumerate(order):
                if is_cancelled(task.id):
                    break
                if not wait_while_paused(task.id):
                    break

                transcript = ctx_file.read_transcript()

                prompt = build_prompt(
                    system_prompt_template=agent.system_prompt,
                    input_text=input_text,
                    context=ctx_file.read_for_strategy(
                        agent.id, agent.context_strategy, agent.context_window,
                    ),
                    transcript=transcript,
                    chunks=chunk_mgr.summary() if has_chunks else "",
                )
                prompt = self._apply_directives(prompt, task)

                stage_num = task.next_stage_number

                if on_turn_start:
                    on_turn_start(task, agent.id, stage_num)

                try:
                    message = self._execute_agent(
                        prompt,
                        agent,
                        task,
                        on_output=on_output,
                        on_thinking=on_thinking,
                        interruptible=False,
                    )
                except Exception as turn_err:
                    # Record failed turn but continue session
                    message = f"[ERROR: agent '{agent.id}' failed: {turn_err}]"
                    logger.error(
                        "[Pipeline] Session '%s' agent '%s' failed in round %d: %s",
                        session.id, agent.id, round_num, turn_err,
                    )

                # Parse chunk directives first so they can be removed from the
                # user-visible transcript while still being broadcast live.
                if has_chunks:
                    chunk_mgr.log_directives(agent.id, stage_num, message)
                    for action in parse_chunk_directives(
                        message, chunk_mgr, agent.id
                    ):
                        self._chunk_emit(on_chunk_event, "chunk_update", action)
                    message = sanitize_chunk_directives(message)
                    self._update_chunk_metadata(task, chunk_mgr)

                # Record as a stage
                stage = StageRecord(
                    stage_number=stage_num,
                    agent_id=agent.id,
                    task_name=f"session:{session.id}:r{round_num}",
                    input_text=f"[round {round_num}]",
                    output_text=message,
                    finished_at=datetime.now(timezone.utc),
                )
                task.add_stage(stage)
                self.queue.update(task)

                # Write to agent's private context file
                ctx_file.append_agent_context(
                    agent_id=agent.id,
                    stage_number=stage_num,
                    input_text=f"[round {round_num}]",
                    output_text=message,
                )

                # Append to transcript
                ctx_file.append_turn(
                    round_number=round_num,
                    agent_id=agent.id,
                    message=message,
                    is_round_start=(turn_idx == 0),
                )

                # Parse chunk directives from agent output
                if has_chunks:
                    self._update_chunk_metadata(task, chunk_mgr)

                if on_turn_complete:
                    on_turn_complete(task, stage)

                final_output = message

                # Check consensus
                if consensus_cfg.method == "vote":
                    if keyword in message.upper():
                        agreements[agent.id] = True
                elif consensus_cfg.method == "moderator_decides":
                    if (
                        agent.id == session.summary_agent
                        and keyword in message.upper()
                    ):
                        # Moderator calls consensus for everyone
                        for k in agreements:
                            agreements[k] = True

            # End of round — check if consensus reached
            agreed_count = sum(1 for v in agreements.values() if v)
            total = len(agreements)

            consensus_reached = False
            if require == "all":
                consensus_reached = agreed_count == total
            elif require == "majority":
                consensus_reached = agreed_count > total / 2

            logger.info(
                "[Pipeline] Session '%s' round %d: %d/%d agreed",
                session.id,
                round_num,
                agreed_count,
                total,
            )

            # Gate consensus on chunk completion
            if consensus_reached and has_chunks and consensus_cfg.require_chunks_done:
                if not chunk_mgr.all_done():
                    consensus_reached = False
                    logger.info(
                        "[Pipeline] Session '%s' consensus votes met but "
                        "chunks not all done — continuing",
                        session.id,
                    )

            if consensus_reached:
                logger.info(
                    "[Pipeline] Session '%s' consensus reached at round %d",
                    session.id,
                    round_num,
                )

                # Run summary agent if specified
                if session.summary_agent:
                    summary_agent = self.agents[session.summary_agent]
                    transcript = ctx_file.read_transcript()
                    summary_prompt = build_prompt(
                        system_prompt_template=summary_agent.system_prompt,
                        input_text=input_text,
                        context=ctx_file.read_for_strategy(
                            summary_agent.id, summary_agent.context_strategy,
                            summary_agent.context_window,
                        ),
                        transcript=transcript,
                        chunks=chunk_mgr.summary() if has_chunks else "",
                    )
                    summary_prompt = self._apply_directives(summary_prompt, task)
                    runtime = self._get_runtime(summary_agent)
                    final_output = runtime.run(
                        summary_prompt, summary_agent, task,
                        on_output=on_output, on_thinking=on_thinking,
                        on_tool=on_tool,
                    )

                agreed_by = [k for k, v in agreements.items() if v]
                ctx_file.append_consensus(
                    round_number=round_num,
                    agreed_by=agreed_by,
                    summary=final_output,
                )

                task.metadata["session_consensus"] = True
                task.metadata["session_rounds"] = round_num
                if has_chunks:
                    total, done, _ = chunk_mgr.counts()
                    task.metadata["chunks_total"] = total
                    task.metadata["chunks_done"] = done
                return final_output

        # Max rounds exhausted
        logger.warning(
            "[Pipeline] Session '%s' max rounds (%d) reached without consensus",
            session.id,
            max_rounds,
        )
        task.metadata["session_consensus"] = False
        task.metadata["session_rounds"] = max_rounds
        if has_chunks:
            total, done, _ = chunk_mgr.counts()
            task.metadata["chunks_total"] = total
            task.metadata["chunks_done"] = done
        return final_output
