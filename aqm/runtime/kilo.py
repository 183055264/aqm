"""KiloRuntime — kilocode/kilo CLI integration."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import os
from pathlib import Path

from aqm.core.agent import AgentDefinition
from aqm.core.task import Task
from aqm.runtime.base import (
    AbstractRuntime,
    OutputCallback,
    RuntimeExecutionError,
    ThinkingCallback,
    ToolCallback,
)
from aqm.runtime.json_stream import JsonStreamParser

logger = logging.getLogger(__name__)

_DEFAULT_KILO_MODEL = None


def _classify_error(stderr: str, returncode: int) -> str:
    lower = stderr.lower() if stderr else ""
    if "context window" in lower or "token limit" in lower or "too long" in lower:
        return "context_overflow"
    if returncode == 127 or "not found" in lower or "no such file" in lower:
        return "cli_missing"
    if "timed out" in lower or "timeout" in lower:
        return "timeout"
    return "unknown"


def _check_kilo_cli_available() -> None:
    if shutil.which("kilocode") is None and shutil.which("kilo") is None:
        raise FileNotFoundError(
            "Neither 'kilocode' nor 'kilo' CLI was found on PATH."
        )


def _resolve_cmd(exe: str) -> list[str]:
    """Resolve CLI to argv prefix. Returns list of args to prepend.

    .cmd wrappers on Windows go through cmd.exe which mangles multi-line
    arguments (newlines get eaten). For npm-installed JS-based CLIs we
    fall back to invoking node.exe directly with the script path, bypassing
    cmd.exe entirely.
    """
    def _find(npm_root: str, pkg: str) -> str | None:
        for sub in ("bin", "bin/bin"):
            c = os.path.join(npm_root, "node_modules", pkg, sub, exe + ".exe")
            if os.path.exists(c):
                return c
        return None

    def _find_js(npm_root: str, pkg: str) -> str | None:
        c = os.path.join(npm_root, "node_modules", pkg, "bin", exe)
        # Must be a regular file; some packages ship a directory with the
        # same name (e.g. @kilocode/cli has bin/kilocode/ as a WASM fallback
        # folder, but the real entry point is bin/kilo).
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
        r = shutil.which(exe + ".exe")
        if r:
            return [r]
        npm_root = _npm_root()
        if npm_root:
            for pkg in (exe, "@kilocode/cli"):
                found = _find(npm_root, pkg)
                if found:
                    return [found]
            node_exe = shutil.which("node.exe") or shutil.which("node")
            if node_exe:
                for pkg in (exe, "@kilocode/cli"):
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
    r = shutil.which(exe)
    return [r] if r else [exe]


class KiloRuntime(AbstractRuntime):

    def __init__(self, project_root=None, timeout: int = 600) -> None:
        self._project_root = project_root or Path.cwd()
        self._timeout = timeout
        # The @kilocode/cli package exposes both `kilo` and `kilocode` npm shims.
        # The actual JS entry point in node_modules is `bin/kilo`; `bin/kilocode`
        # is a directory (contains a WASM fallback), so resolving the latter
        # through _resolve_cmd would fail. Always target `kilo`.
        self._binary = "kilo"

    @property
    def name(self) -> str:
        return "kilo"

    def run(
        self,
        prompt: str,
        agent: AgentDefinition,
        task: Task,
        on_output: OutputCallback = None,
        on_thinking: ThinkingCallback = None,
        on_tool: ToolCallback = None,
    ) -> str:
        _check_kilo_cli_available()

        # --format json emits structured events (text / tool_use / step_finish)
        # per step, so the Web UI's Live Output panel shows real-time progress
        # instead of going silent until the agent finishes. The parser also
        # reconstructs the assistant's final answer from text events.
        cmd: list[str] = _resolve_cmd(self._binary) + [
            "run", prompt, "--auto", "--format", "json",
        ]

        if agent.model:
            cmd.extend(["--model", agent.model])
        if agent.cli_flags:
            cmd.extend(agent.cli_flags)
        cmd.extend(["--dir", str(self._project_root)])

        logger.info(
            "[KiloRuntime] Running agent '%s' (model=%s) via %s",
            agent.id,
            agent.model or _DEFAULT_KILO_MODEL,
            self._binary,
        )

        if on_output or on_tool or on_thinking:
            return self._run_streaming(cmd, agent, on_output, on_thinking, on_tool)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeExecutionError(
                f"{self._binary} CLI timed out after {self._timeout}s (agent={agent.id})",
                error_category="timeout",
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            error_category = _classify_error(stderr, result.returncode)
            error_msg = stderr or f"process exited with code {result.returncode}"
            logger.error(
                "[KiloRuntime] Agent '%s' failed (%s): %s",
                agent.id,
                error_category,
                error_msg,
            )
            raise RuntimeExecutionError(
                f"{self._binary} CLI execution failed (agent={agent.id}): {error_msg}",
                partial_output=result.stdout.strip(),
                error_category=error_category,
            )

        parser = JsonStreamParser()
        for line in result.stdout.splitlines():
            parser.feed_line(line)
        output = parser.collected_text
        logger.info(
            "[KiloRuntime] Agent '%s' completed (%d chars, %d tools)",
            agent.id,
            len(output),
            parser.tool_count,
        )
        return output

    def _run_streaming(
        self,
        cmd: list[str],
        agent: AgentDefinition,
        on_output: OutputCallback | None,
        on_thinking: ThinkingCallback | None = None,
        on_tool: ToolCallback | None = None,
    ) -> str:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self._project_root),
        )
        parser = JsonStreamParser(
            on_output=on_output,
            on_thinking=on_thinking,
            on_tool=on_tool,
        )

        try:
            while True:
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    break
                parser.feed_line(line)
        except subprocess.TimeoutExpired:
            raise RuntimeExecutionError(
                f"{self._binary} CLI timed out after {self._timeout}s (agent={agent.id})",
                partial_output=parser.collected_text,
                error_category="timeout",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        if proc.returncode != 0:
            try:
                stderr = proc.stderr.read().strip() if proc.stderr else ""
            except Exception:
                stderr = ""
            error_category = _classify_error(stderr, proc.returncode)
            error_msg = stderr or f"process exited with code {proc.returncode}"
            logger.error(
                "[KiloRuntime] Agent '%s' failed (%s): %s",
                agent.id,
                error_category,
                error_msg,
            )
            raise RuntimeExecutionError(
                f"{self._binary} CLI execution failed (agent={agent.id}): {error_msg}",
                partial_output=parser.collected_text,
                error_category=error_category,
            )

        output = parser.collected_text
        logger.info(
            "[KiloRuntime] Agent '%s' completed (%d chars, %d steps, %d tools)",
            agent.id,
            len(output),
            parser.step_count,
            parser.tool_count,
        )
        return output
