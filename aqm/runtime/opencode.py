"""OpencodeRuntime ¡ª opencode CLI integration."""



from __future__ import annotations



import logging

import shutil

import subprocess
import sys
import os

from pathlib import Path



from aqm.core.agent import AgentDefinition

from aqm.core.task import Task

from aqm.runtime.base import AbstractRuntime, OutputCallback, RuntimeExecutionError, ThinkingCallback, ToolCallback



logger = logging.getLogger(__name__)



_DEFAULT_OPENCODE_MODEL = None





def _classify_error(stderr: str, returncode: int) -> str:

    lower = stderr.lower() if stderr else ""

    if "context window" in lower or "token limit" in lower or "too long" in lower:

        return "context_overflow"

    if returncode == 127 or "not found" in lower or "no such file" in lower:

        return "cli_missing"

    if "timed out" in lower or "timeout" in lower:

        return "timeout"

    return "unknown"





def _check_opencode_cli_available() -> None:

    if shutil.which("opencode") is None:

        raise FileNotFoundError(

            "The 'opencode' CLI was not found on PATH."

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
        if os.path.exists(c):
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
        # 1) Real .exe on PATH
        r = shutil.which(exe + ".exe")
        if r:
            return [r]
        # 2) Real .exe under npm node_modules
        npm_root = _npm_root()
        if npm_root:
            for pkg in (exe, exe.replace("kilo", "@kilocode/cli"), exe.replace("opencode", "opencode-ai")):
                found = _find(npm_root, pkg)
                if found:
                    return [found]
        # 3) For JS-based CLIs, invoke node.exe directly (bypasses cmd.exe mangling)
        if npm_root:
            node_exe = shutil.which("node.exe") or shutil.which("node")
            if node_exe:
                for pkg in (exe, exe.replace("kilo", "@kilocode/cli"), exe.replace("opencode", "opencode-ai")):
                    found = _find_js(npm_root, pkg)
                    if found:
                        return [node_exe, found]
        # 4) Fallback: any .cmd/.bat (may mangle multi-line args but works for simple cases)
        for ext in (".cmd", ".bat"):
            r = shutil.which(exe + ext)
            if r:
                return [r]
        r = shutil.which(exe)
        if r:
            return [r]
    r = shutil.which(exe)
    return [r] if r else [exe]


class OpencodeRuntime(AbstractRuntime):

    def __init__(self, project_root=None, timeout: int = 600) -> None:

        self._project_root = project_root or Path.cwd()

        self._timeout = timeout



    @property

    def name(self) -> str:

        return "opencode"



    def run(

        self,

        prompt: str,

        agent: AgentDefinition,

        task: Task,

        on_output: OutputCallback = None,

        on_thinking: ThinkingCallback = None,

        on_tool: ToolCallback = None,

    ) -> str:

        _check_opencode_cli_available()



        full_prompt = prompt



        cmd: list[str] = _resolve_cmd("opencode") + ["run", full_prompt, "--auto"]




        if agent.model:

            cmd.extend(["--model", agent.model])



        if agent.cli_flags:

            cmd.extend(agent.cli_flags)



        cmd.extend(["--dir", str(self._project_root)])



        logger.info(

            "[OpencodeRuntime] Running agent '%s' (model=%s)",

            agent.id,

            agent.model or _DEFAULT_OPENCODE_MODEL,

        )



        if on_output:

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

                f"opencode CLI timed out after {self._timeout}s (agent={agent.id})",

                error_category="timeout",

            )



        if result.returncode != 0:

            stderr = result.stderr.strip()

            error_category = _classify_error(stderr, result.returncode)

            error_msg = stderr or f"process exited with code {result.returncode}"

            logger.error(

                "[OpencodeRuntime] Agent '%s' failed (%s): %s",

                agent.id,

                error_category,

                error_msg,

            )

            raise RuntimeExecutionError(

                f"opencode CLI execution failed (agent={agent.id}): {error_msg}",

                partial_output=result.stdout.strip(),

                error_category=error_category,

            )



        output = result.stdout.strip()

        logger.info(

            "[OpencodeRuntime] Agent '%s' completed (%d chars)",

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

        except subprocess.TimeoutExpired:

            raise RuntimeExecutionError(

                f"opencode CLI timed out after {self._timeout}s (agent={agent.id})",

                partial_output="".join(output_parts).strip(),

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

                "[OpencodeRuntime] Agent '%s' failed (%s): %s",

                agent.id,

                error_category,

                error_msg,

            )

            raise RuntimeExecutionError(

                f"opencode CLI execution failed (agent={agent.id}): {error_msg}",

                partial_output="".join(output_parts).strip(),

                error_category=error_category,

            )



        return "".join(output_parts).strip()

