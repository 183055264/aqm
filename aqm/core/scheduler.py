"""Work-block scheduling: decide which members may run a turn in parallel.

The scheduler owns *strategy*, never *assignment*:

* assignment (which member owns which work block) lives on ``Chunk.owner`` and
  is produced by the architecture/split stage or typed in by a human;
* strategy (how many members run at once, and in which order) is decided here,
  deterministically, from the declared write scopes and the project settings.

Design rules:

* A member without a ``write_scope`` never runs in parallel — we cannot prove
  it stays inside its own files, and all members share one project directory.
* Two members whose scopes may touch the same path never run together.
* A member whose scope covers a "hot file" (lockfiles, package manifests, …)
  always runs alone.
* When fewer than two members are eligible the batch degrades to serial and
  the reason is reported so the console can explain *why*.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Directories that are never part of a member's write scope walks.
SCOPE_IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".aqm", "node_modules", "__pycache__", ".venv",
    "venv", "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage",
}

DEFAULT_PARALLEL_SETTINGS: dict = {
    "enabled": False,
    "max_workers": 2,
    "max_rounds": 40,
    "on_conflict": "block",   # block | warn | serialize
    "audit": True,
    "audit_max_files": 4000,
    # Copy the files each parallel round may touch into
    # .aqm/checkpoints/<task>/stage-NNN/ so a bad write can be recovered
    # without relying on git.
    "checkpoint": True,
    "checkpoint_max_files": 400,
    "hot_files": [
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.txt",
        "pyproject.toml",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        ".env",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
    ],
}


def resolve_parallel_settings(raw: dict | None) -> dict:
    """Merge project/pipeline ``parallel`` settings with the defaults."""
    settings = {
        key: (list(value) if isinstance(value, list) else value)
        for key, value in DEFAULT_PARALLEL_SETTINGS.items()
    }
    for key, value in (raw or {}).items():
        if key in settings and value is not None:
            settings[key] = value
    try:
        settings["max_workers"] = max(1, int(settings.get("max_workers", 2)))
    except (TypeError, ValueError):
        settings["max_workers"] = 2
    try:
        settings["max_rounds"] = max(1, int(settings.get("max_rounds", 40)))
    except (TypeError, ValueError):
        settings["max_rounds"] = 40
    mode = str(settings.get("on_conflict") or "block").lower()
    if mode not in {"block", "warn", "serialize"}:
        mode = "block"
    settings["on_conflict"] = mode
    settings["enabled"] = bool(settings.get("enabled"))
    settings["audit"] = bool(settings.get("audit", True))
    settings["checkpoint"] = bool(settings.get("checkpoint", True))
    try:
        settings["checkpoint_max_files"] = max(
            0, int(settings.get("checkpoint_max_files", 400))
        )
    except (TypeError, ValueError):
        settings["checkpoint_max_files"] = 400
    return settings


# ---------------------------------------------------------------------------
# Scope globs
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> str:
    """Translate a scope glob into a regular expression (path separators '/')."""
    pattern = pattern.replace("\\", "/").strip().strip("/")
    if not pattern:
        return r"^.*$"  # empty scope == whole project
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**/", i):
                out.append(r"(?:.*/)?")  # "**/" also matches zero dirs
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append(r"[^/]")
            i += 1
            continue
        out.append(re.escape(ch))
        i += 1
    return r"^" + "".join(out) + r"$"


def scope_matches(scope: str, relpath: str) -> bool:
    """True when *relpath* (posix, project-relative) is inside *scope*."""
    if relpath.startswith("./"):
        relpath = relpath[2:]
    relpath = relpath.replace("\\", "/")
    pattern = (scope or "").replace("\\", "/").strip()
    if not pattern:
        return True
    candidate = pattern
    if not any(ch in pattern for ch in "*?[]"):
        # A plain path is either a file or a directory prefix.
        if relpath == pattern.lstrip("/"):
            return True
        candidate = pattern.rstrip("/") + "/**"
    if re.match(_glob_to_regex(candidate), relpath):
        return True
    # Directory prefix coverage: "src/ui" also covers "src/ui/x.py".
    return relpath.startswith(pattern.strip("/") + "/")


def scopes_overlap(a: str, b: str) -> bool:
    """Conservative overlap test between two write scopes."""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return True  # an empty scope is "the whole project"
    # Probe each scope with the other's static prefix.
    for scope, other in ((a, b), (b, a)):
        probe = _scope_probe(other)
        if probe is not None and scope_matches(scope, probe):
            return True
    # Fall back to a literal comparison of their static prefixes.
    pa, pb = _static_prefix(a), _static_prefix(b)
    if pa == pb:
        return True
    return pa.startswith(pb + "/") or pb.startswith(pa + "/")


def _static_prefix(scope: str) -> str:
    scope = scope.replace("\\", "/").strip("/")
    parts: list[str] = []
    for part in scope.split("/"):
        if any(ch in part for ch in "*?[]"):
            break
        parts.append(part)
    return "/".join(parts)


def _scope_probe(scope: str) -> str | None:
    """Build a representative path inside *scope*."""
    prefix = _static_prefix(scope).strip("/")
    if prefix:
        return prefix + "/_probe_"
    if "**" in scope:
        return "_probe_/" + ("x.py" if ".py" in scope else "x")
    return None


def scopes_covering(scopes: Iterable[str], relpath: str) -> list[str]:
    return [scope for scope in scopes if scope_matches(scope, relpath)]


def hot_file_hit(scopes: Iterable[str], hot_files: Iterable[str]) -> str | None:
    """Return the first hot file a scope may touch, or None."""
    for hot in hot_files:
        hot = (hot or "").replace("\\", "/")
        if not hot:
            continue
        hot_re = re.compile(_glob_to_regex(hot))
        for scope in scopes:
            prefix = _static_prefix(scope).strip("/")
            probe = prefix + "/" + hot if prefix else hot
            if hot_re.match(probe) or scope_matches(scope, hot):
                return hot
    return None


# ---------------------------------------------------------------------------
# Batch planning
# ---------------------------------------------------------------------------

@dataclass
class AgentPlan:
    """A member that could take part in a batch."""

    agent_id: str
    scopes: list[str] = field(default_factory=list)
    pending: int = 0
    has_gate: bool = False
    has_human_input: bool = False

    @property
    def parallel_ready(self) -> bool:
        return bool(self.scopes) and self.pending > 0


@dataclass
class ScheduleDecision:
    selected: list[str] = field(default_factory=list)
    waiting: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mode: str = "serial"
    max_workers: int = 1
    round: int = 0

    def as_dict(self) -> dict:
        return {
            "selected": list(self.selected),
            "waiting": list(self.waiting),
            "notes": list(self.notes),
            "mode": self.mode,
            "max_workers": self.max_workers,
            "round": self.round,
        }


def plan_batch(
    agents: Iterable[AgentPlan],
    max_workers: int,
    hot_files: Iterable[str] = (),
    on_conflict: str = "block",
    round_number: int = 0,
) -> ScheduleDecision:
    """Pick the members that may run this round, deterministically."""
    decision = ScheduleDecision(max_workers=max_workers, round=round_number)
    candidates: list[AgentPlan] = []

    for plan in sorted(agents, key=lambda item: item.agent_id):
        if plan.pending <= 0:
            decision.waiting.append({
                "agent_id": plan.agent_id, "reason": "没有待处理的工作块",
            })
            continue
        if not plan.scopes:
            decision.waiting.append({
                "agent_id": plan.agent_id,
                "reason": "未声明写范围 write_scope，串行执行更安全",
            })
            continue
        if plan.has_human_input:
            decision.waiting.append({
                "agent_id": plan.agent_id,
                "reason": "需要人工输入，无法并行",
            })
            continue
        if plan.has_gate:
            decision.waiting.append({
                "agent_id": plan.agent_id,
                "reason": "配置了审批门，需串行执行才能审批",
            })
            continue
        hot = hot_file_hit(plan.scopes, hot_files)
        if hot:
            decision.waiting.append({
                "agent_id": plan.agent_id,
                "reason": f"写范围包含热文件 {hot}，必须串行",
            })
            continue
        candidates.append(plan)

    selected: list[AgentPlan] = []
    for plan in candidates:
        if len(selected) >= max_workers:
            decision.waiting.append({
                "agent_id": plan.agent_id,
                "reason": f"已达并行上限 {max_workers}，下一轮再跑",
            })
            continue
        clash = None
        for chosen in selected:
            for a in plan.scopes:
                for b in chosen.scopes:
                    if scopes_overlap(a, b):
                        clash = (chosen.agent_id, a, b)
                        break
                if clash:
                    break
            if clash:
                break
        if clash:
            decision.waiting.append({
                "agent_id": plan.agent_id,
                "reason": (
                    f"写范围与 {clash[0]} 重叠（{clash[1]} ↔ {clash[2]}）"
                ),
            })
            continue
        selected.append(plan)

    decision.selected = [plan.agent_id for plan in selected]
    if len(decision.selected) >= 2:
        decision.mode = "parallel"
    else:
        decision.mode = "serial"
        if not decision.selected:
            decision.notes.append("本轮没有可并行的成员，改用串行执行")
        else:
            decision.notes.append(
                f"本轮只有 {decision.selected[0]} 可以运行，少于 2 人，串行执行"
            )
    if on_conflict == "serialize" and decision.mode == "parallel":
        decision.notes.append("配置为 on_conflict=serialize，冲突成员已排队")
    return decision


# ---------------------------------------------------------------------------
# Same-directory safety: snapshot + audit
# ---------------------------------------------------------------------------

def snapshot_scopes(
    root: Path, scopes: Iterable[str], max_files: int = 4000,
    ignored_dirs: Iterable[str] = SCOPE_IGNORED_DIRS,
) -> dict[str, tuple[int, int]] | None:
    """Cheap snapshot (size, mtime_ns) of every file inside *scopes*.

    Returns ``None`` when the scope set is too large to audit (or empty), so the
    caller can report that the audit was skipped.  An empty dict simply means
    "the scopes currently contain no files".
    """
    root = Path(root)
    scopes = [scope for scope in scopes if scope and scope.strip()]
    if not scopes:
        return None
    ignored = {name.lower() for name in ignored_dirs}
    snapshot: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ignored]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not any(scope_matches(scope, rel) for scope in scopes):
                continue
            try:
                stat = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            snapshot[rel] = (stat.st_size, stat.st_mtime_ns)
            if len(snapshot) >= max_files:
                return None
    return snapshot


def diff_snapshot(
    before: dict[str, tuple[int, int]] | None,
    after: dict[str, tuple[int, int]] | None,
) -> list[str]:
    """Files created, deleted or modified between two snapshots."""
    if before is None or after is None:
        return []
    changed = []
    for path, meta in after.items():
        if before.get(path) != meta:
            changed.append(path)
    for path in before:
        if path not in after:
            changed.append(path)
    return sorted(changed)


def audit_changes(
    scopes_by_agent: dict[str, list[str]],
    changed_files: Iterable[str],
) -> tuple[list[dict], list[str]]:
    """Attribute changed files to scope owners.

    Concurrency makes exact attribution impossible without per-write hooks, so
    this is an audit: a file covered by two or more selected scopes, or by none
    of them, is reported.  Returns (conflicts, unclaimed).
    """
    conflicts: list[dict] = []
    unclaimed: list[str] = []
    for path in changed_files:
        owners = [
            agent_id for agent_id, scopes in scopes_by_agent.items()
            if any(scope_matches(scope, path) for scope in scopes)
        ]
        if len(owners) > 1:
            conflicts.append({"path": path, "agents": sorted(owners)})
        elif not owners:
            unclaimed.append(path)
    return conflicts, unclaimed


def copy_checkpoint(
    root: Path,
    scopes: Iterable[str],
    dest: Path,
    max_files: int = 400,
) -> dict:
    """Copy every file inside *scopes* into *dest* and write a manifest.

    Returns ``{"ok": True, "count": n, "dir": str(dest)}`` or
    ``{"ok": False, "reason": "..."}`` when the scope is too large.
    """
    import json
    import shutil

    root = Path(root)
    scopes = [scope for scope in scopes if scope and str(scope).strip()]
    if not scopes:
        return {"ok": False, "reason": "没有声明写范围"}
    ignored = {name.lower() for name in SCOPE_IGNORED_DIRS}
    files: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ignored]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not any(scope_matches(scope, rel) for scope in scopes):
                continue
            files.append((rel, Path(dirpath) / name))
            if len(files) > max_files:
                return {
                    "ok": False,
                    "reason": f"写范围内文件超过 {max_files} 个，已跳过快照",
                }
    dest = Path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for rel, src in files:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        (dest / "manifest.json").write_text(
            json.dumps(
                {
                    "scope": list(scopes),
                    "files": [rel for rel, _ in files],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "reason": f"快照写入失败：{exc}"}
    return {"ok": True, "count": len(files), "dir": str(dest)}