"""Pre-PR gate for distributed mode (issue #11, D5).

Before a distributed agent runs `gh pr create`, the branch must clear a
toolchain-free gate so we never ship a half-match or a scope violation
upstream. Every check is small, pure-ish, and independently testable.

The four checks (all must pass):

  (a) GREEN          — `make diff FUNC=FUN_<va>` returns rc=0 (byte-identical).
                       Reuses the SAME grader as the worker loop
                       (worker._grade_outcome) so local and distributed
                       agree on what "matched" means.
  (b) ONE-FILE SCOPE — the branch's diff vs. the base adds EXACTLY ONE new
                       file, `src/<bin>/_rosetta/FUN_<va>.cpp`, and edits
                       nothing else. Per meteor-decomp/AGENTS.md "Don't
                       touch what isn't yours": no tools/, Makefile, PLAN.md,
                       README, docs/, include/, config/, or any other file.
                       (Distributed PRs add the _rosetta file directly — the
                       claims branch + reconcile own the YAML, so the agent
                       does NOT touch config/<bin>.yaml.)
  (c) AGPL HEADER    — the new .cpp opens with the project's verbatim AGPL
                       header (copied from a sibling _rosetta/*.cpp).
  (d) VA PARSE + NOT-SOLVED — the filename VA parses, and the VA is NOT
                       already in the committed solved set on the base tree
                       (we're not re-solving something already upstream).

`run_pr_gate` runs all four and returns a :class:`GateReport`. Each check
returns a :class:`GateCheck` (ok + reason) so a caller can log precisely
which gate failed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_BASE = 0x400000

# The single legal added path. <bin> is a binary stem, <va> 8-hex VA.
_RE_ROSETTA_PATH = re.compile(
    r"^src/(?P<bin>ffxiv[a-z]+)/_rosetta/FUN_(?P<va>[0-9a-fA-F]+)\.cpp$"
)

# The verbatim AGPL header lines a _rosetta/*.cpp must open with. Matched
# as an ordered prefix so trailing per-function comments don't matter. The
# wording mirrors the sibling _rosetta files (see
# src/<bin>/_rosetta/FUN_*.cpp) and meteor-decomp's own license boilerplate.
AGPL_HEADER_LINES = (
    "// meteor-decomp — clean-room decompilation of FINAL FANTASY XIV 1.x client binaries",
    "// Copyright (C) 2026  Samuel Stegall",
    "//",
    "// This program is free software: you can redistribute it and/or modify",
    "// it under the terms of the GNU Affero General Public License as published",
    "// by the Free Software Foundation, either version 3 of the License, or",
    "// (at your option) any later version.",
    "//",
    "// SPDX-License-Identifier: AGPL-3.0-or-later",
)

# The single SPDX trailer line that MUST appear regardless of header
# wording drift — the load-bearing license tag.
SPDX_LINE = "// SPDX-License-Identifier: AGPL-3.0-or-later"


@dataclass(frozen=True)
class GateCheck:
    name: str
    ok: bool
    reason: str


@dataclass
class GateReport:
    checks: list[GateCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        parts = [f"{c.name}={'ok' if c.ok else 'FAIL'}" for c in self.checks]
        return f"PR-gate {verdict}: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# (a) GREEN — reuse the worker's grader
# ---------------------------------------------------------------------------


def check_green(clone_path: Path, symbol: str) -> GateCheck:
    """`make diff FUNC=<symbol>` must be GREEN (rc=0).

    Delegates to worker._grade_outcome so the GREEN definition is shared,
    not duplicated. (Imported lazily to avoid importing the SDK-touching
    worker module at gate-import time.)
    """
    from .work_queue import Function
    from .worker import _grade_outcome

    # _grade_outcome only needs fn.symbol; the rest is filler.
    fn = Function(
        binary="",
        rva=0,
        end=0,
        size=0,
        module="",
        symbol=symbol,
        type="matching",
        status="unmatched",
        section=".text",
    )
    outcome = _grade_outcome(clone_path, fn, bailed=False)
    if outcome == "matched":
        return GateCheck("green", True, f"make diff FUNC={symbol} GREEN")
    return GateCheck("green", False, f"make diff FUNC={symbol} not GREEN (outcome={outcome})")


# ---------------------------------------------------------------------------
# (b) ONE-FILE SCOPE — exactly one new _rosetta/FUN_<va>.cpp
# ---------------------------------------------------------------------------


def changed_paths(clone_path: Path, base_ref: str) -> list[tuple[str, str]]:
    """`git diff --name-status <base>...HEAD` -> [(status, path), ...].

    Three-dot range: changes on HEAD since it diverged from base, which is
    exactly the set a PR would carry.
    """
    r = subprocess.run(
        ["git", "-C", str(clone_path), "diff", "--name-status", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0].strip(), parts[-1].strip()))
    return out


def check_one_file_scope(
    paths: list[tuple[str, str]], *, binary: str | None = None
) -> GateCheck:
    """Exactly one path, status 'A' (added), matching the _rosetta pattern.

    Pure: takes the (status, path) list so it's testable without git. Any
    extra path, any non-add status, or a wrong-shaped path fails. When
    `binary` is given, the added file's binary must match it.
    """
    if not paths:
        return GateCheck("one_file_scope", False, "diff is empty — nothing to PR")
    if len(paths) != 1:
        names = ", ".join(p for _, p in paths)
        return GateCheck(
            "one_file_scope",
            False,
            f"touches {len(paths)} files (must be exactly 1): {names}",
        )
    status, path = paths[0]
    if status != "A":
        return GateCheck(
            "one_file_scope",
            False,
            f"the single change must be an ADD (got status {status!r} for {path})",
        )
    m = _RE_ROSETTA_PATH.match(path)
    if not m:
        return GateCheck(
            "one_file_scope",
            False,
            f"added path is not src/<bin>/_rosetta/FUN_<va>.cpp: {path}",
        )
    if binary is not None and m.group("bin") != binary:
        return GateCheck(
            "one_file_scope",
            False,
            f"added file is for binary {m.group('bin')!r}, expected {binary!r}",
        )
    return GateCheck("one_file_scope", True, f"single new _rosetta file: {path}")


# ---------------------------------------------------------------------------
# (c) AGPL HEADER — verbatim, present in the new .cpp
# ---------------------------------------------------------------------------


def check_agpl_header(content: str) -> GateCheck:
    """The new .cpp must carry the verbatim AGPL header in its opening region.

    The full AGPL_HEADER_LINES block must appear CONTIGUOUSLY and IN ORDER,
    AND the SPDX tag must be present. Pure: takes the file content string
    so it's testable on fixtures.

    The block is allowed to be preceded ONLY by benign leading comment
    lines (``//`` line comments and blank lines) — most real sibling
    _rosetta/*.cpp files were generated by ``tools/stamp_clusters.py`` and
    open with a 3-line ``// [STAMPED] …`` provenance banner before the
    AGPL boilerplate. An agent that copies a stamped sibling's header
    verbatim must still pass. We fail-closed: if any NON-comment, non-blank
    line (i.e. real code or a ``/* … */`` block) appears before the AGPL
    block, the header is not at the top and the check fails — so a license
    block buried below code can never sneak through.
    """
    if SPDX_LINE not in content:
        return GateCheck(
            "agpl_header", False, f"missing SPDX tag {SPDX_LINE!r}"
        )
    lines = [ln.rstrip() for ln in content.splitlines()]
    first = AGPL_HEADER_LINES[0]

    # Find every candidate start (a line equal to the first AGPL line) that
    # is preceded only by benign leading comment / blank lines, then verify
    # the full block follows contiguously in order.
    for start, line in enumerate(lines):
        if line != first:
            continue
        if not _only_leading_comments(lines[:start]):
            # Code (or a non-`//` line) precedes this AGPL line → not a
            # top-of-file header. Keep scanning in case a later, properly
            # positioned block exists, but this candidate is rejected.
            continue
        if start + len(AGPL_HEADER_LINES) > len(lines):
            continue
        window = lines[start : start + len(AGPL_HEADER_LINES)]
        if window == list(AGPL_HEADER_LINES):
            return GateCheck("agpl_header", True, "verbatim AGPL header present")

    return GateCheck(
        "agpl_header",
        False,
        "verbatim AGPL header block not found in the file's opening "
        "comment region (only a `// [STAMPED]`-style comment banner may "
        "precede it)",
    )


def _only_leading_comments(lines: list[str]) -> bool:
    """True iff every line is blank or a `//` line comment.

    Used to bound how much may precede the AGPL block: a `// [STAMPED]`
    provenance banner (or blank lines) is fine; any real code or a `/* */`
    opener is not — that would mean the license block is buried, not at the
    top, which must fail-closed.
    """
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("//"):
            return False
    return True


# ---------------------------------------------------------------------------
# (d) VA PARSE + NOT-SOLVED
# ---------------------------------------------------------------------------


def parse_va_from_path(path: str) -> int | None:
    """Parse the VA (int) out of a _rosetta/FUN_<va>.cpp path, or None."""
    m = _RE_ROSETTA_PATH.match(path)
    if not m:
        return None
    try:
        return int(m.group("va"), 16)
    except ValueError:
        return None


def check_va_parses_and_unsolved(
    path: str,
    *,
    base_solved_vas: set[int],
) -> GateCheck:
    """The filename VA parses AND is not already in the committed solved set.

    `base_solved_vas` is the set of VAs already present in the base tree's
    _rosetta listing (a VA already there means the PR is redundant). Pure:
    inject the solved set so it's testable.
    """
    va = parse_va_from_path(path)
    if va is None:
        return GateCheck(
            "va_parse", False, f"could not parse a VA from {path!r}"
        )
    if va in base_solved_vas:
        return GateCheck(
            "va_parse",
            False,
            f"VA 0x{va:08x} is already solved on the base tree (redundant PR)",
        )
    return GateCheck("va_parse", True, f"VA 0x{va:08x} parses and is unsolved")


# ---------------------------------------------------------------------------
# the full gate
# ---------------------------------------------------------------------------


def run_pr_gate(
    *,
    clone_path: Path,
    base_ref: str,
    symbol: str,
    binary: str,
    base_solved_vas: set[int],
) -> GateReport:
    """Run all four checks against a prepared per-function branch.

    Reads the added file's content off HEAD to gate its header. Returns a
    GateReport; the caller proceeds to `gh pr create` only when
    ``report.ok``.
    """
    report = GateReport()

    # (a) GREEN
    report.checks.append(check_green(clone_path, symbol))

    # (b) one-file scope
    paths = changed_paths(clone_path, base_ref)
    scope = check_one_file_scope(paths, binary=binary)
    report.checks.append(scope)

    # The header + VA checks need the single added path; if scope already
    # failed (zero or many files), short-circuit them with that reason so
    # the report is unambiguous rather than crashing on paths[0].
    if not scope.ok:
        report.checks.append(GateCheck("agpl_header", False, "skipped: scope failed"))
        report.checks.append(GateCheck("va_parse", False, "skipped: scope failed"))
        return report

    _, added_path = paths[0]

    # (c) AGPL header — read the file off HEAD (not the working tree) so we
    #     gate exactly what the PR will carry.
    content = _read_blob(clone_path, "HEAD", added_path)
    report.checks.append(check_agpl_header(content or ""))

    # (d) VA parse + not-already-solved
    report.checks.append(
        check_va_parses_and_unsolved(added_path, base_solved_vas=base_solved_vas)
    )
    return report


def _read_blob(clone_path: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(clone_path), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout if r.returncode == 0 else None
