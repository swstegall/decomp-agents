"""Single worker process. Spawned by the orchestrator as a subprocess.

Each worker:
  1. attaches its own git worktree (pre-provisioned by the orchestrator)
  2. picks one function at a time from the SQLite claim queue
  3. spins up a Claude Agent SDK session pointed at the worktree
  4. drives the matching-workflow loop (claim → asm → cpp → make .obj → diff → iterate)
  5. on success: commits, switches branch, releases the claim
  6. on bail: releases the claim with outcome='blocked' and moves on

CLI:
  python -m decomp_agents.worker --agent-id 0 --session-id 42

stdout is line-buffered JSON events the orchestrator tails.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .hooks import make_posttool_hook, make_pretool_hook
from .prompt_context import PromptContext, build_context, classify_tier
from .prompts import load_worker_system_prompt
from .work_queue import ClaimRecord, Function, WorkQueue
from .worktree import current_sha, is_clean, stage_and_commit

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema" / "coordination.sql"


def _emit(event: dict[str, Any]) -> None:
    """Print a JSON line to stdout for the orchestrator to tail."""
    sys.stdout.write(json.dumps(event, default=str) + "\n")
    sys.stdout.flush()


def _build_options(
    *,
    cwd: Path,
    system_prompt: str,
    queue: WorkQueue | None,
    worker_id: int,
    claim_id_ref: dict[str, int | None],
    transcripts_dir: Path,
    agent_id: int,
    model: str,
):
    """Construct ClaudeAgentOptions for one worker session.

    Imported lazily so the worker module doesn't crash at import-time
    if the SDK isn't installed yet (helpful in tests + early setup).

    ``queue`` may be ``None`` (distributed mode): the coordination-DB
    tool-event hooks are simply omitted then, since distributed agents
    don't share a SQLite ledger. The SDK options are otherwise identical
    so the match loop is byte-for-byte the same in both modes.
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher  # type: ignore

    hooks = {}
    if queue is not None:
        pretool = make_pretool_hook(
            queue, worker_id, claim_id_ref, transcripts_dir, agent_id
        )
        posttool = make_posttool_hook(
            queue, worker_id, claim_id_ref, pretool, transcripts_dir, agent_id
        )
        hooks = {
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[pretool])],
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[posttool])],
        }

    return ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        model=model,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="acceptEdits",
        # The SDK reads NDJSON from the inner Claude CLI; each tool
        # result is one JSON line. Asm dumps and Ghidra pseudo-C
        # routinely cross the SDK's 1 MiB default, which raises
        # "Failed to decode JSON: JSON message exceeded maximum buffer
        # size" and tears down the whole session. 16 MiB is enough for
        # every file in ffxivgame's asm/ tree.
        max_buffer_size=16 * 1024 * 1024,
        hooks=hooks,
    )


