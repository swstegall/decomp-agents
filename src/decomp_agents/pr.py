"""Upstream PR creation for distributed mode (issue #11, D5).

After the per-function branch passes the PR-gate (pr_gate.run_pr_gate), the
agent opens a cross-repo PR from its FORK to the upstream base branch:

    gh pr create
      --repo <upstream>
      --base <upstream_branch>            # e.g. develop
      --head <fork-owner>:<branch>        # cross-repo head ref
      --title 'decomp: match <Sym> @0x<rva>'
      --body  ...

Opening the PR (which adds `src/<bin>/_rosetta/FUN_<va>.cpp`) is also what
PINS the claim upstream — reconcile.yml's pin path keys off the PR's added
_rosetta file, so the lease stops expiring once the PR is open.

The body builder (:func:`build_pr_body`) and the title builder
(:func:`build_pr_title`) are PURE so they're testable without gh.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PRResult:
    ok: bool
    url: str
    message: str


def build_pr_title(symbol: str, rva: int) -> str:
    """`decomp: match <Sym> @0x<rva>` — matches the issue's spec verbatim."""
    return f"decomp: match {symbol} @0x{rva:08x}"


def build_pr_body(
    *,
    binary: str,
    symbol: str,
    rva: int,
    iterations: int,
    gate_summary: str,
) -> str:
    """A short, honest PR body. PURE.

    Records the binary/VA/symbol, how the agent verified GREEN, the
    one-file scope claim, and the gate summary, so a human reviewer (and
    reconcile.yml) has the context to land it. Ends with the project's
    Claude Code attribution trailer.
    """
    va = rva + 0x400000
    return (
        f"Clean-room match of `{symbol}` in `{binary}` "
        f"(VA `0x{va:08x}`, RVA `0x{rva:08x}`).\n\n"
        f"- Adds exactly one file: `src/{binary}/_rosetta/FUN_{va:08x}.cpp`.\n"
        f"- Verified byte-identical: `make diff FUNC=FUN_{va:08x}` returns "
        f"GREEN (rc=0).\n"
        f"- Reached GREEN in {iterations} iteration(s).\n"
        f"- Pre-PR gate: {gate_summary}\n\n"
        f"Opening this PR pins the upstream claim for this VA "
        f"(`claims/{binary}/{rva}.json`).\n\n"
        f"🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    )


def create_pr(
    *,
    upstream: str,
    base_branch: str,
    fork_owner: str,
    branch: str,
    title: str,
    body: str,
) -> PRResult:
    """Open the upstream PR via `gh pr create`. Never raises.

    The head ref is `<fork-owner>:<branch>` so gh resolves the cross-repo
    head correctly even though we invoke it with `--repo <upstream>`.
    """
    head = f"{fork_owner}:{branch}"
    r = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            upstream,
            "--base",
            base_branch,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode == 0:
        url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        log.info("opened PR %s -> %s:%s (%s)", head, upstream, base_branch, url)
        return PRResult(ok=True, url=url, message=url)
    msg = (r.stderr or r.stdout).strip()
    log.warning("gh pr create failed: %s", msg)
    return PRResult(ok=False, url="", message=msg)
