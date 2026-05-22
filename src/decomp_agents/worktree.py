"""Git worktree lifecycle. Each worker runs in its own worktree of meteor-decomp."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    repo: Path
    path: Path
    branch: str
    agent_id: int


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {repo}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def existing_worktrees(repo: Path) -> dict[Path, str]:
    """Map of worktree path → checked-out branch."""
    raw = _git(repo, "worktree", "list", "--porcelain")
    result: dict[Path, str] = {}
    cur_path: Path | None = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            cur_path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch ") and cur_path is not None:
            ref = line.removeprefix("branch ").strip()
            result[cur_path] = ref.removeprefix("refs/heads/")
    return result


def provision(
    repo: Path,
    target: Path,
    branch: str,
    agent_id: int,
    base_branch: str | None = None,
) -> Worktree:
    """Idempotent: reuses the worktree if already present at `target`."""
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = existing_worktrees(repo)
    if target in existing:
        current_branch = existing[target]
        if current_branch != branch:
            log.info(
                "worktree %s exists on branch %s — switching to %s",
                target,
                current_branch,
                branch,
            )
            _git(target, "checkout", "-B", branch)
        return Worktree(repo=repo, path=target, branch=branch, agent_id=agent_id)

    base = base_branch or _current_branch(repo)
    log.info("creating worktree %s on new branch %s from %s", target, branch, base)
    _git(repo, "worktree", "add", "-B", branch, str(target), base)
    return Worktree(repo=repo, path=target, branch=branch, agent_id=agent_id)


def remove(repo: Path, target: Path, force: bool = False) -> None:
    target = target.resolve()
    existing = existing_worktrees(repo)
    if target not in existing:
        log.debug("worktree %s already absent", target)
        return
    args = ["worktree", "remove", str(target)]
    if force:
        args.insert(2, "--force")
    _git(repo, *args, check=False)


def prune(repo: Path) -> None:
    _git(repo, "worktree", "prune")


def current_sha(repo_or_worktree: Path) -> str:
    return _git(repo_or_worktree, "rev-parse", "HEAD")


def is_clean(worktree: Path) -> bool:
    return not _git(worktree, "status", "--porcelain")


def stage_and_commit(worktree: Path, message: str, paths: list[str] | None = None) -> str:
    """Stage `paths` (or all changes) and commit. Returns the new SHA, or '' if nothing to commit."""
    if paths:
        _git(worktree, "add", *paths)
    else:
        _git(worktree, "add", "-A")
    status = _git(worktree, "status", "--porcelain")
    if not status:
        return ""
    _git(worktree, "commit", "-m", message)
    return current_sha(worktree)