def _function_brief(
    fn: Function,
    cfg: Config,
    ctx: PromptContext,
    *,
    distributed: bool = False,
    repo_root: Path | None = None,
) -> str:
    """The per-function task prompt fed to the agent for ONE function.

    ``distributed`` selects the fork/PR-topology loop variant. The two
    differ ONLY in the write-target / build / commit-scope instructions:

      - LOCAL: write ``src/<bin>/<module>/<symbol>.cpp``, build with
        ``make src/.../<symbol>.obj``, update the row in
        ``config/<bin>.yaml``, commit BOTH. (matches AGENTS.md's
        local-agent contract.)
      - DISTRIBUTED: write the single file ``src/<bin>/_rosetta/FUN_<va>.cpp``,
        build with ``make rosetta BINARY=<bin>.exe`` (the only path that
        produces the ``build/obj/_rosetta/<func>.obj`` that ``make diff``
        reads), do NOT touch ``config/<bin>.yaml`` (the upstream claims
        branch + reconcile.yml own status), and ``git add`` only that one
        file. This is what the distributed PR-gate
        (:func:`pr_gate.run_pr_gate`) enforces, so a brief-following agent
        produces a branch that clears the gate.

    The shared asm/sibling-reading steps (1, 2) and the diff-verdict /
    canonical-fixes loop (5–8) are identical in both modes.
    """
    # The agent's actual write root. In distributed mode the orchestrator
    # runs the SDK with cwd=the fork clone (a STANDALONE clone, NOT a
    # worktree of cfg.repo), so the brief must point there — never at
    # cfg.repo, which in distributed mode is the MAIN checkout (DECOMP_REPO,
    # also the read-only artifacts source). Pointing the agent at cfg.repo is
    # what let it write stray _rosetta/*.cpp files into the main checkout. In
    # local mode repo_root is None and we fall back to cfg.repo so the prompt
    # is byte-for-byte unchanged.
    root = repo_root or cfg.repo
    max_iters = cfg.max_iterations_per_function

    head = f"""You are matching ONE function from {fn.binary}.exe.

## Target

  binary  : {fn.binary}
  rva     : {fn.rva_hex}
  end     : 0x{fn.end:08x}
  size    : 0x{fn.size:x} ({fn.size} bytes)
  module  : {fn.module}
  symbol  : {fn.symbol}
  section : {fn.section}

"""

    body = ctx.to_markdown(max_iters=max_iters, fn=fn)

    tail = (
        _distributed_loop(fn, max_iters)
        if distributed
        else _local_loop(fn, max_iters)
    )

    if distributed:
        workspace = f"""
## Workspace context (read-only)

  - `{root}/AGENTS.md` — the matching-workflow contract + canonical fixes table

Stay in THIS clone at `{root}` and write ONLY inside it. It is a
STANDALONE clone of the fork — it does NOT share a `.git` with any other
checkout. Never write to, `cd` into, or reference any path outside
`{root}`. The only file you create is the single
`src/{fn.binary}/_rosetta/{fn.symbol}.cpp` described in your loop below,
under THIS clone.

You CANNOT open Ghidra (`*.gpr` files are a GUI artefact, not readable
text). All your decompilation hints come from the asm dump and the
optional headless pseudo-C above.
"""
    else:
        workspace = f"""
## Workspace context (read-only)

  - `{cfg.repo.parent}/CLAUDE.md` — workspace overview + every dump's
    location and grep hints

Stay in this worktree. The repo path is `{cfg.repo}`; your worktree
is a checkout sharing the same .git. Never `cd` outside of it.

You CANNOT open Ghidra (`*.gpr` files are a GUI artefact, not readable
text). All your decompilation hints come from the asm dump and the
optional headless pseudo-C above.
"""

    return head + body + tail + workspace


def _local_loop(fn: Function, max_iters: int) -> str:
    """The LOCAL-mode loop tail: module/symbol.cpp + YAML row + commit both."""
    return f"""

## Your loop

  1. Read the asm. Identify calling convention (`__cdecl` / `__stdcall` /
     `__thiscall` / `__fastcall`), stack frame size, return type, branch
     shape. Don't proceed until you can name them.
  2. If pseudo-C is available, read it as a hint. If not, look at the
     sibling matches above and find the closest structural analog.
  3. Write `src/{fn.binary}/<module>/<symbol>.cpp` following the local
     idiom. Copy the AGPL header from a sibling — don't reinvent it.
  4. Compile: `make src/{fn.binary}/<module>/<symbol>.obj`
  5. Diff:    `make diff FUNC={fn.symbol}`
  6. The diff prints `✅ GREEN` (exit 0) for byte-identical, `PARTIAL`
     (exit 1) for same-length-but-wrong-bytes, or `MISMATCH` (exit 2)
     for wholesale wrong. **Only GREEN is a match.**
  7. If GREEN: continue to step 9.
  8. If PARTIAL/MISMATCH: pick the most-likely cause from the canonical
     fixes table (in `meteor-decomp/AGENTS.md` and
     `docs/matching-workflow.md` §7) and try ONE adjustment. Recompile
     + re-diff. Repeat up to {max_iters} times.
  9. Update `config/{fn.binary}.yaml` row for this RVA: set
     `status: matched` and `owner: null`. Touch nothing else in the
     YAML (no other rows, no whitespace normalisation).
 10. `git add` only the new src/ file and the YAML row (and any type
     note you wrote). Commit with:
       decomp: match {fn.symbol} @{fn.rva_hex}
       <one-line note on which fix worked>
 11. STOP. Do not pick another function — the orchestrator will hand
     you the next one.

## On bail

If you can't reach GREEN within {max_iters} iterations, do NOT commit
a half-match. Revert any in-progress src/ edits with
`git checkout -- src/{fn.binary}/`, leave the YAML untouched, write
the post-mortem file described above, then emit a final text message
containing the literal string `BLOCKED:` followed by a one-sentence
reason. Then stop.
"""


