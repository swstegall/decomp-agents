"""Merge worker-completed branches back into the canonical base branch.

Worktree topology gotcha
------------------------
Git refuses to check out the same branch in two worktrees. The user's
main meteor-decomp checkout typically sits on `master`, so the merge
orchestrator CANNOT just `git worktree add` a second copy of `master`.

Workaround:
  - the merge worktree lives on a dedicated `_merge/staging` branch
  - at the top of each merge pass we `git reset --hard <base_branch>`
    to snap staging to wherever the real base ref is
  - after a successful merge, `git update-ref refs/heads/<base_branch>
    refs/heads/_merge/staging` advances the real base ref atomically
  - the user's main checkout sees the new base_branch SHA the next
    time it reads the ref — no worktree collision, no checkout dance

Strategy:
  1. cleanup (`git merge --abort`) + snap _merge/staging to base_branch
  2. check the branch exists + isn't a no-op (SHA == base) before merging
  3. `git merge --no-ff <branch>` on staging
  4. on success → optional stamp + update-ref base_branch + (optionally) push
  5. on conflict → run classify_conflicts(); each path gets a strategy:
     - config/<bin>.yaml         → take theirs (each claim's own row)
     - src/<bin>/_rosetta/*.cpp  → prefer hand-written over stamped
                                    (workers occasionally re-write a
                                    sibling .cpp during their iterations;
                                    that's harmless if we keep one of
                                    the two functionally-equivalent
                                    versions — see the audit in
                                    docs/git-shape-conflicts.md)
     - decomp-notes/blocked/*.md → append-merge (post-mortems concat)
     - decomp-notes/types/*.md   → take theirs (newer type discovery wins)
     - anything else             → escalate
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .work_queue import ClaimRecord, WorkQueue
from .worktree import current_sha, provision

log = logging.getLogger(__name__)

_YAML_PATH_RE = re.compile(r"^config/ffxiv[a-z]+\.yaml$")
_ROSETTA_PATH_RE = re.compile(r"^src/ffxiv[a-z]+/_rosetta/FUN_[0-9a-fA-F]+\.cpp$")
_NOTES_BLOCKED_PATH_RE = re.compile(r"^decomp-notes/blocked/ffxiv[a-z]+/.+\.md$")
_NOTES_TYPES_PATH_RE = re.compile(r"^decomp-notes/types/ffxiv[a-z]+/.+\.md$")

# First-line discriminator written by tools/stamp_clusters.py.
_STAMPED_FIRST_LINE = "// [STAMPED]"

# Auto-resolution strategies a path can receive from the classifier.
_S_THEIRS = "theirs"
_S_OURS = "ours"
_S_PREFER_HANDWRITTEN = "prefer-handwritten"
_S_APPEND_MD = "append-md"

STAGING_BRANCH = "_merge/staging"

# Walltime cap for the post-merge stamp+validate pass. The validator
# uses compile-once-share-many so the cost is one cl.exe per cluster
# (~5 s) plus per-sibling compare.py (~50 ms). Even a 1,500-sibling
# cluster lands well under this — the cap is just to keep a runaway
# from blocking the merge loop forever.
_STAMP_TIMEOUT_SECONDS = 30 * 60

# Cap how much of a conflicted file's content we capture in the
# escalation payload. Most _rosetta/*.cpp are 50–200 lines; this gives
# enough room without bloating the JSON.
_ESCALATION_CONTENT_CAP_BYTES = 8192


@dataclass
class MergeOutcome:
    claim_id: int
    branch: str
    result: str  # merged | conflict_resolved | conflict_escalated | reverted | skipped
    base_sha_before: str
    base_sha_after: str
    notes: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _conflicted_paths(merge_repo: Path) -> list[Path]:
    raw = _git(merge_repo, "diff", "--name-only", "--diff-filter=U", check=False)
    return [Path(p) for p in raw.stdout.splitlines() if p.strip()]


def _yaml_only_conflicts(paths: list[Path]) -> bool:
    """Backwards-compatible predicate kept for the existing smoke test.

    The current merge loop uses :func:`classify_conflicts` instead. This
    helper still returns True iff every path is a YAML row — a strict
    subset of what the broader classifier handles, so existing callers
    keep working without re-thinking.
    """
    return bool(paths) and all(_YAML_PATH_RE.match(str(p)) for p in paths)


@dataclass
class _ConflictPlan:
    """Per-merge dispatch table built by :func:`classify_conflicts`.

    A plan is fully auto-resolvable iff ``escalate`` is empty. The merge
    driver walks ``strategies`` and applies each one in order; any
    failure inside a strategy mid-walk causes the whole merge to
    escalate (we can't half-resolve and ship).
    """

    strategies: list[tuple[Path, str]] = field(default_factory=list)
    escalate: list[tuple[Path, str]] = field(default_factory=list)


def classify_conflicts(paths: list[Path]) -> _ConflictPlan:
    """Decide an auto-resolution strategy for each conflicted path.

    Path categories and their recovery rules:

    - ``config/<bin>.yaml`` — take theirs. Each claim's only YAML edit
      is flipping its own row; conflicts here are stale-base-row noise.
    - ``src/<bin>/_rosetta/FUN_<rva>.cpp`` — prefer hand-written. Both
      sides may have added the same sibling .cpp during their work;
      the auto-stamper writes a ``// [STAMPED]`` header that lets us
      detect which side is auto-generated. When both sides are
      equivalent (both stamped, or both hand-written), take theirs.
    - ``decomp-notes/blocked/<bin>/<rva>_<sym>.md`` — append-merge.
      Post-mortems are additive; preserve both observations.
    - ``decomp-notes/types/<bin>/<rva>.md`` — take theirs. Workers
      that successfully match overwrite prior type discovery notes.
    - Anything else (incl. ``decomp-notes/idioms/`` which is curator-
      only, and shared ``include/`` headers) — escalate.
    """
    plan = _ConflictPlan()
    for p in paths:
        s = str(p)
        if _YAML_PATH_RE.match(s):
            plan.strategies.append((p, _S_THEIRS))
        elif _ROSETTA_PATH_RE.match(s):
            plan.strategies.append((p, _S_PREFER_HANDWRITTEN))
        elif _NOTES_BLOCKED_PATH_RE.match(s):
            plan.strategies.append((p, _S_APPEND_MD))
        elif _NOTES_TYPES_PATH_RE.match(s):
            plan.strategies.append((p, _S_THEIRS))
        else:
            plan.escalate.append((p, f"no auto-resolver for path category: {s}"))
    return plan


def _read_index_stage(repo: Path, stage: int, path: Path) -> str | None:
    """Read git's index stage (2=ours, 3=theirs) for a conflicted path.

    Returns None if the side didn't have the file (e.g. add/add with
    only one side present — though that's not actually a conflict).
    """
    r = _git(repo, "show", f":{stage}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def _is_stamped(text: str | None) -> bool:
    return bool(text) and text.strip().startswith(_STAMPED_FIRST_LINE)


def _pick_rosetta_version(repo: Path, path: Path) -> tuple[bool, str]:
    """Resolve a `_rosetta/FUN_<rva>.cpp` ADD/ADD conflict.

    Preference order:
      1. Hand-written beats stamped (the ``// [STAMPED]`` marker is the
         discriminator — anything else is human-or-LLM-authored).
      2. If both sides are stamped, take theirs (newer commit).
      3. If both are hand-written, take theirs (newer commit). Either
         version is GREEN-validated by the time it lands in a branch,
         so either compiles to byte-identical bytes; preferring the
         incoming branch keeps the merge ordering monotonic.
    """
    ours = _read_index_stage(repo, 2, path)
    theirs = _read_index_stage(repo, 3, path)

    if ours is None and theirs is None:
        return False, f"both sides empty at {path}"
    if ours is None:
        return _checkout_side(repo, path, "theirs", note=f"theirs only: {path}")
    if theirs is None:
        return _checkout_side(repo, path, "ours", note=f"ours only: {path}")

    ours_stamped = _is_stamped(ours)
    theirs_stamped = _is_stamped(theirs)

    if ours_stamped and not theirs_stamped:
        return _checkout_side(
            repo, path, "theirs", note=f"theirs hand-written > ours stamped: {path}"
        )
    if theirs_stamped and not ours_stamped:
        return _checkout_side(
            repo, path, "ours", note=f"ours hand-written > theirs stamped: {path}"
        )
    # Either both stamped or both hand-written: pick the incoming branch.
    kind = "both stamped" if ours_stamped else "both hand-written"
    return _checkout_side(
        repo, path, "theirs", note=f"theirs ({kind}, equal preference): {path}"
    )


def _checkout_side(
    repo: Path, path: Path, side: str, *, note: str
) -> tuple[bool, str]:
    """Run `git checkout --<side> <path> && git add <path>`."""
    co = _git(repo, "checkout", f"--{side}", str(path), check=False)
    if co.returncode != 0:
        return False, f"checkout --{side} failed: {co.stderr.strip()[:160]}"
    add = _git(repo, "add", str(path), check=False)
    if add.returncode != 0:
        return False, f"git add failed: {add.stderr.strip()[:160]}"
    return True, note


def _append_merge_md(repo: Path, path: Path) -> tuple[bool, str]:
    """Concat both versions of a markdown note (post-mortem). Always wins.

    Post-mortems under ``decomp-notes/blocked/`` are additive — each
    block describes an attempt, separated by a horizontal rule. If two
    branches each appended a block to the same file, both observations
    deserve to survive.
    """
    ours = _read_index_stage(repo, 2, path) or ""
    theirs = _read_index_stage(repo, 3, path) or ""
    if not (ours or theirs):
        return False, f"append-merge: both stages empty at {path}"
    merged = ours.rstrip() + "\n\n---\n\n" + theirs.lstrip()
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(merged)
    add = _git(repo, "add", str(path), check=False)
    if add.returncode != 0:
        return False, f"git add failed: {add.stderr.strip()[:160]}"
    return True, f"append-merge ({len(ours)} + {len(theirs)} bytes): {path}"


def _apply_strategy(repo: Path, path: Path, strategy: str) -> tuple[bool, str]:
    """Dispatch one (path, strategy) pair. Returns (ok, note)."""
    if strategy in (_S_THEIRS, _S_OURS):
        return _checkout_side(
            repo,
            path,
            "theirs" if strategy == _S_THEIRS else "ours",
            note=f"{strategy}: {path}",
        )
    if strategy == _S_PREFER_HANDWRITTEN:
        return _pick_rosetta_version(repo, path)
    if strategy == _S_APPEND_MD:
        return _append_merge_md(repo, path)
    return False, f"unknown strategy {strategy!r} for {path}"


def _abort(merge_repo: Path) -> None:
    _git(merge_repo, "merge", "--abort", check=False)


def _cleanup_stale_merge_state(merge_repo: Path) -> None:
    """Ensure the staging worktree isn't mid-merge from a previous crash.

    `git reset --hard` doesn't abort an in-progress merge; if a prior
    pass was SIGKILLed between `git merge` and `git commit`, MERGE_HEAD
    will still exist and the next `git merge` will refuse with "you
    have not concluded your merge (MERGE_HEAD exists)". Call this
    before every merge pass.
    """
    # `git merge --abort` is a no-op if no merge is in progress (rc=128
    # with stderr "There is no merge to abort."). Suppress via check=False.
    _git(merge_repo, "merge", "--abort", check=False)
    # Also kill any unmerged paths the abort might not have caught
    # (rare, but cheap insurance).
    _git(merge_repo, "reset", "--hard", "HEAD", check=False)


def _branch_exists_locally(repo: Path, branch: str) -> bool:
    r = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    return r.returncode == 0


def _branch_sha(repo: Path, branch: str) -> str | None:
    r = _git(repo, "rev-parse", f"refs/heads/{branch}", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _has_remote(repo: Path) -> bool:
    result = _git(repo, "remote", check=False)
    return bool(result.stdout.strip())


def _push(
    repo: Path,
    refspec: str,
    *,
    label: str,
) -> tuple[bool, str]:
    """Push `refspec` (local:remote or just branch) to origin.

    Returns (success, stderr-or-stdout). Never raises — push failures
    are logged and surfaced to the caller, but don't abort the merge
    or the worker.
    """
    if not _has_remote(repo):
        return False, "no remote configured"
    result = _git(repo, "push", "origin", refspec, check=False)
    if result.returncode == 0:
        log.info("push %s OK: %s", label, refspec)
        return True, result.stdout.strip()
    msg = (result.stderr or result.stdout).strip()
    log.warning("push %s FAILED (%s): %s", label, refspec, msg)
    return False, msg


def _escalation_payload(
    claim: ClaimRecord,
    paths: list[Path],
    reason: str,
    *,
    merge_repo: Path | None = None,
    classifier_reasons: dict[str, str] | None = None,
) -> dict:
    """Build the escalation JSON for the human / future LLM resolver.

    When ``merge_repo`` is provided, also capture per-path index-stage
    content (ours / theirs, truncated to ``_ESCALATION_CONTENT_CAP_BYTES``)
    plus the line-by-line diff. That gives an offline reader enough
    state to act without re-running the merge.
    """
    out: dict = {
        "claim_id": claim.id,
        "branch": claim.branch,
        "function": {
            "binary": claim.function.binary,
            "rva": claim.function.rva_hex,
            "symbol": claim.function.symbol,
        },
        "conflicted_paths": [str(p) for p in paths],
        "reason": reason,
        "escalated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if classifier_reasons:
        out["classifier_reasons"] = classifier_reasons
    if merge_repo is not None:
        out["snapshots"] = [
            _snapshot_conflict(merge_repo, p) for p in paths
        ]
    return out


def _snapshot_conflict(repo: Path, path: Path) -> dict:
    """Capture ours / theirs / diff for one conflicted path (truncated)."""
    ours = _read_index_stage(repo, 2, path)
    theirs = _read_index_stage(repo, 3, path)
    diff = _git(repo, "diff", ":2", ":3", "--", str(path), check=False)
    return {
        "path": str(path),
        "ours_present": ours is not None,
        "theirs_present": theirs is not None,
        "ours_stamped": _is_stamped(ours),
        "theirs_stamped": _is_stamped(theirs),
        "ours_excerpt": _truncate(ours, _ESCALATION_CONTENT_CAP_BYTES),
        "theirs_excerpt": _truncate(theirs, _ESCALATION_CONTENT_CAP_BYTES),
        "diff_excerpt": _truncate(diff.stdout, _ESCALATION_CONTENT_CAP_BYTES),
    }


def _truncate(s: str | None, cap: int) -> str | None:
    if s is None:
        return None
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n…[truncated, {len(s) - cap} more bytes]"


def _try_stamp_cluster(
    *,
    repo: Path,
    staging_wt: Path,
    binary: str,
    matched_rva: int,
    matched_symbol: str,
) -> list[str]:
    """Post-merge: stamp the just-matched function's cluster siblings.

    Pipeline (all run in the staging worktree, never touches main repo):
      1. Look up matched_rva in `build/easy_wins/<bin>.clusters_reloc.json`
         (from the main repo's build dir — staging starts empty).
      2. If singleton or no cluster info → return (fast path).
      3. Copy cluster JSONs into staging's build/easy_wins/.
      4. `tools/stamp_clusters.py <bin> --reloc` — write per-sibling
         `_rosetta/FUN_<sibling_rva>.cpp` from the just-merged template.
      5. `tools/validate_clusters.py <bin>` — compile-once-share-many
         + per-sibling compare.py (GREEN / PARTIAL / MISMATCH verdict).
      6. `tools/update_yaml_status.py <bin>` — flip YAML rows whose
         `_rosetta/.cpp` came out GREEN.
      7. Commit stamps + YAML on the staging branch. Caller advances
         the base ref to include this commit.

    Never raises — any failure logs a warning and returns notes. The
    primary merge has already landed; stamping is best-effort gravy.

    Returns human-readable notes for the merge log (may be empty list
    when the matched function is a cluster singleton — most matches
    are, so we keep the no-op path silent).
    """
    notes: list[str] = []

    clusters_path = repo / "build" / "easy_wins" / f"{binary}.clusters_reloc.json"
    if not clusters_path.exists():
        notes.append(
            f"stamp-skip: {clusters_path.relative_to(repo)} missing — "
            f"run `make stamp-reloc` once to seed cluster data"
        )
        return notes

    try:
        clusters = json.loads(clusters_path.read_text())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"stamp-skip: cannot parse {clusters_path.name}: {exc}")
        return notes

    rva_to_members: dict[int, list[dict]] = {}
    for _shape, members in clusters.items():
        for m in members:
            rva_to_members[int(m["rva"])] = members

    members = rva_to_members.get(matched_rva)
    if not members or len(members) <= 1:
        return []  # singleton — silent fast path

    siblings = [m for m in members if int(m["rva"]) != matched_rva]
    if not siblings:
        return []

    # Pre-flight: are there any siblings that don't already have a .cpp
    # under _rosetta/? If every sibling is already stamped, skip the
    # heavy validate pass (idempotent stamps are cheap, but we'd rather
    # not run cl.exe + compare.py for no incremental gain).
    image_base = 0x400000
    rosetta_dir = staging_wt / "src" / binary / "_rosetta"
    unstamped = [
        s for s in siblings
        if not (rosetta_dir / f"FUN_{int(s['rva']) + image_base:08x}.cpp").exists()
    ]
    if not unstamped:
        notes.append(
            f"stamp-skip: all {len(siblings)} sibling(s) of FUN_{matched_rva + image_base:08x} already stamped"
        )
        return notes

    log.info(
        "post-merge stamp: %s cluster_size=%d unstamped_siblings=%d",
        matched_symbol,
        len(members),
        len(unstamped),
    )

    # Stage the cluster JSONs into the staging worktree's build dir.
    # validate_clusters.py reads the (non-reloc) clusters.json for its
    # "find a primary in this cluster" lookup, so copy both when present.
    # `_merge/build` is often a symlink to the main `build/` (build outputs
    # are gitignored and shared across worktrees), in which case the copy
    # is a no-op onto itself — skip it.
    staging_easy_wins = staging_wt / "build" / "easy_wins"
    staging_easy_wins.mkdir(parents=True, exist_ok=True)

    def _stage(src: Path) -> None:
        dst = staging_easy_wins / src.name
        if dst.exists() and dst.resolve() == src.resolve():
            return
        shutil.copy2(src, dst)

    _stage(clusters_path)
    sibling_json = repo / "build" / "easy_wins" / f"{binary}.clusters.json"
    if sibling_json.exists():
        _stage(sibling_json)

    # 1) stamp
    stamp = _run_tool(
        staging_wt,
        ["tools/stamp_clusters.py", binary, "--reloc"],
        label="stamp_clusters",
    )
    if stamp is None:
        notes.append("stamp-fail: stamp_clusters.py errored — see merge log")
        return notes
    notes.append(f"stamp: {_last_line(stamp)}")

    # 2) validate (compile-once-share-many + per-sibling compare)
    validate = _run_tool(
        staging_wt,
        ["tools/validate_clusters.py", binary],
        label="validate_clusters",
    )
    if validate is None:
        notes.append("stamp-fail: validate_clusters.py errored — leaving stamped .cpp's uncommitted")
        # Revert unstaged _rosetta/ additions so the staging branch
        # doesn't carry orphaned .cpp's that never validated.
        _git(staging_wt, "checkout", "--", f"src/{binary}/_rosetta/", check=False)
        return notes
    notes.append(f"validate: {_last_line(validate)}")

    # 3) flip YAML rows for GREEN verdicts
    yaml_sync = _run_tool(
        staging_wt,
        ["tools/update_yaml_status.py", binary],
        label="update_yaml_status",
    )
    if yaml_sync is not None:
        notes.append(f"yaml: {_last_line(yaml_sync)}")

    # 4) commit if anything changed
    _git(staging_wt, "add", f"src/{binary}/_rosetta/", f"config/{binary}.yaml", check=False)
    cached = _git(staging_wt, "diff", "--cached", "--quiet", check=False)
    if cached.returncode == 0:
        notes.append("stamp: no net new files to commit")
        return notes

    msg = (
        f"stamp: cluster of {matched_symbol} ({len(unstamped)} sibling(s))\n\n"
        f"Post-merge cluster stamp triggered by the matching of "
        f"{matched_symbol}.  See build/easy_wins/{binary}.stamp.log and "
        f"build/easy_wins/{binary}.validate_results.json."
    )
    commit = _git(staging_wt, "commit", "-m", msg, check=False)
    if commit.returncode != 0:
        notes.append(
            f"stamp-fail: commit returned rc={commit.returncode}: "
            f"{(commit.stderr or commit.stdout).strip()[:160]}"
        )
        return notes
    notes.append(f"stamp-commit: {current_sha(staging_wt)[:10]}")
    return notes


def _run_tool(cwd: Path, argv: list[str], *, label: str) -> str | None:
    """Run a meteor-decomp `tools/*.py` script. Returns stdout on success, None on failure."""
    cmd = [sys.executable, *argv]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_STAMP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.warning("%s timed out after %d s in %s", label, _STAMP_TIMEOUT_SECONDS, cwd)
        return None
    if result.returncode != 0:
        log.warning(
            "%s rc=%d (cwd=%s): %s",
            label,
            result.returncode,
            cwd,
            (result.stderr or result.stdout).strip()[-400:],
        )
        return None
    return result.stdout or ""


def _last_line(stdout: str) -> str:
    """Last non-empty stdout line — most of the tools print a one-line summary."""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            return line.strip()
    return "(no output)"


def _advance_base_and_maybe_push(
    *,
    wt_path: Path,
    base_branch: str,
    push_master: bool,
) -> tuple[str, list[str]]:
    """Fast-forward the real base_branch ref to staging's HEAD and (optionally) push.

    Returns (new base SHA, list of human-readable notes).
    """
    notes: list[str] = []
    new_sha = current_sha(wt_path)
    _git(
        wt_path,
        "update-ref",
        f"refs/heads/{base_branch}",
        new_sha,
    )
    notes.append(f"advanced refs/heads/{base_branch} → {new_sha[:10]}")
    if push_master:
        ok, msg = _push(wt_path, f"{new_sha}:refs/heads/{base_branch}", label=base_branch)
        notes.append(f"push {base_branch}: {'ok' if ok else 'FAIL (' + msg + ')'}")
    return new_sha, notes


def merge_one(
    *,
    queue: WorkQueue,
    repo: Path,
    base_branch: str,
    merge_worktree_path: Path,
    claim: ClaimRecord,
    escalation_dir: Path,
    push_branches: bool = True,
    push_master: bool = True,
    post_merge_stamp: bool = False,
) -> MergeOutcome:
    """Merge a single completed claim's branch into `base_branch`.

    Topology: the merge happens on a dedicated `_merge/staging` branch
    so we never collide with the user's parent-checkout branch.
    """
    # Provision (or reuse) the merge worktree on the dedicated staging branch.
    wt = provision(
        repo=repo,
        target=merge_worktree_path,
        branch=STAGING_BRANCH,
        agent_id=-1,
        base_branch=base_branch,
    )

    # Defense in depth: a previous merge pass may have crashed between
    # `git merge` and `git commit` and left MERGE_HEAD behind. Abort
    # any in-flight merge before we touch the index.
    _cleanup_stale_merge_state(wt.path)

    # Snap the staging branch to whatever the real base_branch is now.
    _git(wt.path, "reset", "--hard", base_branch)
    base_before = current_sha(wt.path)

    # Pre-merge guards: branch must exist + actually carry new commits.
    if not _branch_exists_locally(wt.path, claim.branch):
        log.warning(
            "claim %d: branch %s missing locally — worker likely died before push",
            claim.id,
            claim.branch,
        )
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha="",
            result="skipped",
            notes=f"branch {claim.branch} not found locally",
        )
        queue.mark_merge(claim.id, "skipped")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="skipped",
            base_sha_before=base_before,
            base_sha_after=base_before,
            notes=[f"branch {claim.branch} not found — skipped"],
        )

    branch_sha = _branch_sha(wt.path, claim.branch)
    if branch_sha and branch_sha == base_before:
        # The branch is at the same SHA as the base — nothing to merge.
        # Mark merged so the queue stops pending it.
        log.info(
            "claim %d: branch %s already at base SHA %s — no-op",
            claim.id,
            claim.branch,
            base_before[:10],
        )
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha=base_before,
            result="merged",
            notes="no-op (branch already at base SHA)",
        )
        queue.mark_merge(claim.id, "merged")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="merged",
            base_sha_before=base_before,
            base_sha_after=base_before,
            notes=["no-op merge (branch already at base SHA)"],
        )

    result = _git(wt.path, "merge", "--no-ff", claim.branch, check=False)

    if result.returncode == 0:
        # Clean merge. Optionally chain a cluster-stamp commit on top
        # before advancing the real base ref + pushing.
        stamp_notes: list[str] = []
        if post_merge_stamp:
            stamp_notes = _try_stamp_cluster(
                repo=repo,
                staging_wt=wt.path,
                binary=claim.function.binary,
                matched_rva=claim.function.rva,
                matched_symbol=claim.function.symbol,
            )
        base_after, extra_notes = _advance_base_and_maybe_push(
            wt_path=wt.path, base_branch=base_branch, push_master=push_master
        )
        if push_branches:
            ok, msg = _push(wt.path, f"refs/heads/{claim.branch}", label=claim.branch)
            extra_notes.append(
                f"push {claim.branch}: {'ok' if ok else 'FAIL (' + msg + ')'}"
            )
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha=base_after,
            result="merged",
            notes="; ".join(["clean fast-merge", *stamp_notes, *extra_notes]),
        )
        queue.mark_merge(claim.id, "merged")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="merged",
            base_sha_before=base_before,
            base_sha_after=base_after,
            notes=["clean merge", *stamp_notes, *extra_notes],
        )

    paths = _conflicted_paths(wt.path)
    if not paths:
        _abort(wt.path)
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha="",
            result="reverted",
            notes=f"merge failed with no conflicts:\n{result.stderr}",
        )
        queue.mark_merge(claim.id, "reverted")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="reverted",
            base_sha_before=base_before,
            base_sha_after=base_before,
            notes=["merge failed without conflicts; see merges DB for stderr"],
        )

    plan = classify_conflicts(paths)

    if plan.escalate:
        # At least one path has no auto-resolver — escalate the whole
        # merge. Build the payload BEFORE aborting so we can still read
        # index stages 2/3 for the snapshots; `git merge --abort` wipes
        # the unmerged-paths state.
        classifier_reasons = {str(p): reason for p, reason in plan.escalate}
        payload = _escalation_payload(
            claim,
            paths,
            reason="unrecognised path category",
            merge_repo=wt.path,
            classifier_reasons=classifier_reasons,
        )
        _abort(wt.path)
        escalation_dir.mkdir(parents=True, exist_ok=True)
        path = (
            escalation_dir
            / f"claim-{claim.id:06d}_{claim.function.binary}_{claim.function.rva_hex}.json"
        )
        path.write_text(json.dumps(payload, indent=2))
        log.warning(
            "escalated merge conflict for claim %d (%d path(s)) → %s",
            claim.id,
            len(paths),
            path,
        )
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha="",
            result="conflict_escalated",
            notes=f"escalated to {path}",
        )
        queue.mark_merge(claim.id, "conflict")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="conflict_escalated",
            base_sha_before=base_before,
            base_sha_after=base_before,
            notes=[f"escalated to {path}"],
        )

    # Every conflict has a strategy. Apply each in turn. Any single
    # failure → escalate the whole merge so we don't ship a half-resolved
    # tree.
    log.info(
        "auto-resolving %d conflict(s) for claim %d (%s)",
        len(plan.strategies),
        claim.id,
        ", ".join(f"{p}::{strat}" for p, strat in plan.strategies),
    )
    apply_notes: list[str] = []
    apply_failed: tuple[Path, str] | None = None
    for p, strategy in plan.strategies:
        ok, note = _apply_strategy(wt.path, p, strategy)
        apply_notes.append(note)
        if not ok:
            apply_failed = (p, note)
            break

    if apply_failed is not None:
        bad_path, bad_note = apply_failed
        log.warning(
            "strategy failed for claim %d at %s — escalating: %s",
            claim.id,
            bad_path,
            bad_note,
        )
        payload = _escalation_payload(
            claim,
            paths,
            reason=f"auto-resolver failed at {bad_path}: {bad_note}",
            merge_repo=wt.path,
        )
        _abort(wt.path)
        escalation_dir.mkdir(parents=True, exist_ok=True)
        path = (
            escalation_dir
            / f"claim-{claim.id:06d}_{claim.function.binary}_{claim.function.rva_hex}.json"
        )
        path.write_text(json.dumps(payload, indent=2))
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha="",
            result="conflict_escalated",
            notes=f"escalated to {path} (strategy fail: {bad_note})",
        )
        queue.mark_merge(claim.id, "conflict")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="conflict_escalated",
            base_sha_before=base_before,
            base_sha_after=base_before,
            notes=[f"escalated to {path}", *apply_notes],
        )

    # Commit the auto-resolved merge.
    summary = ", ".join(f"{p.name}({strat})" for p, strat in plan.strategies)
    commit = _git(
        wt.path,
        "commit",
        "--no-edit",
        "-m",
        f"merge: {claim.function.symbol} (auto-resolved {len(plan.strategies)} conflict(s): {summary})",
        check=False,
    )
    if commit.returncode != 0:
        log.warning("auto-resolve commit failed: %s", commit.stderr.strip())
        _abort(wt.path)
        queue.log_merge(
            claim_id=claim.id,
            base_sha=base_before,
            branch_sha="",
            result="reverted",
            notes=f"auto-resolve commit failed: {commit.stderr.strip()[:200]}",
        )
        queue.mark_merge(claim.id, "reverted")
        return MergeOutcome(
            claim_id=claim.id,
            branch=claim.branch,
            result="reverted",
            base_sha_before=base_before,
            base_sha_after=base_before,
            notes=["auto-resolve commit failed", *apply_notes],
        )

    stamp_notes: list[str] = []
    if post_merge_stamp:
        stamp_notes = _try_stamp_cluster(
            repo=repo,
            staging_wt=wt.path,
            binary=claim.function.binary,
            matched_rva=claim.function.rva,
            matched_symbol=claim.function.symbol,
        )
    base_after, extra_notes = _advance_base_and_maybe_push(
        wt_path=wt.path, base_branch=base_branch, push_master=push_master
    )
    if push_branches:
        ok, msg = _push(wt.path, f"refs/heads/{claim.branch}", label=claim.branch)
        extra_notes.append(
            f"push {claim.branch}: {'ok' if ok else 'FAIL (' + msg + ')'}"
        )
    queue.log_merge(
        claim_id=claim.id,
        base_sha=base_before,
        branch_sha=base_after,
        result="conflict_resolved",
        notes="; ".join([
            f"auto-resolved {len(plan.strategies)} conflict(s): {summary}",
            *apply_notes,
            *stamp_notes,
            *extra_notes,
        ]),
    )
    queue.mark_merge(claim.id, "merged")
    return MergeOutcome(
        claim_id=claim.id,
        branch=claim.branch,
        result="conflict_resolved",
        base_sha_before=base_before,
        base_sha_after=base_after,
        notes=[
            f"auto-resolved {len(plan.strategies)} conflict(s)",
            *apply_notes,
            *stamp_notes,
            *extra_notes,
        ],
    )


def run_merge_pass(
    *,
    queue: WorkQueue,
    session_id: int,
    repo: Path,
    base_branch: str,
    merge_worktree_path: Path,
    escalation_dir: Path,
    push_branches: bool = True,
    push_master: bool = True,
    post_merge_stamp: bool = False,
    cross_session: bool = False,
) -> list[MergeOutcome]:
    pending = queue.pending_merges(session_id, cross_session=cross_session)
    if not pending:
        return []
    log.info(
        "merge pass: %d pending claim(s)%s",
        len(pending),
        " (cross-session drain)" if cross_session else "",
    )
    outcomes: list[MergeOutcome] = []
    for claim in pending:
        outcomes.append(
            merge_one(
                queue=queue,
                repo=repo,
                base_branch=base_branch,
                merge_worktree_path=merge_worktree_path,
                claim=claim,
                escalation_dir=escalation_dir,
                push_branches=push_branches,
                push_master=push_master,
                post_merge_stamp=post_merge_stamp,
            )
        )
    return outcomes


def reset_escalated_to_pending(
    queue: WorkQueue,
    *,
    cross_session: bool,
    binary: str | None = None,
) -> list[ClaimRecord]:
    """Flip every conflict-escalated claim back to merge_status=pending.

    Run once after the auto-resolver broadens to give previously-stuck
    claims another shot. Returns the claims that were flipped (so a CLI
    can report what happened + move their escalation JSONs).
    """
    where = ["c.merge_status = 'conflict'", "c.released_at IS NOT NULL"]
    params: list = []
    if binary is not None:
        where.append("c.binary = ?")
        params.append(binary)
    sql = (
        "SELECT c.id, c.binary, c.rva, c.symbol, c.module, c.size_bytes, "
        "       c.branch, c.iterations, c.worker_id "
        "FROM claims c WHERE " + " AND ".join(where)
    )
    if not cross_session:
        sql += " AND c.session_id = (SELECT MAX(id) FROM sessions)"

    flipped: list[ClaimRecord] = []
    with queue._conn() as conn:  # noqa: SLF001 — internal coordination DB access
        for row in conn.execute(sql, params).fetchall():
            conn.execute(
                "UPDATE claims SET merge_status='pending' WHERE id = ?",
                (row["id"],),
            )
            from .work_queue import Function as _Fn  # local import: avoid cycle

            flipped.append(
                ClaimRecord(
                    id=row["id"],
                    function=_Fn(
                        binary=row["binary"],
                        rva=row["rva"],
                        end=row["rva"] + row["size_bytes"],
                        size=row["size_bytes"],
                        module=row["module"] or "_unknown",
                        symbol=row["symbol"],
                        type="matching",
                        status="matched",
                        section=".text",
                    ),
                    worker_id=row["worker_id"],
                    branch=row["branch"],
                    iterations=row["iterations"],
                )
            )
    return flipped
