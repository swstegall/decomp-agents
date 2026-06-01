"""Free-set discovery for distributed mode (issue #11, D5).

The local queue (work_queue.py::filter_eligible + claim_next) excludes a VA
via the SQLite ledger. In distributed mode there is no shared SQLite — the
exclusion sources are all on GitHub:

  (a) SOLVED       — `src/<bin>/_rosetta/FUN_<va>.cpp` exists on the
                     upstream base tree (the committed tree IS the solved
                     set, per tools/claim.py).
  (b) LIVE CLAIM   — a non-expired / pinned entry in the upstream `claims`
                     ledger (`claims/<bin>/<decimal_rva>.json`).
  (c) OPEN PR      — a VA whose `_rosetta/FUN_<va>.cpp` is ADDED by an open
                     upstream PR (so two agents don't both PR the same VA
                     before the claim ledger catches up).

This module computes those three exclusion sets and subtracts them from the
binary's eligible function pool (reusing work_queue's Function + the same
size / type / status / diff-compat filters). The PURE core,
:func:`eligible_functions`, takes the three exclusion sets as plain
arguments so it can be unit-tested with injected inputs. The network-backed
:func:`discover_free_set` is the thin wrapper that gathers them from a fork
clone via git + gh.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from .work_queue import Function, WorkQueue

log = logging.getLogger(__name__)

IMAGE_BASE = 0x400000

# _rosetta solved-file name: FUN_<va_hex>.cpp (hex, no 0x, any width).
_RE_FUN_NAME = re.compile(r"^FUN_([0-9a-fA-F]+)\.cpp$")
# Ledger file name: <decimal_rva>.json
_RE_CLAIM_NAME = re.compile(r"^([0-9]+)\.json$")
# Match a _rosetta path inside an arbitrary repo subtree (PR file lists,
# `git show` output): src/<bin>/_rosetta/FUN_<va>.cpp
_RE_ROSETTA_PATH = re.compile(
    r"(?:^|/)src/(?P<bin>ffxiv[a-z]+)/_rosetta/FUN_(?P<va>[0-9a-fA-F]+)\.cpp$"
)


def rva_to_va(rva: int, image_base: int = IMAGE_BASE) -> int:
    return rva + image_base


def va_to_rva(va: int, image_base: int = IMAGE_BASE) -> int:
    return va - image_base


# ---------------------------------------------------------------------------
# exclusion-set parsing (pure / filesystem-only) — unit-testable
# ---------------------------------------------------------------------------


def solved_rvas(
    binary: str, *, src_root: Path, image_base: int = IMAGE_BASE
) -> set[int]:
    """RVAs already solved: every FUN_<va>.cpp under src/<bin>/_rosetta/."""
    out: set[int] = set()
    rosetta = src_root / binary / "_rosetta"
    if not rosetta.is_dir():
        return out
    for cpp in rosetta.glob("FUN_*.cpp"):
        m = _RE_FUN_NAME.match(cpp.name)
        if m:
            out.add(va_to_rva(int(m.group(1), 16), image_base))
    return out


def live_claim_rvas(
    binary: str, *, ledger_dir: Path
) -> set[int]:
    """RVAs under a claim in the ledger directory for `binary`.

    Treats every ledger file present as a live claim — sweep.yml deletes
    expired non-pinned claims at the source, so a file's mere presence is a
    conservative "do not hand out" signal. (We exclude on presence, not on
    a wall-clock lease check, because the agent should defer to whoever is
    mid-flight rather than race an about-to-expire lease.)
    """
    out: set[int] = set()
    bindir = ledger_dir / binary
    if not bindir.is_dir():
        return out
    for f in bindir.glob("*.json"):
        m = _RE_CLAIM_NAME.match(f.name)
        if m:
            out.add(int(m.group(1)))
    return out


def open_pr_rvas_from_paths(
    binary: str, paths: list[str], *, image_base: int = IMAGE_BASE
) -> set[int]:
    """RVAs whose _rosetta/FUN_<va>.cpp appears in a list of changed paths.

    `paths` is the union of files added/changed across open upstream PRs
    (as returned by `gh pr list ... --json files`). Filtering to the target
    binary keeps cross-binary PRs from over-excluding.
    """
    out: set[int] = set()
    for p in paths:
        m = _RE_ROSETTA_PATH.search(p)
        if m and m.group("bin") == binary:
            out.add(va_to_rva(int(m.group("va"), 16), image_base))
    return out


# ---------------------------------------------------------------------------
# pure eligibility (inject the three exclusion sets) — unit-testable
# ---------------------------------------------------------------------------


def eligible_functions(
    pool: list[Function],
    *,
    queue: WorkQueue,
    max_size: int,
    solved: set[int],
    claimed: set[int],
    open_pr: set[int],
) -> list[Function]:
    """Subtract the three GitHub exclusion sets from the eligible pool.

    Reuses work_queue.filter_eligible for the size / type / status /
    naked-stub / diff-compatible filters (identical to local mode), then
    drops any RVA that is solved, under a live claim, or in an open PR.
    Order-preserving so the caller can walk it deterministically.
    """
    base = queue.filter_eligible(pool, max_size=max_size)
    excluded = solved | claimed | open_pr
    return [fn for fn in base if fn.rva not in excluded]


# ---------------------------------------------------------------------------
# network-backed discovery (git fetch + gh pr list)
# ---------------------------------------------------------------------------


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _open_pr_changed_paths(upstream: str, base_branch: str) -> list[str]:
    """Every file path added/changed across OPEN upstream PRs targeting base.

    Uses `gh pr list --json number,files` and flattens the per-PR file
    lists. Best-effort: on any gh error returns [] (we'd rather under-
    exclude — the claim ledger is the real guard — than crash discovery).
    """
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            upstream,
            "--state",
            "open",
            "--base",
            base_branch,
            "--limit",
            "500",
            "--json",
            "files",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        log.warning("gh pr list failed; skipping open-PR exclusion: %s", r.stderr.strip())
        return []
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []
    paths: list[str] = []
    for pr in prs:
        for f in pr.get("files", []) or []:
            path = f.get("path")
            if path:
                paths.append(path)
    return paths


def discover_free_set(
    *,
    clone_path: Path,
    upstream: str,
    binary: str,
    base_branch: str,
    queue: WorkQueue,
    yaml_path: Path,
    max_size: int,
    image_base: int = IMAGE_BASE,
) -> list[Function]:
    """Gather the three exclusion sets from GitHub and return eligible fns.

    Steps:
      1. `git fetch upstream <base> + claims` so the solved tree + ledger
         are current locally.
      2. Materialise the solved set from the upstream base tree's
         src/<bin>/_rosetta listing (via `git ls-tree`).
      3. Materialise the live-claim set from the upstream `claims` tree.
      4. Materialise the open-PR set from `gh pr list --json files`.
      5. Subtract all three from the YAML pool (eligible_functions).
    """
    _git(clone_path, "fetch", "upstream", base_branch)
    _git(clone_path, "fetch", "upstream", "claims")

    solved = solved_rvas_from_ref(
        clone_path,
        upstream_ref=f"upstream/{base_branch}",
        binary=binary,
        image_base=image_base,
    )
    claimed = _claims_from_tree(clone_path, binary=binary)
    open_pr_paths = _open_pr_changed_paths(upstream, base_branch)
    open_pr = open_pr_rvas_from_paths(binary, open_pr_paths, image_base=image_base)

    pool = queue.load_pool(yaml_path)
    out = eligible_functions(
        pool,
        queue=queue,
        max_size=max_size,
        solved=solved,
        claimed=claimed,
        open_pr=open_pr,
    )
    log.info(
        "free set for %s: pool=%d solved=%d claimed=%d open_pr=%d -> eligible=%d",
        binary,
        len(pool),
        len(solved),
        len(claimed),
        len(open_pr),
        len(out),
    )
    return out


def solved_rvas_from_ref(
    clone_path: Path,
    *,
    upstream_ref: str,
    binary: str,
    image_base: int = IMAGE_BASE,
) -> set[int]:
    """Solved RVAs from `git ls-tree <ref> src/<bin>/_rosetta/`.

    REF-based (reads the committed tree at ``upstream_ref``), so unlike
    :func:`solved_rvas` (which scans the working directory) it is NOT
    polluted by an in-flight, just-committed _rosetta file on the current
    branch. The distributed orchestrator uses this to compute the base
    solved set without self-colliding on the VA it is about to PR.
    """
    r = _git(
        clone_path,
        "ls-tree",
        "-r",
        "--name-only",
        upstream_ref,
        f"src/{binary}/_rosetta/",
    )
    if r.returncode != 0:
        return set()
    out: set[int] = set()
    for line in r.stdout.splitlines():
        m = _RE_ROSETTA_PATH.search(line.strip())
        if m and m.group("bin") == binary:
            out.add(va_to_rva(int(m.group("va"), 16), image_base))
    return out


# Backward-compatible internal alias (was the private name).
_solved_from_tree = solved_rvas_from_ref


def _claims_from_tree(clone_path: Path, *, binary: str) -> set[int]:
    """Live-claim RVAs from `git ls-tree upstream/claims claims/<bin>/`."""
    r = _git(
        clone_path,
        "ls-tree",
        "-r",
        "--name-only",
        "upstream/claims",
        f"claims/{binary}/",
    )
    if r.returncode != 0:
        return set()
    out: set[int] = set()
    for line in r.stdout.splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        m = _RE_CLAIM_NAME.match(name)
        if m:
            out.add(int(m.group(1)))
    return out
