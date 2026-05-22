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
from .prompt_context import build_context
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
    queue: WorkQueue,
    worker_id: int,
    claim_id_ref: dict[str, int | None],
    transcripts_dir: Path,
    agent_id: int,
    model: str,
):
    """Construct ClaudeAgentOptions for one worker session.

    Imported lazily so the worker module doesn't crash at import-time
    if the SDK isn't installed yet (helpful in tests + early setup).
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher  # type: ignore

    pretool = make_pretool_hook(
        queue, worker_id, claim_id_ref, transcripts_dir, agent_id
    )
    posttool = make_posttool_hook(
        queue, worker_id, claim_id_ref, pretool, transcripts_dir, agent_id
    )

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
        hooks={
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[pretool])],
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[posttool])],
        },
    )


def _function_brief(fn: Function, cfg: Config) -> str:
    """The per-function task prompt fed to the agent for ONE function."""
    ctx = build_context(fn, repo=cfg.repo)
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

    tail = f"""

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

## Workspace context (read-only)

  - `{cfg.repo.parent}/CLAUDE.md` — workspace overview + every dump's
    location and grep hints

Stay in this worktree. The repo path is `{cfg.repo}`; your worktree
is a checkout sharing the same .git. Never `cd` outside of it.

You CANNOT open Ghidra (`*.gpr` files are a GUI artefact, not readable
text). All your decompilation hints come from the asm dump and the
optional headless pseudo-C above.
"""

    return head + body + tail


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
    from claude_agent_sdk import query  # type: ignore

    system_prompt = load_worker_system_prompt()
    options = _build_options(
        cwd=cwd,
        system_prompt=system_prompt,
        queue=queue,
        worker_id=worker_id,
        claim_id_ref=claim_id_ref,
        transcripts_dir=transcripts_dir,
        agent_id=agent_id,
        model=cfg.worker_model,
    )
    user_prompt = _function_brief(claim.function, cfg).replace(
        "{max_iters}", str(cfg.max_iterations_per_function)
    )

    iterations = 0
    bailed = False
    async for message in query(prompt=user_prompt, options=options):
        msg_type = getattr(message, "type", None) or message.__class__.__name__
        _emit(
            {
                "kind": "sdk_event",
                "agent_id": agent_id,
                "claim_id": claim.id,
                "msg_type": msg_type,
            }
        )
        # The SDK doesn't expose a per-turn counter directly; we
        # approximate by counting tool-result messages.
        if msg_type.endswith("ToolResult") or msg_type.endswith("ToolResultMessage"):
            iterations += 1
            queue.update_iterations(claim.id, iterations)
        # Bail if the agent printed BLOCKED.
        text = getattr(message, "text", "") or ""
        if isinstance(text, str) and "BLOCKED:" in text.upper():
            bailed = True

    outcome = _grade_outcome(cwd, claim.function, bailed=bailed)
    return outcome, iterations


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