def _distributed_loop(fn: Function, max_iters: int) -> str:
    """The DISTRIBUTED-mode loop tail: a single _rosetta/FUN_<va>.cpp.

    The agent works from a FORK clone tracking the upstream base branch and
    will open a PR upstream, so the output must satisfy the toolchain-free
    PR-gate: EXACTLY ONE added file `src/<bin>/_rosetta/FUN_<va>.cpp`, no
    YAML edit, verbatim AGPL header. The upstream `claims` branch +
    reconcile.yml own the YAML status field — the PR must not carry it.
    """
    rosetta = f"src/{fn.binary}/_rosetta/{fn.symbol}.cpp"
    return f"""

## Your loop (distributed / fork-PR mode)

You are preparing a PULL REQUEST to upstream. The PR must add EXACTLY
ONE file and nothing else. Do NOT edit `config/{fn.binary}.yaml`,
`tools/`, `Makefile`, `PLAN.md`, `README*`, `docs/`, `include/`, or any
other coordination surface — the upstream claims branch and reconcile.yml
own the YAML status; a PR that touches them will be rejected by the gate.

  1. Read the asm. Identify calling convention (`__cdecl` / `__stdcall` /
     `__thiscall` / `__fastcall`), stack frame size, return type, branch
     shape. Don't proceed until you can name them.
  2. If pseudo-C is available, read it as a hint. If not, look at the
     sibling matches above and find the closest structural analog.
  3. Write the SINGLE new file `{rosetta}` following the local idiom.
     Copy the AGPL header verbatim from a sibling `_rosetta/*.cpp` — don't
     reinvent it. (If the sibling you copy from opens with a `// [STAMPED]`
     banner, you do NOT need that banner — start your file at the
     `// meteor-decomp …` AGPL line.) Do NOT create a `<module>/`
     subdirectory; the only legal path is the `_rosetta/FUN_<va>.cpp` one.
  4. Compile + diff in one step: `make rosetta BINARY={fn.binary}.exe`.
     This compiles every staged `src/{fn.binary}/_rosetta/*.cpp` into
     `build/obj/_rosetta/{fn.binary}/<func>.obj` and runs `compare.py` —
     the same GREEN grader. (Equivalently you can run
     `make diff FUNC={fn.symbol}` once the obj exists.)
  5. The diff prints `✅ GREEN` (exit 0) for byte-identical, `PARTIAL`
     (exit 1) for same-length-but-wrong-bytes, or `MISMATCH` (exit 2)
     for wholesale wrong. **Only GREEN is a match.**
  6. If GREEN: continue to step 8.
  7. If PARTIAL/MISMATCH: pick the most-likely cause from the canonical
     fixes table (in `AGENTS.md` and
     `docs/matching-workflow.md` §7) and try ONE adjustment. Recompile
     + re-diff. Repeat up to {max_iters} times.
  8. `git add` ONLY `{rosetta}` (nothing else — no YAML, no notes added to
     shared files). Commit with:
       decomp: match {fn.symbol} @{fn.rva_hex}
       <one-line note on which fix worked>
  9. STOP. Do not pick another function — the orchestrator will hand
     you the next one (and run the PR-gate + open the PR).

## On bail

If you can't reach GREEN within {max_iters} iterations, do NOT commit
a half-match. Revert any in-progress src/ edits with
`git checkout -- src/{fn.binary}/` (and `git reset` any staged file),
then emit a final text message containing the literal string `BLOCKED:`
followed by a one-sentence reason. Then stop.
"""


