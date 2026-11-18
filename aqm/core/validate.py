"""Resource availability and permission validation for agents.yaml.

Provides checks that go beyond JSON Schema validation:
- Runtime CLI availability (claude, gemini, codex)
- MCP server command availability
- Environment variable existence
- Filesystem path accessibility
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Warning data structure
# ---------------------------------------------------------------------------


class ValidationWarning(NamedTuple):
    """A non-fatal validation warning."""

    level: str  # "warning" or "error" (when --strict)
    path: str  # dot-path in the YAML structure
    message: str
    fix: str


# ---------------------------------------------------------------------------
# Environment variable reference pattern
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _extract_env_refs(value: str) -> list[str]:
    """Extract environment variable names referenced in *value*.

    Supports both ``$VAR`` and ``${VAR}`` syntax.  Returns an empty list
    if the value doesn't reference any environment variables.
    """
    refs: list[str] = []
    for m in _ENV_VAR_RE.finditer(value):
        refs.append(m.group(1) or m.group(2))
    return refs


# ---------------------------------------------------------------------------
# Resource availability checks
# ---------------------------------------------------------------------------


def check_resource_availability(data: dict[str, Any]) -> list[ValidationWarning]:
    """Check that runtime CLIs and MCP commands referenced in *data* exist.

    Parameters
    ----------
    data:
        Parsed agents.yaml content (a dict).

    Returns
    -------
    list[ValidationWarning]
        Warnings for missing resources.
    """
    warnings: list[ValidationWarning] = []
    agents = data.get("agents", [])

    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue

        agent_id = agent.get("id", f"#{i}")
        agent_type = agent.get("type", "agent")
        prefix = f"agents[{i}]({agent_id})"

        # --- Runtime CLI check ---
        runtime = agent.get("runtime")
        if runtime and agent_type != "session":
            if shutil.which(runtime) is None:
                warnings.append(
                    ValidationWarning(
                        level="warning",
                        path=f"{prefix}.runtime",
                        message=f"Runtime CLI '{runtime}' not found in PATH",
                        fix=f"Install the '{runtime}' CLI or ensure it is on your PATH.",
                    )
                )

        # --- MCP server command check ---
        mcp_list = agent.get("mcp", [])
        for j, mcp in enumerate(mcp_list if isinstance(mcp_list, list) else []):
            if not isinstance(mcp, dict):
                continue

            server_name = mcp.get("server", "")
            mcp_prefix = f"{prefix}.mcp[{j}]({server_name})"
            command = mcp.get("command")

            # If no command specified, default is "npx" for most MCP servers
            effective_command = command if command else "npx"

            if shutil.which(effective_command) is None:
                warnings.append(
                    ValidationWarning(
                        level="warning",
                        path=f"{mcp_prefix}.command",
                        message=f"MCP command '{effective_command}' not found in PATH",
                        fix=f"Install '{effective_command}' or set the correct 'command' field.",
                    )
                )

    return warnings


# ---------------------------------------------------------------------------
# Permission / environment checks
# ---------------------------------------------------------------------------


def check_permissions(data: dict[str, Any]) -> list[ValidationWarning]:
    """Check environment variables and filesystem paths referenced in *data*.

    Parameters
    ----------
    data:
        Parsed agents.yaml content (a dict).

    Returns
    -------
    list[ValidationWarning]
        Warnings for missing env vars or inaccessible paths.
    """
    warnings: list[ValidationWarning] = []
    agents = data.get("agents", [])

    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue

        agent_id = agent.get("id", f"#{i}")
        prefix = f"agents[{i}]({agent_id})"

        mcp_list = agent.get("mcp", [])
        for j, mcp in enumerate(mcp_list if isinstance(mcp_list, list) else []):
            if not isinstance(mcp, dict):
                continue

            server_name = mcp.get("server", "")
            mcp_prefix = f"{prefix}.mcp[{j}]({server_name})"

            # --- Environment variable check ---
            env = mcp.get("env")
            if isinstance(env, dict):
                for key, value in env.items():
                    if not isinstance(value, str):
                        continue
                    env_refs = _extract_env_refs(value)
                    for var_name in env_refs:
                        if var_name not in os.environ:
                            warnings.append(
                                ValidationWarning(
                                    level="warning",
                                    path=f"{mcp_prefix}.env.{key}",
                                    message=f"Environment variable '{var_name}' is not set",
                                    fix=f"Set the environment variable: export {var_name}=<value>",
                                )
                            )

            # --- Filesystem path check ---
            if server_name == "filesystem":
                args = mcp.get("args", [])
                if isinstance(args, list):
                    # filesystem server typically has paths as trailing args
                    # after the server package name, e.g.:
                    #   args: ["-y", "@modelcontextprotocol/server-filesystem", "/path1", "/path2"]
                    for k, arg in enumerate(args):
                        if not isinstance(arg, str):
                            continue
                        # Skip flags and package names
                        if arg.startswith("-") or arg.startswith("@"):
                            continue
                        # Treat remaining args as filesystem paths
                        if arg.startswith("/") or arg.startswith("~"):
                            expanded = os.path.expanduser(arg)
                            if not os.path.exists(expanded):
                                warnings.append(
                                    ValidationWarning(
                                        level="warning",
                                        path=f"{mcp_prefix}.args[{k}]",
                                        message=f"Path does not exist: {arg}",
                                        fix=f"Create the directory or update the path.",
                                    )
                                )
                            elif not os.access(expanded, os.R_OK):
                                warnings.append(
                                    ValidationWarning(
                                        level="warning",
                                        path=f"{mcp_prefix}.args[{k}]",
                                        message=f"Path is not readable: {arg}",
                                        fix=f"Check file permissions: chmod +r {arg}",
                                    )
                                )

    return warnings


# ---------------------------------------------------------------------------
# Work-block (chunks) checks
# ---------------------------------------------------------------------------


def check_chunks(data: dict[str, Any]) -> list[ValidationWarning]:
    """Validate ``chunks`` and session consensus settings."""
    warnings: list[ValidationWarning] = []
    agents = data.get("agents", [])

    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue
        agent_id = agent.get("id", f"#{i}")
        prefix = f"agents[{i}]({agent_id})"
        agent_type = agent.get("type", "agent")

        if agent_type not in ("agent", "session"):
            warnings.append(ValidationWarning(
                level="error",
                path=f"{prefix}.type",
                message=f"Unknown agent type '{agent_type}'",
                fix="Use 'agent' or 'session'.",
            ))

        chunks = agent.get("chunks")
        consensus = agent.get("consensus") or {}
        require_done = bool(
            isinstance(consensus, dict) and consensus.get("require_chunks_done")
        )
        if require_done and not chunks:
            warnings.append(ValidationWarning(
                level="error",
                path=f"{prefix}.consensus.require_chunks_done",
                message="require_chunks_done=true but no chunks config",
                fix="Add a 'chunks' block or disable require_chunks_done.",
            ))
        if chunks is None:
            continue
        if not isinstance(chunks, dict):
            warnings.append(ValidationWarning(
                level="error",
                path=f"{prefix}.chunks",
                message="chunks must be a mapping",
                fix="Use a mapping with enabled/initial/enforce fields.",
            ))
            continue

        enforce = chunks.get("enforce")
        if enforce not in (None, "off", "warn", "retry", "fail"):
            warnings.append(ValidationWarning(
                level="warning",
                path=f"{prefix}.chunks.enforce",
                message=f"Unknown chunks.enforce value '{enforce}'",
                fix="Use off / warn / retry / fail.",
            ))
        retry_limit = chunks.get("retry_limit")
        if retry_limit is not None and (
            not isinstance(retry_limit, int) or retry_limit < 0
        ):
            warnings.append(ValidationWarning(
                level="warning",
                path=f"{prefix}.chunks.retry_limit",
                message="chunks.retry_limit must be a non-negative integer",
                fix="Use 0 or a positive integer.",
            ))
        initial = chunks.get("initial")
        if initial is not None and not isinstance(initial, list):
            warnings.append(ValidationWarning(
                level="warning",
                path=f"{prefix}.chunks.initial",
                message="chunks.initial must be a list",
                fix="Use a YAML list of descriptions.",
            ))
        elif isinstance(initial, list):
            for j, item in enumerate(initial):
                if isinstance(item, str) and item.strip():
                    continue
                if isinstance(item, dict) and (
                    item.get("description") or item.get("title")
                ):
                    continue
                warnings.append(ValidationWarning(
                    level="warning",
                    path=f"{prefix}.chunks.initial[{j}]",
                    message="Empty or invalid work-block entry",
                    fix="Use a non-empty description string.",
                ))
        forward = chunks.get("forward_on_done")
        review_owner = chunks.get("review_owner")
        known_ids = {
            a.get("id") for a in agents if isinstance(a, dict) and a.get("id")
        }
        if forward and not review_owner:
            warnings.append(ValidationWarning(
                level="warning",
                path=f"{prefix}.chunks.forward_on_done",
                message="forward_on_done=true but no review_owner",
                fix="Set chunks.review_owner to the member that reviews each block.",
            ))
        if review_owner and known_ids and review_owner not in known_ids:
            warnings.append(ValidationWarning(
                level="warning",
                path=f"{prefix}.chunks.review_owner",
                message=f"review_owner '{review_owner}' is not a known agent",
                fix="Use an agent id declared in this pipeline.",
            ))
        scope = agent.get("write_scope")
        if scope is not None and (
            not isinstance(scope, list)
            or any(not isinstance(item, str) or not item.strip() for item in scope)
        ):
            warnings.append(ValidationWarning(
                level="warning",
                path=f"{prefix}.write_scope",
                message="write_scope must be a list of non-empty glob paths",
                fix='Use e.g. write_scope: ["src/ui/**"].',
            ))
    return warnings


def check_parallel(data: dict[str, Any]) -> list[ValidationWarning]:
    """Validate the optional top-level ``parallel`` scheduling block."""
    warnings: list[ValidationWarning] = []
    parallel = data.get("parallel")
    if parallel is None:
        return warnings
    if not isinstance(parallel, dict):
        return [ValidationWarning(
            level="error",
            path="parallel",
            message="parallel must be a mapping",
            fix="Use parallel: {enabled: true, max_workers: 2}.",
        )]
    if not parallel.get("enabled"):
        return warnings

    workers = parallel.get("max_workers", 2)
    if not isinstance(workers, int) or workers < 1 or workers > 8:
        warnings.append(ValidationWarning(
            level="warning",
            path="parallel.max_workers",
            message="max_workers should be between 1 and 8",
            fix="Start with 2 and raise it only after the scopes are verified.",
        ))
    mode = parallel.get("on_conflict")
    if mode not in (None, "block", "warn", "serialize"):
        warnings.append(ValidationWarning(
            level="warning",
            path="parallel.on_conflict",
            message=f"Unknown on_conflict value '{mode}'",
            fix="Use block / warn / serialize.",
        ))
    hot = parallel.get("hot_files")
    if hot is not None and not isinstance(hot, list):
        warnings.append(ValidationWarning(
            level="warning",
            path="parallel.hot_files",
            message="hot_files must be a list of glob patterns",
            fix='Use e.g. hot_files: ["package.json", "*.lock"].',
        ))

    agents = [a for a in data.get("agents", []) if isinstance(a, dict)]
    scoped = [a for a in agents if a.get("write_scope")]
    if len(scoped) < 2:
        warnings.append(ValidationWarning(
            level="warning",
            path="parallel.enabled",
            message=(
                "parallel is enabled but fewer than two members declare "
                "write_scope, so every batch will fall back to serial"
            ),
            fix="Add write_scope (non-overlapping globs) to the module members.",
        ))
    return warnings


# ---------------------------------------------------------------------------
# Combined check
# ---------------------------------------------------------------------------


def run_all_checks(data: dict[str, Any]) -> list[ValidationWarning]:
    """Run all resource and permission checks.

    Parameters
    ----------
    data:
        Parsed agents.yaml content (a dict).

    Returns
    -------
    list[ValidationWarning]
        Combined list of warnings from all checks.
    """
    warnings: list[ValidationWarning] = []
    warnings.extend(check_resource_availability(data))
    warnings.extend(check_permissions(data))
    warnings.extend(check_chunks(data))
    warnings.extend(check_parallel(data))
    return warnings
