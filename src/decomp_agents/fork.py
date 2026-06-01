"""Fork provisioning for distributed mode (issue #11, D5).

The local mode runs N git *worktrees* of a single meteor-decomp checkout
(see worktree.py). Distributed mode instead works from a single working
*clone of the contributor's FORK* of the upstream repo, with the canonical
upstream wired up as a second remote so the agent can:

  - track the upstream solved set + claims ledger (`git fetch upstream`)
  - cut a per-function branch off the up-to-date upstream base
  - push that branch to the fork (its `origin`)
  - open a cross-repo PR (`<fork-owner>:<branch>` -> `upstream:develop`)

This module mirrors worktree.py's *role* (provision a clean working tree
on a per-function branch) but for the fork topology. It shells out to
`git`/`gh` like the rest of the project, and uses the agent's OWN gh auth
(its own GitHub identity) — there is no shared bot token here.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class ForkError(RuntimeError):
    pass


@dataclass(frozen=True)
class ForkClone:
    """A provisioned working clone of the contributor's fork."""

    path: Path
    upstream: str          # "owner/repo"
    fork_owner: str        # login that owns `origin`
    upstream_branch: str   # base branch on upstream (e.g. "develop")


def _run(
    argv: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise ForkError(
            f"command failed ({' '.join(argv)}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _git(clone: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(clone), *args], check=check)


def gh_login() -> str:
    """The agent's own authenticated GitHub login (its identity).

    Distributed claims are attributed to whoever runs the agent — the
    upstream claim.yml binds `owner` to the AUTHENTICATED comment author,
    never to body data. So the agent must know its own login to recognise
    its own wins when polling the ledger.
    """
    result = _run(["gh", "api", "user", "--jq", ".login"], check=True)
    login = result.stdout.strip()
    if not login:
        raise ForkError("gh api user returned an empty login — is `gh auth login` done?")
    return login


def _remote_url(clone: Path, name: str) -> str | None:
    r = _git(clone, "remote", "get-url", name, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _ensure_upstream_remote(clone: Path, upstream: str) -> None:
    """Add (or re-point) an `upstream` remote at the canonical repo."""
    want = f"https://github.com/{upstream}.git"
    have = _remote_url(clone, "upstream")
    if have is None:
        _git(clone, "remote", "add", "upstream", want)
    elif have.rstrip("/").removesuffix(".git") != want.rstrip("/").removesuffix(".git"):
        _git(clone, "remote", "set-url", "upstream", want)


def _origin_owner(clone: Path, fallback: str) -> str:
    """Parse the `origin` (fork) owner from its URL; fall back to `fallback`."""
    url = _remote_url(clone, "origin") or ""
    # Accept both https://github.com/<owner>/<repo>(.git) and
    # git@github.com:<owner>/<repo>(.git).
    tail = url
    for sep in ("github.com/", "github.com:"):
        if sep in tail:
            tail = tail.split(sep, 1)[1]
            break
    tail = tail.rstrip("/").removesuffix(".git")
    parts = tail.split("/")
    if len(parts) >= 2 and parts[-2]:
        return parts[-2]
    return fallback


def provision_fork(
    *,
    upstream: str,
    fork: str,
    target: Path,
    upstream_branch: str = "develop",
    agent_login: str | None = None,
) -> ForkClone:
    """Provision a working clone of the contributor's fork.

    Idempotent: if `target` already holds a clone, reuse it (just refresh
    the `upstream` remote + fetch). Otherwise create it:

      - if `fork` is empty: `gh repo fork <upstream> --clone` creates the
        fork under the agent's identity (a no-op if it already exists) and
        clones it, wiring `upstream` automatically.
      - if `fork` is "owner/repo" or a URL: `git clone` it directly, then
        add the `upstream` remote by hand.

    Then `git fetch upstream <branch>` so the solved set + base ref are
    current. Returns a :class:`ForkClone`.
    """
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    login = agent_login or gh_login()

    if (target / ".git").exists():
        log.info("fork clone already present at %s — reusing", target)
        _ensure_upstream_remote(target, upstream)
    elif not fork:
        # gh wires the `upstream` remote for us when forking+cloning.
        log.info("forking %s and cloning into %s (gh repo fork)", upstream, target)
        _run(
            [
                "gh",
                "repo",
                "fork",
                upstream,
                "--clone",
                f"--fork-name={upstream.split('/')[-1]}",
                "--",
                str(target),
            ],
            check=True,
        )
        _ensure_upstream_remote(target, upstream)
    else:
        url = fork if "://" in fork or fork.startswith("git@") else f"https://github.com/{fork}.git"
        log.info("cloning fork %s into %s", url, target)
        _run(["git", "clone", url, str(target)], check=True)
        _ensure_upstream_remote(target, upstream)

    # Refresh the upstream base + the claims ledger branch so the solved
    # set and lease ledger the agent reasons about are current.
    _git(target, "fetch", "upstream", upstream_branch, check=False)
    _git(target, "fetch", "upstream", "claims", check=False)

    fork_owner = _origin_owner(target, fallback=login)
    return ForkClone(
        path=target,
        upstream=upstream,
        fork_owner=fork_owner,
        upstream_branch=upstream_branch,
    )


def function_branch_name(rva: int, symbol: str) -> str:
    """Per-function branch name: `agents/<rva_hex>_<safe_sym>`.

    Mirrors work_queue.Function.safe_branch_name's sanitising rules but
    without the `<binary>/` prefix (the fork branch lives under one repo;
    the binary is implied by the VA + PR title).
    """
    safe_sym = symbol.replace("::", "__").replace("/", "_").replace(".", "_")
    return f"agents/0x{rva:08x}_{safe_sym}"


def start_function_branch(
    clone: ForkClone, *, rva: int, symbol: str
) -> str:
    """Cut (or reset) a fresh per-function branch off the upstream base.

    Snaps to `upstream/<branch>` so the working tree carries the latest
    solved set before the agent starts matching. Returns the branch name.
    """
    branch = function_branch_name(rva, symbol)
    base = f"upstream/{clone.upstream_branch}"
    # Make sure the base ref exists locally; a fresh clone may not have
    # fetched it yet under this exact name.
    _git(clone.path, "fetch", "upstream", clone.upstream_branch, check=False)
    _git(clone.path, "checkout", "-B", branch, base)
    log.info("started function branch %s off %s", branch, base)
    return branch


def push_function_branch(clone: ForkClone, branch: str) -> tuple[bool, str]:
    """Push the per-function branch to the fork's `origin`. Best-effort.

    Returns (ok, message). Never raises so a push hiccup doesn't abort
    the agent mid-claim.
    """
    r = _git(clone.path, "push", "-u", "origin", branch, check=False)
    if r.returncode == 0:
        return True, r.stdout.strip()
    return False, (r.stderr or r.stdout).strip()