async def run_match_loop(
    *,
    cfg: Config,
    fn: Function,
    cwd: Path,
    model: str,
    ctx: PromptContext,
    queue: WorkQueue | None = None,
    worker_id: int = -1,
    agent_id: int = -1,
    claim_id_ref: dict[str, int | None] | None = None,
    transcripts_dir: Path | None = None,
    on_iteration=None,
    distributed: bool = False,
) -> tuple[str, int]:
    """Drive the Claude Agent SDK match loop for ONE function.

    This is the shared loop body used by BOTH local mode
    (:func:`_run_function`) and distributed mode
    (distributed_orchestrator). It builds the per-function brief, runs the
    SDK ``query`` stream, counts iterations, detects the ``BLOCKED:`` bail,
    and grades the outcome via :func:`_grade_outcome` (the single GREEN
    grader — never duplicated).

    ``queue`` is optional: when ``None`` (distributed mode) the
    coordination-DB tool-event hooks + iteration writes are skipped, but
    the loop is otherwise identical. ``on_iteration(i)`` is an optional
    callback fired after each counted tool-result so a caller can persist
    the iteration count its own way.

    ``distributed`` selects the fork/PR-topology brief variant: the agent
    writes a single ``src/<bin>/_rosetta/FUN_<va>.cpp`` (the path the
    PR-gate + Makefile `rosetta` target expect) and does NOT touch the
    YAML. Defaults to False so local mode is byte-for-byte unchanged.

    Returns ``(outcome, iterations)`` where ``outcome`` is "matched" iff
    ``make diff`` came back GREEN, else "blocked".
    """
    from claude_agent_sdk import query  # type: ignore

    system_prompt = load_worker_system_prompt()
    options = _build_options(
        cwd=cwd,
        system_prompt=system_prompt,
        queue=queue,
        worker_id=worker_id,
        claim_id_ref=claim_id_ref if claim_id_ref is not None else {"current": None},
        transcripts_dir=transcripts_dir or cfg.transcripts_dir,
        agent_id=agent_id,
        model=model,
    )
    user_prompt = _function_brief(
        fn,
        cfg,
        ctx,
        distributed=distributed,
        # cwd IS the fork clone in distributed mode; keep local mode None so
        # the brief falls back to cfg.repo and renders byte-for-byte as before.
        repo_root=cwd if distributed else None,
    ).replace("{max_iters}", str(cfg.max_iterations_per_function))

    iterations = 0
    bailed = False
    async for message in query(prompt=user_prompt, options=options):
        msg_type = getattr(message, "type", None) or message.__class__.__name__
        # The SDK doesn't expose a per-turn counter directly; we
        # approximate by counting tool-result messages.
        if msg_type.endswith("ToolResult") or msg_type.endswith("ToolResultMessage"):
            iterations += 1
            if on_iteration is not None:
                on_iteration(iterations)
        # Bail if the agent printed BLOCKED.
        text = getattr(message, "text", "") or ""
        if isinstance(text, str) and "BLOCKED:" in text.upper():
            bailed = True

    outcome = _grade_outcome(cwd, fn, bailed=bailed)
    return outcome, iterations


