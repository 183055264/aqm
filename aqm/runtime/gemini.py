"""GeminiRuntime — Google Gemini CLI integration."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from aqm.core.agent import AgentDefinition
from aqm.core.task import Task
from aqm.runtime.base import AbstractRuntime, OutputCallback, RuntimeExecutionError, ThinkingCallback, ToolCallback

logger = logging.getLogger(__name__)

_DEFAULT_GEMINI_MODEL = None


def _classify_error(stderr: str, returncode: int) -> str:
    lower = stderr.lower() if stderr else ""
    if "context window" in lower or "token limit" in lower or "too long" in lower:
        return "context_overflow"
    if returncode == 127 or "not found" in lower or "no such file" in lower:
        return "cli_missing"
    if "timed out" in lower or "timeout" in lower:
        return "timeout"
    return "unknown"


def _check_gemini_cli_available() -> None:
    if shutil.which("gemini") is None:
        raise FileNotFoundError(
            "The 'gemini' CLI was not found on PATH. "
            "Install it with: npm install -g @google/gemini-cli"
        )


def _resolve_cmd(exe: str) -> list[str]:
    """Resolve CLI to argv prefix."""
    if sys.platform == "win32":
        # 1) Real .exe on PATH
        r = shutil.which(exe + ".exe")
        if r:
            return [r]
        # 2) Fallback: any .cmd/.bat
        for ext in (".cmd", ".bat"):
            r = shutil.which(exe + ext)
            if r:
                return [r]
        r = shutil.which(exe)
        if r:
            return [r]
    r = shutil.which(exe)
    return [r] if r else [exe]




_TEMP_FILES_TO_CLEANUP: list[Path] = []


def _cleanup_temp_files() -> None:
    """Remove temp files registered for cleanup."""
    for path in _TEMP_FILES_TO_CLEANUP:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    _TEMP_FILES_TO_CLEANUP.clear()


def _build_mcp_config(mcp_servers: list[dict]) -> dict:
    """Build MCP configuration for Gemini CLI."""
    return {
        "mcpServers": {
            server["name"]: server for server in mcp_servers
        }
    }


def _write_temp_file(content: str, *, prefix: str = "aq_gemini_", suffix: str = ".json") -> Path:
    """Write content to a temp file and register for cleanup."""
    import tempfile
    import json

    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        prefix=prefix,
        suffix=suffix,
    ) as f:
        if isinstance(content, (dict, list)):
            json.dump(content, f, indent=2)
        else:
            f.write(content)
        path = Path(f.name)

    _TEMP_FILES_TO_CLEANUP.append(path)
    return path


def _redact_command(cmd: list[str]) -> list[str]:
    """Redact sensitive parts of command for logging."""
    return [str(c) for c in cmd]



class GeminiRuntime(AbstractRuntime):
    """Execute tasks using the Google Gemini CLI."""

    def __init__(self, project_root=None, timeout: int = 600) -> None:
        self._project_root = project_root or Path.cwd()
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "gemini"

    def run(
        self,
        prompt: str,
        agent: AgentDefinition,
        task: Task,
        on_output: OutputCallback = None,
        on_thinking: ThinkingCallback = None,
        on_tool: ToolCallback = None,
    ) -> str:
        _check_gemini_cli_available()

        full_prompt = prompt

        cmd: list[str] = _resolve_cmd("gemini") + [full_prompt, "--auto"]

        if agent.model:
            cmd.extend(["--model", agent.model])

        if agent.cli_flags:
            cmd.extend(agent.cli_flags)

        cmd.extend(["--dir", str(self._project_root)])

        logger.info(
            "[GeminiRuntime] Running agent '%s' (model=%s)",
            agent.id,
            agent.model or _DEFAULT_GEMINI_MODEL,
        )

        if on_output and sys.platform != "win32":
            return self._run_streaming(cmd, agent, on_output, on_tool)

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
                f"gemini CLI timed out after {self._timeout}s (agent={agent.id})",
                error_category="timeout",
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            error_category = _classify_error(stderr, result.returncode)
            error_msg = stderr or f"process exited with code {result.returncode}"
            logger.error(
                "[GeminiRuntime] Agent '%s' failed (%s): %s",
                agent.id,
                error_category,
                error_msg,
            )
            raise RuntimeExecutionError(
                f"gemini CLI execution failed (agent={agent.id}): {error_msg}",
                partial_output=result.stdout.strip(),
                error_category=error_category,
            )

        output = result.stdout.strip()
        logger.info(
            "[GeminiRuntime] Agent '%s' completed (%d chars)",
            agent.id,
            len(output),
        )
        return output

    def _run_streaming(
        self,
        cmd: list[str],
        agent: AgentDefinition,
        on_output: OutputCallback,
        on_tool: ToolCallback = None,
    ) -> str:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self._project_root),
        )

        output_parts: list[str] = []

        try:
            while True:
                line = proc.stdout.readline() if proc.stdout else None
                if not line:
                    break
                output_parts.append(line)
                try:
                    on_output(line.rstrip("\n"))
                except Exception:
                    pass

            proc.wait(timeout=10)

        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeExecutionError(
                f"gemini CLI timed out after {self._timeout}s (agent={agent.id})",
                error_category="timeout",
            )

        output = "".join(output_parts).strip()
        if proc.returncode != 0:
            stderr = proc.stderr.read() if proc.stderr else ""
            error_category = _classify_error(stderr, proc.returncode)
            raise RuntimeExecutionError(
                f"gemini CLI execution failed (agent={agent.id}): {stderr or 'unknown error'}",
                partial_output=output,
                error_category=error_category,
            )

        return output


# Backward-compatible alias
GeminiCLIRuntime = GeminiRuntime
