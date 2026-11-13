"""Long-lived CLI sessions used by collaborative meetings."""

from __future__ import annotations

import codecs
import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any, Callable

from aqm.core.interrupt import AgentInterrupted
from aqm.runtime.claude_config import (
    claude_thinking_args,
    should_pass_claude_model,
    trust_claude_project,
)
from aqm.runtime.cline_config import resolve_cline_provider_id
from aqm.runtime.stream_guard import ThinkingGuard, collapse_repeats

logger = logging.getLogger(__name__)

_ACTION_STYLES = {
    "read": ("📖", "读取"),
    "edit": ("✏️", "编辑"),
    "delete": ("🗑️", "删除"),
    "move": ("📦", "移动"),
    "search": ("🔍", "搜索"),
    "execute": ("▶️", "运行"),
    "fetch": ("🌐", "访问"),
    "think": ("💭", "思考"),
    "other": ("🔧", "调用"),
}

# Preferred rawInput keys, in order, for naming the object of a tool call.
_ACTION_TARGET_KEYS = (
    "filePath",
    "path",
    "file",
    "command",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
)

_CLAUDE_TOOL_STYLES = {
    "Read": ("📖", "读取"),
    "Write": ("✏️", "写入"),
    "Edit": ("✏️", "编辑"),
    "MultiEdit": ("✏️", "编辑"),
    "NotebookEdit": ("✏️", "编辑"),
    "Bash": ("▶️", "运行"),
    "BashOutput": ("▶️", "查看输出"),
    "Glob": ("🔍", "搜索"),
    "Grep": ("🔍", "搜索"),
    "WebFetch": ("🌐", "访问"),
    "WebSearch": ("🔍", "搜索"),
    "Task": ("🤖", "子任务"),
    "TodoWrite": ("📝", "待办"),
}


def _shorten_action(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else "..." + text[-limit:]


def _format_acp_tool_action(update: dict[str, Any]) -> str:
    """Turn an ACP ``tool_call`` / ``tool_call_update`` into one readable line."""
    kind = str(update.get("kind") or "other").lower()
    icon, label = _ACTION_STYLES.get(kind, _ACTION_STYLES["other"])
    title = str(update.get("title") or "").strip()

    target = ""
    locations = update.get("locations") or []
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict) and location.get("path"):
                target = str(location["path"])
                break
    raw = update.get("rawInput") or {}
    if not isinstance(raw, dict):
        raw = {}
    if kind == "execute":
        command = raw.get("command")
        if isinstance(command, str) and command.strip():
            target = command
    if not target:
        for key in _ACTION_TARGET_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                target = value
                break
            if isinstance(value, (list, tuple)) and value:
                target = " ".join(str(item) for item in value[:6])
                break
    target = _shorten_action(target)
    if target:
        return f"{icon} {label} {target}"
    if title and title.lower() != kind:
        return f"{icon} {label} {_shorten_action(title)}"
    return f"{icon} {label}"


def _format_claude_tool_action(name: str, tool_input: Any) -> str:
    icon, label = _CLAUDE_TOOL_STYLES.get(name, ("🔧", f"调用 {name or '工具'}"))
    target = ""
    if isinstance(tool_input, dict):
        for key in _ACTION_TARGET_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                target = value
                break
    target = _shorten_action(target)
    return f"{icon} {label} {target}".rstrip()



# cline ships a coding-agent system prompt that makes it inspect the repository
# with tools (read/run/edit) before answering.  In a meeting that turns a single
# turn into a multi-minute agentic run and can even modify the user's project,
# while the UI only shows "thinking" until the process exits.  Meetings replace
# the system prompt so each turn is a direct speech.
_CLINE_MEETING_SYSTEM_PROMPT = (
    "你是一个多智能体会议中的参会成员，不是编码代理。"
    "你没有文件系统、终端、代码编辑或其他任何工具，也绝对不要尝试调用任何工具。"
    "不要读取、创建或修改任何文件，不要运行任何命令。"
    "只根据用户消息中已经给出的信息，直接输出你的发言正文。"
    "发言完成后，调用 submit_and_exit 结束本次运行，"
    "并把你的完整发言正文原样放入 summary 字段。"
)