async def _run_function(
    *,
    cfg: Config,
    queue: WorkQueue,
    claim: ClaimRecord,
    worker_id: int,
    agent_id: int,
    cwd: Path,
    claim_id_ref: dict[str, int | None],
    transcripts_dir: Path,
) -> tuple[str, int]:
    """Drive the SDK loop for one function. Returns (outcome, iterations)."""
    # Build context once and use it for both the brief and the model
    # tier choice — building it touches a handful of YAML/JSON/disk
    # reads we don't want to repeat.
    ctx = build_context(claim.function, repo=cfg.repo)
    tier = classify_tier(claim.function, ctx)
    model = cfg.model_for_tier(tier)
    _emit(
        {
            "kind": "tier",
            "agent_id": agent_id,
            "claim_id": claim.id,
            "tier": tier,
            "model": model,
        }
    )

    def _persist(i: int) -> None:
        queue.update_iterations(claim.id, i)
        _emit(
            {
                "kind": "sdk_event",
                "agent_id": agent_id,
                "claim_id": claim.id,
                "msg_type": "ToolResult",
            }
        )

    return await run_match_loop(
        cfg=cfg,
        fn=claim.function,
        cwd=cwd,
        model=model,
        ctx=ctx,
        queue=queue,
        worker_id=worker_id,
        agent_id=agent_id,
        claim_id_ref=claim_id_ref,
        transcripts_dir=transcripts_dir,
        on_iteration=_persist,
    )


def _grade_outcome(cwd: Path, fn: Function, *, bailed: bool) -> str:
    """Inspect the worktree to decide what actually happened.

    The agent's textual "BLOCKED:" is a hint; ground truth is the diff.

    tools/compare.py exit codes (canonical per its module docstring):
      0  GREEN    — byte-identical match
      1  PARTIAL  — same length but some bytes differ
      2  MISMATCH — different lengths / wholesale wrong
      3  USAGE / SETUP error
    """
    if bailed:
        return "blocked"
    diff_cmd = subprocess.run(
        ["make", "-C", str(cwd), "diff", f"FUNC={fn.symbol}"],
        capture_output=True,
        text=True,
    )
    if diff_cmd.returncode == 0:
        return "matched"
    return "blocked"


