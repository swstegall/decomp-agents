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

from .config import SUPPORTED_BINARIES

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


def _ensure_git_push_auth() -> None:
    """Make `git push` authenticate via the agent's gh login.

    A fork clone (whether from `gh repo fork --clone` or `git clone owner/repo`)
    gets an HTTPS `origin`, and modern GitHub rejects HTTPS password auth
    ("Password authentication is not supported for Git operations"), so the
    branch push that precedes `gh pr create` fails. `gh auth setup-git`
    registers gh as the git credential helper for github.com so HTTPS pushes use
    the gh token. Idempotent + best-effort: on failure (e.g. gh too old) warn
    and continue — an SSH `origin` or a pre-existing credential helper still works.
    """
    r = _run(["gh", "auth", "setup-git"], check=False)
    if r.returncode != 0:
        log.warning(
            "`gh auth setup-git` failed (%s); `git push` may prompt for "
            "credentials — configure SSH keys or a gh credential helper",
            (r.stderr or r.stdout).strip()[:140],
        )


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
    _ensure_git_push_auth()

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


# ---------------------------------------------------------------------------
# toolchain-artifact provisioning (distributed mode GREEN grader)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactLink:
    """One provisioned (or attempted) artifact symlink."""

    kind: str          # "orig" | "symbols"
    binary: str        # binary stem, e.g. "ffxivgame"
    src: Path          # source under <artifacts_dir>
    dst: Path          # symlink target inside the clone
    ok: bool           # True iff the symlink now points at an existing source
    reason: str        # human-readable detail (esp. on failure)


@dataclass(frozen=True)
class ArtifactProvisionResult:
    """Outcome of :func:`provision_toolchain_artifacts`.

    ``ok`` is True iff the *configured* binary's BOTH artifacts (orig +
    symbols) are now present in the clone — i.e. `make diff` can grade.
    Other binaries' artifacts are best-effort extras and never gate ``ok``.
    """

    ok: bool
    links: list[ArtifactLink]
    warning: str | None  # set (and logged) when grading will fail


def _symlink_force(src: Path, dst: Path) -> None:
    """Create/refresh a symlink dst -> src. Idempotent.

    Removes any stale entry first (broken symlink, old symlink to a
    different source, or a plain file) so reruns converge. We deliberately
    do NOT copy: the source is copyrighted SE-derived material that must
    never land inside a tracked tree. A symlink under the clone's
    gitignored `orig/` / `config/*.symbols.json` paths can never be staged.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    # `exists()` follows symlinks (False for a broken link); `is_symlink()`
    # catches the broken/stale-symlink case so we still replace it.
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.symlink_to(src)


def provision_toolchain_artifacts(
    clone_path: Path,
    artifacts_dir: Path | None,
    binary: str,
) -> ArtifactProvisionResult:
    """Symlink the user's copyright-derived toolchain artifacts into a clone.

    Distributed mode works from a fresh clone of the fork. `make diff` (the
    GREEN grader) needs two files that are gitignored in EVERY meteor-decomp
    clone — and therefore absent from a fresh one:

      - ``orig/<binary>.exe``            (the SE game binary)
      - ``config/<binary>.symbols.json`` (the Ghidra dump derived from it)

    We never ship, copy, or commit these (we cannot distribute copyrighted
    material). Instead the user points ``DECOMP_ARTIFACTS_DIR`` at a local
    meteor-decomp checkout that already holds their OWN copies, and we
    SYMLINK them into the clone. Because the clone's `.gitignore` already
    ignores ``orig/*`` and ``config/*.symbols.json``, those symlinks are
    gitignored and can never be staged / committed / pushed in the agent's
    PR — copyrighted bytes cannot leak.

    Symlinks ALL five binaries' artifacts when present (cheap, and lets one
    provisioning cover a multi-binary session), but only the *configured*
    ``binary``'s pair gates the returned ``ok``.

    Idempotent: stale symlinks are replaced. Degrades gracefully — if
    ``artifacts_dir`` is None or a source file is missing, it logs a clear
    WARNING that `make diff` grading will fail until the user sets
    ``DECOMP_ARTIFACTS_DIR`` to their own orig/symbols, and returns
    ``ok=False`` WITHOUT raising (claiming + free-set still work; only
    grading needs the artifacts).
    """
    clone_path = clone_path.resolve()
    links: list[ArtifactLink] = []

    if artifacts_dir is None:
        warning = (
            "no toolchain-artifact source configured (DECOMP_ARTIFACTS_DIR "
            "unset and DECOMP_REPO has no orig/) — `make diff` grading will "
            "FAIL in distributed mode until you point DECOMP_ARTIFACTS_DIR "
            "at a local meteor-decomp checkout holding your OWN "
            f"orig/{binary}.exe + config/{binary}.symbols.json (these "
            "copyrighted SE-derived files are never distributed). Claiming "
            "and free-set discovery still work; only grading needs them."
        )
        log.warning("%s", warning)
        return ArtifactProvisionResult(ok=False, links=links, warning=warning)

    artifacts_dir = artifacts_dir.resolve()

    # Provision all five binaries' pairs where the source exists; the
    # configured binary's pair is the one that gates `ok`.
    ordered = [binary] + [b for b in SUPPORTED_BINARIES if b != binary]
    for stem in ordered:
        for kind, src, dst in (
            (
                "orig",
                artifacts_dir / "orig" / f"{stem}.exe",
                clone_path / "orig" / f"{stem}.exe",
            ),
            (
                "symbols",
                artifacts_dir / "config" / f"{stem}.symbols.json",
                clone_path / "config" / f"{stem}.symbols.json",
            ),
        ):
            if not src.exists():
                # Only the CONFIGURED binary's missing artifacts are
                # noteworthy; other binaries are optional extras.
                if stem == binary:
                    links.append(
                        ArtifactLink(
                            kind=kind,
                            binary=stem,
                            src=src,
                            dst=dst,
                            ok=False,
                            reason=f"source missing: {src}",
                        )
                    )
                continue
            try:
                _symlink_force(src, dst)
                links.append(
                    ArtifactLink(
                        kind=kind,
                        binary=stem,
                        src=src,
                        dst=dst,
                        ok=True,
                        reason=f"symlinked {dst} -> {src}",
                    )
                )
                log.info("provisioned %s artifact: %s -> %s", stem, dst, src)
            except OSError as exc:
                links.append(
                    ArtifactLink(
                        kind=kind,
                        binary=stem,
                        src=src,
                        dst=dst,
                        ok=False,
                        reason=f"symlink failed: {exc}",
                    )
                )
                log.warning("failed to symlink %s -> %s: %s", dst, src, exc)

    # Grading needs BOTH artifacts for the configured binary.
    cfg_links = [ln for ln in links if ln.binary == binary]
    have_orig = any(ln.kind == "orig" and ln.ok for ln in cfg_links)
    have_symbols = any(ln.kind == "symbols" and ln.ok for ln in cfg_links)
    ok = have_orig and have_symbols

    warning: str | None = None
    if not ok:
        missing = []
        if not have_orig:
            missing.append(f"orig/{binary}.exe")
        if not have_symbols:
            missing.append(f"config/{binary}.symbols.json")
        warning = (
            f"could not provision {', '.join(missing)} from {artifacts_dir} "
            f"— `make diff` grading will FAIL until your DECOMP_ARTIFACTS_DIR "
            f"checkout holds these (run `make split BINARY={binary}.exe` there "
            "if needed). Claiming + free-set still work; only grading needs them."
        )
        log.warning("%s", warning)

    return ArtifactProvisionResult(ok=ok, links=links, warning=warning)