class MeetingAgentSession(ABC):
    @property
    @abstractmethod
    def is_alive(self) -> bool:
        ...

    def exit_detail(self) -> str:
        """Return a short reason why the session is no longer alive."""
        return ""

    @abstractmethod
    def run(
        self,
        prompt: str,
        timeout: int,
        stop_event: threading.Event,
        on_output: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_process: Callable[[str], None] | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> str:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class _JsonRpcProcess:
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._on_message = on_message
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._closed = threading.Event()
        self._stderr: deque[str] = deque(maxlen=80)
        # Any line from the CLI counts as activity: the JSON-RPC request
        # timeout is an *idle* timeout, so a long-running turn that keeps
        # streaming updates is never killed mid-answer.
        self.last_message_at = time.monotonic()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: int,
        stop_event: threading.Event | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            result: dict[str, Any] = {}
            self._pending[request_id] = (event, result)

        try:
            self._write({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            })
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        self.last_message_at = time.monotonic()
        while not event.wait(0.1):
            if stop_event and stop_event.is_set():
                with self._lock:
                    self._pending.pop(request_id, None)
                raise InterruptedError("stopped by user")
            if interrupt_event and interrupt_event.is_set():
                with self._lock:
                    self._pending.pop(request_id, None)
                raise AgentInterrupted("human interjection")
            if self._closed.is_set():
                raise RuntimeError(self._process_error())
            # Reset on every message from the CLI so only a genuinely silent
            # runtime triggers the timeout.
            if time.monotonic() - self.last_message_at >= timeout:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise TimeoutError(
                    f"{method} timed out after {timeout}s without any output"
                )

        if "error" in result:
            error = result["error"]
            message = f"{method} failed: {error.get('message', error)}"
            stderr_tail = " | ".join(list(self._stderr)[-3:]).strip()
            if stderr_tail:
                message = f"{message} (stderr: {stderr_tail[:500]})"
            raise RuntimeError(message)
        return result.get("result") or {}

    def _write(self, message: dict[str, Any]) -> None:
        if self.process.poll() is not None or not self.process.stdin:
            raise RuntimeError(self._process_error())
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def _read_stdout(self) -> None:
        try:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.last_message_at = time.monotonic()
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON CLI output: %s", line.rstrip())
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    with self._lock:
                        pending = self._pending.pop(message["id"], None)
                    if pending:
                        event, result = pending
                        result.update(message)
                        event.set()
                elif self._on_message:
                    try:
                        self._on_message(message)
                    except Exception:
                        logger.exception("JSON-RPC message handler failed")
        except (OSError, ValueError):
            logger.debug("JSON-RPC reader stopped", exc_info=True)
        finally:
            self._closed.set()
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for event, result in pending:
                result["error"] = {"message": self._process_error()}
                event.set()

    def _read_stderr(self) -> None:
        if not self.process.stderr:
            return
        for line in self.process.stderr:
            self._stderr.append(line.rstrip())

    def _process_error(self) -> str:
        detail = "\n".join(self._stderr).strip()
        return detail or f"CLI process exited with code {self.process.poll()}"

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self._closed.set()


class _ACPMeetingSession(MeetingAgentSession):
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        system_prompt: str,
        stop_event: threading.Event | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        model: str = "",
    ) -> None:
        self.cwd = cwd.resolve()
        self.system_prompt = system_prompt.strip()
        self.model = (model or "").strip()
        self.session_id = ""
        self._turn_lock = threading.Lock()
        self._output_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._thinking_guard = ThinkingGuard()
        self.last_thinking = ""
        self._action_lines: list[str] = []
        self._narration_parts: list[str] = []
        self._segment_parts: list[str] = []
        self.last_actions = ""
        self._seen_tools: set[str] = set()
        self._failed_tools: set[str] = set()
        self._output_callback: Callable[[str], None] | None = None
        self._thinking_callback: Callable[[str], None] | None = None
        self._action_callback: Callable[[str], None] | None = None
        self._process_callback: Callable[[str], None] | None = None
        self._terminals: dict[str, _Terminal] = {}
        self._rpc = _JsonRpcProcess(command, cwd, self._on_message)
        try:
            self._rpc.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "aqm", "title": "AQM", "version": "1"},
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": True,
                    },
                },
                timeout=30,
                stop_event=stop_event,
            )
            session = self._rpc.request(
                "session/new",
                {
                    "cwd": str(self.cwd),
                    "mcpServers": mcp_servers or [],
                },
                timeout=60,
                stop_event=stop_event,
            )
            self.session_id = session["sessionId"]
            self._apply_model(session, stop_event)
        except Exception:
            self._rpc.close()
            raise

    def _apply_model(
        self,
        session: dict[str, Any],
        stop_event: threading.Event | None,
    ) -> None:
        if not self.model:
            return
        model_config = next(
            (
                option
                for option in session.get("configOptions") or []
                if option.get("id") == "model"
            ),
            None,
        )
        if model_config is None:
            logger.warning(
                "Runtime does not expose a model config option; "
                "requested model %s cannot be applied",
                self.model,
            )
            return
        try:
            updated = self._rpc.request(
                "session/set_config_option",
                {
                    "sessionId": self.session_id,
                    "configId": "model",
                    "value": self.model,
                },
                timeout=30,
                stop_event=stop_event,
            )
        except RuntimeError as exc:
            message = str(exc)
            if "-32601" in message or "Method not found" in message:
                logger.warning(
                    "Runtime does not support session/set_config_option "
                    "(requested model %s not applied)",
                    self.model,
                )
                return
            raise RuntimeError(
                f"Could not select model '{self.model}': {exc}"
            ) from exc
        current = next(
            (
                option.get("currentValue")
                for option in updated.get("configOptions") or []
                if option.get("id") == "model"
            ),
            None,
        )
        if current is not None and current != self.model:
            raise RuntimeError(
                f"Model '{self.model}' was rejected by the runtime "
                f"(active model is '{current}')"
            )

    @property
    def is_alive(self) -> bool:
        return self._rpc.process.poll() is None

    def exit_detail(self) -> str:
        if self.is_alive:
            return ""
        try:
            return self._rpc._process_error().strip()[:500]
        except Exception:  # pragma: no cover - defensive
            return ""

    def _on_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "session/update":
            update = params.get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    text = content.get("text", "")
                    with self._turn_lock:
                        self._output_parts.append(text)
                        self._segment_parts.append(text)
                        callback = self._output_callback
                    if callback and text:
                        callback(text)
            elif update.get("sessionUpdate") == "agent_thought_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    text = content.get("text", "")
                    if text:
                        forwarded = self._thinking_guard.feed(text)
                        if forwarded:
                            with self._turn_lock:
                                self._thinking_parts.append(forwarded)
                                callback = self._thinking_callback
                            if callback:
                                callback(forwarded)
            elif update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
                self._emit_tool_action(update)
        elif method:
            request_id = message.get("id")
            if request_id is not None:
                threading.Thread(
                    target=self._handle_agent_request,
                    args=(request_id, method, params),
                    daemon=True,
                ).start()

    def _handle_agent_request(
        self,
        request_id: Any,
        method: str,
        params: dict[str, Any],
    ) -> None:
        try:
            result = self._handle_client_method(method, params)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        try:
            self._rpc._write(response)
        except (OSError, RuntimeError):
            logger.debug("Could not respond to ACP request %s", method, exc_info=True)

    def _handle_client_method(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if method == "session/request_permission":
            options = params.get("options") or []
            allowed = next(
                (
                    option
                    for option in options
                    if option.get("kind") == "allow_once"
                ),
                None,
            )
            if allowed is None:
                allowed = next(
                    (
                        option
                        for option in options
                        if option.get("kind") == "allow_always"
                    ),
                    None,
                )
            # ACP nests the decision: {"outcome": {"outcome": "selected", ...}}.
            # Returning the inner object makes the agent treat the decision as
            # an unknown outcome, which it reports as "user rejected
            # permission" and then aborts the whole turn.
            if allowed:
                return {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": allowed["optionId"],
                    }
                }
            return {"outcome": {"outcome": "cancelled"}}
        if method == "fs/read_text_file":
            path = self._project_path(params["path"])
            content = path.read_text(encoding="utf-8")
            line = int(params.get("line", 1))
            limit = params.get("limit")
            if line > 1 or limit is not None:
                lines = content.splitlines(keepends=True)
                content = "".join(lines[max(0, line - 1):])
                if limit is not None:
                    content = "".join(content.splitlines(keepends=True)[:int(limit)])
            return {"content": content}
        if method == "fs/write_text_file":
            path = self._project_path(params["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params["content"], encoding="utf-8")
            return {}
        if method == "terminal/create":
            terminal = _Terminal(self.cwd, params)
            self._terminals[terminal.id] = terminal
            return {"terminalId": terminal.id}
        if method == "terminal/output":
            terminal = self._get_terminal(params["terminalId"])
            return terminal.read_output()
        if method == "terminal/wait_for_exit":
            terminal = self._get_terminal(params["terminalId"])
            terminal.finished.wait()
            return terminal.exit_status()
        if method == "terminal/kill":
            self._get_terminal(params["terminalId"]).kill()
            return {}
        if method == "terminal/release":
            terminal = self._terminals.pop(params["terminalId"], None)
            if terminal:
                terminal.close()
            return {}
        raise ValueError(f"Unsupported ACP client method: {method}")

    def _project_path(self, value: str) -> Path:
        path = Path(value).resolve()
        try:
            path.relative_to(self.cwd)
        except ValueError as exc:
            raise ValueError(f"Agent path is outside the project: {path}") from exc
        return path

    def _get_terminal(self, terminal_id: str) -> _Terminal:
        try:
            return self._terminals[terminal_id]
        except KeyError as exc:
            raise ValueError(f"Unknown terminal: {terminal_id}") from exc

    def _emit_tool_action(self, update: dict[str, Any]) -> None:
        """Report file reads/edits/commands as a one-line action log entry."""
        call_id = str(update.get("toolCallId") or "")
        status = str(update.get("status") or "").lower()
        if status == "failed":
            if call_id in self._failed_tools:
                return
            self._failed_tools.add(call_id)
            line = "✗ 失败: " + _format_acp_tool_action(update).lstrip("🔧📖✏️▶️🔍🌐💭🗑️📦 ")
        else:
            if call_id in self._seen_tools:
                return
            has_details = bool(update.get("rawInput")) or bool(update.get("locations"))
            # A pending tool_call usually has no arguments yet; wait for the
            # follow-up update so the line names the actual file/command.
            if status in ("", "pending") and not has_details:
                return
            self._seen_tools.add(call_id)
            line = _format_acp_tool_action(update)
        # Text emitted before a tool call is the agent narrating its plan
        # ("let me look at ..."), not part of the meeting speech. Move it to
        # the thinking/process channel so the final answer stays clean.
        with self._turn_lock:
            narration = "".join(self._segment_parts).strip()
            self._segment_parts.clear()
            if narration:
                self._narration_parts.append(narration)
            process_callback = self._process_callback
        if narration and process_callback:
            process_callback(narration)
        with self._turn_lock:
            self._action_lines.append(line)
            callback = self._action_callback
        if callback:
            callback(line + "\n")

    def run(
        self,
        prompt: str,
        timeout: int,
        stop_event: threading.Event,
        on_output: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_process: Callable[[str], None] | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> str:
        with self._turn_lock:
            self._output_parts.clear()
            self._thinking_parts.clear()
            self._thinking_guard = ThinkingGuard()
            self._action_lines.clear()
            self._narration_parts.clear()
            self._segment_parts.clear()
            self._seen_tools.clear()
            self._failed_tools.clear()
            self.last_thinking = ""
            self.last_actions = ""
            self._output_callback = on_output
            self._process_callback = on_process
            self._thinking_callback = on_thinking
            self._action_callback = on_action
        if self.system_prompt:
            prompt = f"【你的角色与要求】\n{self.system_prompt}\n\n{prompt}"
        response: dict[str, Any] = {}
        try:
            response = self._rpc.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
                timeout=timeout,
                stop_event=stop_event,
                interrupt_event=interrupt_event,
            )
        except AgentInterrupted:
            try:
                self._rpc.notify("session/cancel", {"sessionId": self.session_id})
            except RuntimeError:
                pass
            raise
        except InterruptedError:
            try:
                self._rpc.notify("session/cancel", {"sessionId": self.session_id})
            except RuntimeError:
                pass
            raise
        except TimeoutError:
            self.close()
            raise
        finally:
            with self._turn_lock:
                self._output_callback = None
                self._thinking_callback = None
                self._action_callback = None
                self._process_callback = None
                thinking = "".join(self._thinking_parts).strip()
                narration = "\n\n".join(self._narration_parts).strip()
                self.last_thinking = "\n\n".join(
                    part for part in (collapse_repeats(thinking), narration) if part
                )
                self.last_actions = "\n".join(self._action_lines).strip()
                final_segment = "".join(self._segment_parts).strip()
                full_output = "".join(self._output_parts).strip()
        stop_reason = str((response or {}).get("stopReason") or "")
        if stop_reason:
            logger.debug(
                "ACP turn finished for agent session %s (stopReason=%s)",
                self.session_id, stop_reason,
            )
        # "cancelled"/"refusal" means the agent gave up (for example after a
        # rejected permission).  Those turns must not be mistaken for a real
        # answer, otherwise the pipeline pauses with a half-finished stage.
        if stop_reason in ("cancelled", "refusal"):
            raise RuntimeError(
                f"ACP runtime ended the turn with stopReason={stop_reason}"
            )
        result = final_segment or full_output
        if not result:
            raise RuntimeError(
                "ACP runtime produced no final answer "
                f"(stopReason={stop_reason or 'unknown'})"
            )
        return result

    def close(self) -> None:
        for terminal in list(self._terminals.values()):
            terminal.close()
        self._terminals.clear()
        self._rpc.close()


class _ClineTurn:
    """Mutable state for a single cline CLI invocation."""

    def __init__(self, process: subprocess.Popen[str] | None) -> None:
        self.process = process
        self.lines: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.outputs: list[str] = []
        self.errors: list[str] = []
        self.thinking: list[str] = []
        self.reasoning_stream: list[str] = []
        self.thinking_guard = ThinkingGuard()
        self.on_output: Callable[[str], None] | None = None
        self.on_thinking: Callable[[str], None] | None = None
        self.stdout_closed = False
        self.last_activity = time.monotonic()
        self.lock = threading.Lock()


class _ClineCliMeetingSession(MeetingAgentSession):
    """Per-turn cline CLI session used by meetings.

    cline's ACP server only offers the Cline-hosted providers
    (``cline``/``cline-pass``/``openai-codex``) and accepts any model string
    without validating it, then ends the turn with no text when the model is
    not hosted there.  The one-shot CLI honours the local provider settings
    (``providers.json``) and its configured models, so meetings use it.
    """

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        system_prompt: str,
        model: str = "",
    ) -> None:
        self.command = list(command)
        self.cwd = cwd.resolve()
        self.system_prompt = (system_prompt or "").strip()
        self.model = (model or "").strip()
        self._closed = False
        self._turn: _ClineTurn | None = None
        self.last_thinking = ""
        self.last_actions = ""

    @property
    def is_alive(self) -> bool:
        return not self._closed

    @staticmethod
    def _collect_reasoning(turn: _ClineTurn, line: str) -> None:
        """Record cline's reasoning blocks from its ``--json`` event stream.

        Called while ``turn.lock`` is held.
        """
        text = line.strip()
        if not text.startswith("{"):
            return
        try:
            record = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(record, dict) or record.get("type") != "agent_event":
            return
        event = record.get("event")
        if not isinstance(event, dict):
            return
        if event.get("type") != "content_start":
            return
        kind = event.get("contentType")
        if kind == "reasoning":
            delta = event.get("reasoning")
            if isinstance(delta, str) and delta:
                forwarded = turn.thinking_guard.feed(delta)
                if forwarded:
                    turn.reasoning_stream.append(forwarded)
                    if turn.on_thinking:
                        turn.on_thinking(forwarded)
        elif kind == "text":
            # A new text block starts a new reasoning phase.
            if turn.reasoning_stream:
                joined = "".join(turn.reasoning_stream).strip()
                if joined:
                    turn.thinking.append(joined)
                turn.reasoning_stream = []
            delta = event.get("text")
            if isinstance(delta, str) and delta and turn.on_output:
                turn.on_output(delta)

    @staticmethod
    def _pump(turn: _ClineTurn) -> None:
        """Move queued lines into ``turn`` until stdout ends.

        cline may close stdout but keep the process alive, so ``process.poll``
        is also used as an end-of-stream hint.
        """
        lines = turn.lines
        process = turn.process
        while True:
            try:
                name, line = lines.get(timeout=0.2)
            except queue.Empty:
                if process is not None and process.poll() is not None:
                    with turn.lock:
                        turn.stdout_closed = True
                    return
                continue
            if line is None:
                if name == "stdout":
                    with turn.lock:
                        turn.stdout_closed = True
                continue
            with turn.lock:
                if name == "stdout":
                    turn.last_activity = time.monotonic()
                    turn.outputs.append(line)
                    _ClineCliMeetingSession._collect_reasoning(turn, line)
                else:
                    turn.errors.append(line)

    @staticmethod
    def _drain(turn: _ClineTurn) -> None:
        while True:
            try:
                name, line = turn.lines.get_nowait()
            except queue.Empty:
                return
            if line is None:
                continue
            with turn.lock:
                if name == "stdout":
                    turn.last_activity = time.monotonic()
                    turn.outputs.append(line)
                    _ClineCliMeetingSession._collect_reasoning(turn, line)
                else:
                    turn.errors.append(line)

    def _stop_process(self, process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def run(
        self,
        prompt: str,
        timeout: int,
        stop_event: threading.Event,
        on_output: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_process: Callable[[str], None] | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> str:
        # cline already separates reasoning from the final answer, so there is
        # no extra "process narration" channel to report here.
        del on_process
        if self._closed:
            raise RuntimeError("cline CLI session is closed")

        self.last_thinking = ""
        self.last_actions = ""
        previous = self._turn
        if previous is not None:
            # A timed-out or abandoned turn may leave a child behind; kill it
            # so its output cannot leak into this turn.
            self._stop_process(previous.process)

        if self.system_prompt:
            prompt = f"【你的角色与要求】\n{self.system_prompt}\n\n{prompt}"
        # `timeout` is enforced below as an *idle* timeout. cline's own `-t` is
        # a wall-clock budget, so give it plenty of headroom instead of killing
        # a long but active answer.
        native_timeout = max(1800, int(timeout) * 4)
        command = self.command + ["-t", str(native_timeout), prompt]

        turn = _ClineTurn(None)
        turn.on_output = on_output
        turn.on_thinking = on_thinking
        process = subprocess.Popen(
            command,
            cwd=str(self.cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        turn.process = process
        self._turn = turn

        def _read(stream: Any, name: str) -> None:
            try:
                for line in stream:
                    turn.lines.put((name, line))
            except Exception:  # pragma: no cover - reader best effort
                pass
            finally:
                turn.lines.put((name, None))

        threading.Thread(
            target=_read, args=(process.stdout, "stdout"), daemon=True
        ).start()
        threading.Thread(
            target=_read, args=(process.stderr, "stderr"), daemon=True
        ).start()
        threading.Thread(target=self._pump, args=(turn,), daemon=True).start()

        while not turn.stdout_closed:
            if stop_event and stop_event.is_set():
                self._stop_process(process)
                raise InterruptedError("stopped by user")
            if interrupt_event and interrupt_event.is_set():
                self._stop_process(process)
                raise AgentInterrupted("human interjection")
            if time.monotonic() - turn.last_activity >= timeout:
                self._stop_process(process)
                raise TimeoutError(
                    f"cline produced no output for {timeout}s"
                )
            time.sleep(0.1)

        # cline can keep the process alive after its JSON stream ends (it waits
        # for a signal that never comes in non-interactive mode), so exit is
        # only awaited briefly and the process is then reaped.
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._stop_process(process)
        self._drain(turn)

        with turn.lock:
            blocks = list(turn.thinking)
            pending = "".join(turn.reasoning_stream).strip()
        if pending:
            blocks.append(pending)
        self.last_thinking = "\n\n".join(
            collapse_repeats(block) for block in blocks if block
        )

        from aqm.meeting import _extract_cline_text

        text = _extract_cline_text("".join(turn.outputs))
        if not text:
            detail = "".join(turn.errors).strip() or (
                f"process exited with code {process.returncode}"
            )
            raise RuntimeError(f"cline CLI failed: {detail[:800]}")
        return text

    def close(self) -> None:
        self._closed = True
        turn = self._turn
        if turn is not None:
            self._stop_process(turn.process)

class _Terminal:
    _OUTPUT_LIMIT = 1024 * 1024

    def __init__(self, project_root: Path, params: dict[str, Any]) -> None:
        self.id = str(uuid.uuid4())
        command = params["command"]
        args = params.get("args") or []
        cwd = Path(params.get("cwd") or project_root).resolve()
        try:
            cwd.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(f"Terminal cwd is outside the project: {cwd}") from exc
        env = os.environ.copy()
        env.update({
            item["name"]: item["value"]
            for item in params.get("env", [])
            if "name" in item and "value" in item
        })
        self.process = subprocess.Popen(
            [command, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
        )
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._output = ""
        self._truncated = False
        self._output_limit = min(
            max(int(params.get("outputByteLimit", self._OUTPUT_LIMIT)), 1),
            self._OUTPUT_LIMIT,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        for data in iter(lambda: os.read(self.process.stdout.fileno(), 4096), b""):
            chunk = decoder.decode(data)
            with self._lock:
                output = self._output + chunk
                encoded = output.encode("utf-8")
                if len(encoded) > self._output_limit:
                    encoded = encoded[-self._output_limit:]
                    while encoded and encoded[0] & 0xC0 == 0x80:
                        encoded = encoded[1:]
                    output = encoded.decode("utf-8", errors="ignore")
                    self._truncated = True
                self._output = output
        remainder = decoder.decode(b"", final=True)
        if remainder:
            with self._lock:
                self._output += remainder
        self.process.wait()
        self.finished.set()

    def read_output(self) -> dict[str, Any]:
        with self._lock:
            result = {"output": self._output, "truncated": self._truncated}
        exit_status = self.exit_status()
        if exit_status is not None:
            result["exitStatus"] = exit_status
        return result

    def exit_status(self) -> dict[str, Any] | None:
        if not self.finished.is_set():
            return None
        return {"exitCode": self.process.returncode, "signal": None}

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()

    def close(self) -> None:
        self.kill()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class _ClaudeStreamSession(MeetingAgentSession):
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        stop_event: threading.Event | None = None,
        cleanup_paths: list[Path] | None = None,
    ) -> None:
        # Claude Code phones home for telemetry/update checks before answering;
        # on networks where api.anthropic.com is unreachable that stalls the
        # turn until the timeout, so meetings disable those requests.
        env = dict(os.environ)
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        self._cleanup_paths = cleanup_paths or []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        time.sleep(0.1)
        if (stop_event and stop_event.is_set()) or self.process.poll() is not None:
            error = self._process_error()
            self.close()
            if stop_event and stop_event.is_set():
                raise InterruptedError("stopped by user")
            raise RuntimeError(error)

    @property
    def is_alive(self) -> bool:
        return self.process.poll() is None

    def _read_stdout(self) -> None:
        try:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                try:
                    self._messages.put(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON Claude output: %s", line.rstrip())
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        if self.process.stderr:
            self._stderr.extend(line.rstrip() for line in self.process.stderr)

    def _process_error(self) -> str:
        detail = "\n".join(self._stderr).strip()
        return detail or f"Claude CLI exited with code {self.process.poll()}"

    def run(
        self,
        prompt: str,
        timeout: int,
        stop_event: threading.Event,
        on_output: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_process: Callable[[str], None] | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> str:
        if not self.process.stdin or self.process.poll() is not None:
            raise RuntimeError(self._process_error())
        message = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        action_lines: list[str] = []
        segment_parts: list[str] = []
        narration_parts: list[str] = []
        seen_tools: set[str] = set()
        thinking_guard = ThinkingGuard()
        self.last_thinking = ""
        self.last_actions = ""

        def flush_narration() -> None:
            narration = "".join(segment_parts).strip()
            segment_parts.clear()
            if not narration:
                return
            narration_parts.append(narration)
            if on_process:
                on_process(narration)

        def report_tool(name: str, tool_input: Any, tool_id: str = "") -> None:
            key = tool_id or f"{name}:{json.dumps(tool_input, sort_keys=True, default=str)}"
            if key in seen_tools:
                return
            seen_tools.add(key)
            # Text before a tool call is plan narration, not the speech.
            flush_narration()
            line = _format_claude_tool_action(name, tool_input)
            action_lines.append(line)
            if on_action:
                on_action(line + "\n")
        while True:
            if stop_event.is_set():
                self.close()
                raise InterruptedError("stopped by user")
            if interrupt_event and interrupt_event.is_set():
                # Keep the process alive: the pipeline restarts this member's
                # session and reruns the turn with the human note appended.
                raise AgentInterrupted("human interjection")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise subprocess.TimeoutExpired("claude", timeout)
            try:
                event = self._messages.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            # Any event is activity: the timeout only fires when the model
            # stops producing output entirely.
            deadline = time.monotonic() + timeout
            if event is None:
                raise RuntimeError(self._process_error())
            if event.get("type") == "result":
                self.last_thinking = "\n\n".join(
                    part
                    for part in (
                        collapse_repeats("".join(thinking_parts).strip()),
                        "\n\n".join(narration_parts).strip(),
                    )
                    if part
                )
                self.last_actions = "\n".join(action_lines).strip()
                result = event.get("result")
                return result.strip() if isinstance(result, str) else "".join(text_parts).strip()
            if event.get("type") == "stream_event":
                inner = event.get("event") or {}
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    text_parts.append(text)
                    if on_output and text:
                        on_output(text)
                elif delta.get("type") == "thinking_delta":
                    thought = delta.get("thinking") or delta.get("text") or ""
                    if thought:
                        forwarded = thinking_guard.feed(thought)
                        if forwarded:
                            thinking_parts.append(forwarded)
                            if on_thinking:
                                on_thinking(forwarded)
            elif event.get("type") == "assistant":
                content = (event.get("message") or {}).get("content") or []
                for item in content:
                    if item.get("type") == "tool_use" and item.get("name"):
                        report_tool(
                            item["name"],
                            item.get("input") or {},
                            item.get("id", ""),
                        )
                if not text_parts:
                    text_parts.extend(
                        item["text"]
                        for item in content
                        if item.get("type") == "text" and isinstance(item.get("text"), str)
                    )
                if not thinking_parts:
                    blocks = [
                        collapse_repeats(item["thinking"])
                        for item in content
                        if item.get("type") == "thinking"
                        and isinstance(item.get("thinking"), str)
                    ]
                    if blocks:
                        thinking_parts.extend(blocks)
                        if on_thinking:
                            for block in blocks:
                                on_thinking(block)

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        for path in self._cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary MCP config %s", path)
        self._cleanup_paths.clear()


class _CodexAppServerSession(MeetingAgentSession):
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        model: str,
        system_prompt: str,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._turn_lock = threading.Lock()
        self._turn_done = threading.Event()
        self._turn_output: list[str] = []
        self._output_callback: Callable[[str], None] | None = None
        self._thinking_callback: Callable[[str], None] | None = None
        self._turn_error = ""
        self._turn_id = ""
        self._rpc = _JsonRpcProcess(command, cwd, self._on_message)
        try:
            self._rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "aqm", "title": "AQM", "version": "1"},
                    "capabilities": {"experimentalApi": False},
                },
                timeout=30,
                stop_event=stop_event,
            )
            self._rpc.notify("initialized", {})
            params: dict[str, Any] = {"cwd": str(cwd.resolve())}
            if model:
                params["model"] = model
            params["approvalPolicy"] = "never"
            params["sandbox"] = "workspace-write"
            if system_prompt:
                params["baseInstructions"] = system_prompt
            thread = self._rpc.request(
                "thread/start",
                params,
                timeout=60,
                stop_event=stop_event,
            )
            self.thread_id = thread["thread"]["id"]
        except Exception:
            self._rpc.close()
            raise

    @property
    def is_alive(self) -> bool:
        return self._rpc.process.poll() is None

    def exit_detail(self) -> str:
        if self.is_alive:
            return ""
        try:
            return self._rpc._process_error().strip()[:500]
        except Exception:  # pragma: no cover - defensive
            return ""

    def _on_message(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        with self._turn_lock:
            if method.endswith("agentMessage/delta"):
                delta = params.get("delta", "")
                self._turn_output.append(delta)
                if self._output_callback and delta:
                    self._output_callback(delta)
            elif "reasoning" in method.lower() and method.lower().endswith("delta"):
                thought = params.get("delta") or params.get("text") or ""
                if self._thinking_callback and thought:
                    self._thinking_callback(thought)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") != "completed":
                    error = turn.get("error") or {}
                    self._turn_error = error.get("message") or turn.get("status", "turn failed")
                self._turn_done.set()

    def run(
        self,
        prompt: str,
        timeout: int,
        stop_event: threading.Event,
        on_output: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_process: Callable[[str], None] | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> str:
        del on_process
        with self._turn_lock:
            self._turn_output.clear()
            self._turn_error = ""
            self._turn_done.clear()
            self._output_callback = on_output
            self._thinking_callback = on_thinking
        response = self._rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=30,
        )
        turn = response.get("turn") or {}
        self._turn_id = turn.get("id", "")
        while not self._turn_done.wait(0.1):
            if stop_event.is_set():
                self._rpc.notify(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self._turn_id},
                )
                raise InterruptedError("stopped by user")
            if interrupt_event and interrupt_event.is_set():
                self._rpc.notify(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self._turn_id},
                )
                raise AgentInterrupted("human interjection")
            # Only a silent runtime is timed out; streamed progress resets it.
            if time.monotonic() - self._rpc.last_message_at >= timeout:
                self._rpc.notify(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self._turn_id},
                )
                self.close()
                raise subprocess.TimeoutExpired("codex app-server turn", timeout)
        with self._turn_lock:
            self._output_callback = None
            if stop_event.is_set():
                raise InterruptedError("stopped by user")
            if self._turn_error:
                raise RuntimeError(self._turn_error)
            return "".join(self._turn_output).strip()

    def close(self) -> None:
        self._rpc.close()


def _build_meeting_command(agent: Any, project_root: Path) -> list[str]:
    from aqm.meeting import _resolve_cmd

    runtime = agent.runtime
    if runtime == "claude":
        command = _resolve_cmd("claude") + [
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            # Without this Claude only emits whole assistant messages, which
            # makes the meeting UI look frozen until the turn finishes.
            "--include-partial-messages",
            "--permission-mode", "acceptEdits",
        ]
        if agent.system_prompt:
            # Keep Claude's default system prompt: replacing it made the agent
            # drift into other meeting roles.
            command.extend(["--append-system-prompt", agent.system_prompt])
        if should_pass_claude_model(agent.model):
            command.extend(["--model", agent.model])
        # A user-level settings.json may set CLAUDE_CODE_DISABLE_THINKING=1.
        # Override it per run so thinking_delta reaches the UI; set
        # AQM_CLAUDE_THINKING_TOKENS=0 to keep it off.
        command.extend(claude_thinking_args())
        command.extend(getattr(agent, "cli_flags", None) or [])
        return command
    if runtime == "codex":
        return _resolve_cmd("codex") + ["app-server", "--listen", "stdio://"]
    if runtime == "gemini":
        command = _resolve_cmd("gemini")
        if agent.model:
            command.extend(["--model", agent.model])
        command.extend(getattr(agent, "cli_flags", None) or [])
        return command + ["--acp"]
    if runtime == "opencode":
        # opencode's ACP server rejects `--model`; the model is applied after
        # `session/new` through `session/set_config_option` (see _ACPMeetingSession).
        command = _resolve_cmd("opencode")
        command.extend(getattr(agent, "cli_flags", None) or [])
        return command + ["acp"]
    if runtime == "kilo":
        # kilo, like opencode, has no `--model` flag for ACP startup.
        command = _resolve_cmd("kilo")
        command.extend(getattr(agent, "cli_flags", None) or [])
        return command + ["acp", "--cwd", str(project_root)]
    if runtime == "cline":
        # cline's ACP mode only exposes Cline-hosted providers and silently
        # ends a turn without text for any other model, so meetings keep using
        # the one-shot CLI, which honours the locally configured providers.
        command = _resolve_cmd("cline") + [
            "--json", "--auto-approve", "true", "-y", "-c", str(project_root),
            f"-s={_CLINE_MEETING_SYSTEM_PROMPT}",
        ]
        provider_id = resolve_cline_provider_id()
        if provider_id:
            command.extend(["--provider", provider_id])
        if agent.model:
            command.extend(["-m", agent.model])
        command.extend(getattr(agent, "cli_flags", None) or [])
        return command
    raise ValueError(f"Unsupported meeting runtime: {runtime}")


_LANGUAGE_SECTION = "\n\n【语言要求】"


def _apply_project_language(agent: Any, project_root: Path) -> Any:
    """Append the project's reply-language rule to a meeting member prompt."""
    try:
        from aqm.core.config import load_project_config

        language = (load_project_config(project_root).language or "").strip()
    except Exception:
        return agent
    if not language:
        return agent
    prompt = (getattr(agent, "system_prompt", "") or "").strip()
    if "【语言要求】" in prompt:
        return agent
    rule = (
        f"{_LANGUAGE_SECTION}"
        f"所有发言——包括思考过程、工具调用前后的说明、进度汇报与最终结论——"
        f"都必须使用{language}，不要中英文混杂（代码、命令、专有名词除外）。"
    )
    if hasattr(agent, "model_copy"):
        return agent.model_copy(update={"system_prompt": prompt + rule})
    try:
        agent.system_prompt = prompt + rule
    except Exception:
        pass
    return agent


def start_meeting_agent_session(
    agent: Any,
    project_root: Path,
    stop_event: threading.Event | None = None,
) -> MeetingAgentSession:
    if stop_event and stop_event.is_set():
        raise InterruptedError("stopped by user")
    agent = _apply_project_language(agent, project_root)
    if agent.runtime == "claude":
        trust_claude_project(project_root)
    command = _build_meeting_command(agent, project_root)
    if agent.runtime == "claude":
        cleanup_paths: list[Path] = []
        if getattr(agent, "mcp", None):
            from aqm.runtime.claude import _build_mcp_config, _write_temp_file

            config_path = _write_temp_file(
                json.dumps(_build_mcp_config(agent.mcp)),
                prefix="aqm_mcp_",
                suffix=".json",
            )
            cleanup_paths.append(config_path)
            command.extend(["--mcp-config", str(config_path)])
        try:
            return _ClaudeStreamSession(
                command,
                project_root,
                stop_event,
                cleanup_paths=cleanup_paths,
            )
        except Exception:
            for path in cleanup_paths:
                path.unlink(missing_ok=True)
            raise
    if agent.runtime == "codex":
        return _CodexAppServerSession(
            command,
            project_root,
            agent.model,
            agent.system_prompt,
            stop_event,
        )
    if agent.runtime == "cline":
        return _ClineCliMeetingSession(
            command,
            project_root,
            agent.system_prompt,
            agent.model,
        )
    mcp_servers = []
    for server in getattr(agent, "mcp", []):
        if server.command:
            command_name = server.command
            args = server.args
        else:
            command_name = "npx"
            args = ["-y", f"@modelcontextprotocol/server-{server.server}", *server.args]
        mcp_servers.append({
            "name": server.server,
            "command": command_name,
            "args": args,
            "env": server.env or {},
        })
    return _ACPMeetingSession(
        command,
        project_root,
        agent.system_prompt,
        stop_event,
        mcp_servers=mcp_servers,
        model=agent.model,
    )