async def _heartbeat_loop(queue: WorkQueue, worker_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        queue.heartbeat(worker_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_config()
    queue = WorkQueue(cfg.coordination_db, SCHEMA_PATH)

    worker_id = args.worker_id
    agent_id = args.agent_id
    session_id = args.session_id
    cwd = Path(args.worktree).resolve()
    branch_prefix = args.branch_prefix

    pool = queue.load_pool(cfg.yaml_path)
    candidates = queue.filter_eligible(pool, max_size=cfg.max_function_size)
    _emit(
        {
            "kind": "worker_start",
            "agent_id": agent_id,
            "candidates": len(candidates),
            "pool_total": len(pool),
        }
    )

    stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat_loop(queue, worker_id, stop))

    attempts = 0
    claim_id_ref: dict[str, int | None] = {"current": None}
    try:
        while attempts < cfg.max_attempts_per_worker and not stop.is_set():
            claim = queue.claim_next(
                session_id=session_id,
                worker_id=worker_id,
                candidates=candidates,
                branch_prefix=branch_prefix,
            )
            if claim is None:
                _emit({"kind": "no_work", "agent_id": agent_id})
                break

            claim_id_ref["current"] = claim.id
            queue.set_worker_status(worker_id, "claiming")
            _emit(
                {
                    "kind": "claim",
                    "agent_id": agent_id,
                    "claim_id": claim.id,
                    "rva": claim.function.rva_hex,
                    "symbol": claim.function.symbol,
                }
            )

            # Switch the worker's worktree to this claim's per-function branch.
            try:
                subprocess.run(
                    ["git", "-C", str(cwd), "checkout", "-B", claim.branch],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                _emit(
                    {
                        "kind": "error",
                        "agent_id": agent_id,
                        "phase": "branch_switch",
                        "stderr": exc.stderr,
                    }
                )
                queue.release(claim.id, outcome="error", iterations=0)
                claim_id_ref["current"] = None
                attempts += 1
                continue

            queue.set_worker_status(worker_id, "working")

            try:
                outcome, iterations = await _run_function(
                    cfg=cfg,
                    queue=queue,
                    claim=claim,
                    worker_id=worker_id,
                    agent_id=agent_id,
                    cwd=cwd,
                    claim_id_ref=claim_id_ref,
                    transcripts_dir=cfg.transcripts_dir,
                )
            except Exception as exc:  # noqa: BLE001
                _emit(
                    {
                        "kind": "error",
                        "agent_id": agent_id,
                        "phase": "sdk",
                        "error": repr(exc),
                    }
                )
                queue.release(claim.id, outcome="error", iterations=0)
                claim_id_ref["current"] = None
                attempts += 1
                continue

            # If the agent left the tree dirty without committing, fold
            # the changes into a commit so the merge orchestrator can
            # pick them up. (acceptEdits mode means agents shouldn't
            # need confirmation to commit, but we belt-and-brace it.)
            sha = ""
            if not is_clean(cwd):
                if outcome == "matched":
                    sha = stage_and_commit(
                        cwd,
                        f"decomp: matched {claim.function.symbol} @{claim.function.rva_hex}\n\n"
                        "(auto-committed by orchestrator)",
                    )
                else:
                    # Don't commit half-matches. Discard the dirty tree
                    # so the next claim on this RVA starts fresh.
                    subprocess.run(
                        ["git", "-C", str(cwd), "reset", "--hard", "HEAD"],
                        capture_output=True,
                        check=False,
                    )
                    _emit(
                        {
                            "kind": "reverted_dirty",
                            "agent_id": agent_id,
                            "claim_id": claim.id,
                            "outcome": outcome,
                        }
                    )
            elif outcome == "matched":
                sha = current_sha(cwd)

            # Push the per-function branch to origin so it's visible
            # for review / CI / collaboration. Pushing is per-worker
            # (not deferred to the merge orchestrator) because the
            # branch is unique to this claim and pushes don't conflict.
            push_branches = cfg.autopush in ("branches", "branches+master")
            if push_branches and outcome == "matched" and sha:
                push = subprocess.run(
                    ["git", "-C", str(cwd), "push", "origin", claim.branch],
                    capture_output=True,
                    text=True,
                )
                if push.returncode == 0:
                    _emit(
                        {
                            "kind": "pushed_branch",
                            "agent_id": agent_id,
                            "branch": claim.branch,
                        }
                    )
                else:
                    _emit(
                        {
                            "kind": "push_failed",
                            "agent_id": agent_id,
                            "branch": claim.branch,
                            "stderr": (push.stderr or push.stdout).strip(),
                        }
                    )

            queue.release(
                claim.id,
                outcome=outcome,
                iterations=iterations,
                merge_status="pending" if outcome == "matched" and sha else None,
            )
            _emit(
                {
                    "kind": "release",
                    "agent_id": agent_id,
                    "claim_id": claim.id,
                    "outcome": outcome,
                    "iterations": iterations,
                    "sha": sha,
                }
            )
            claim_id_ref["current"] = None
            attempts += 1

        _emit({"kind": "worker_stop", "agent_id": agent_id, "attempts": attempts})
        return 0
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await hb_task
        queue.set_worker_status(worker_id, "stopped")


def main() -> None:
    ap = argparse.ArgumentParser(description="decomp-agents worker")
    ap.add_argument("--agent-id", type=int, required=True)
    ap.add_argument("--worker-id", type=int, required=True)
    ap.add_argument("--session-id", type=int, required=True)
    ap.add_argument("--worktree", type=str, required=True)
    ap.add_argument("--branch-prefix", type=str, required=True)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [agent-{args.agent_id}] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Graceful SIGTERM (orchestrator kill) → exit clean.
    def _terminate(signum, frame):  # noqa: ARG001
        log.info("received signal %d, exiting", signum)
        sys.exit(143)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    try:
        sys.exit(asyncio.run(_amain(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
