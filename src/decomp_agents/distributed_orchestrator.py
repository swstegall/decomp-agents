"""Distributed-mode orchestrator (issue #11, D5).

Wires the fork-based, GitHub-native flow end to end for ONE agent process
working from a FORK of meteor-decomp:

    provision fork  (fork.provision_fork)
      -> discover free set  (freeset.discover_free_set)
      -> for each eligible VA, until budget:
           claim       (github_claim.GitHubClaimBackend.claim)
           start branch (fork.start_function_branch)
           run the SAME per-function match loop  (worker.run_match_loop)
           PR-gate      (pr_gate.run_pr_gate)
           gh pr create (pr.create_pr) — pins the claim upstream
           heartbeat between long phases  (backend.heartbeat)
      -> on bail / lost / gate-fail: best-effort release (backend.release)

It REUSES worker.run_match_loop (the SDK loop + the single GREEN grader) —
nothing about matching is re-implemented here. The LOCAL orchestrator
(orchestrator.py) is untouched and remains the default; a thin mode switch
in :func:`run` (called from the CLI) routes between them.

This entry path is intentionally single-process (one agent identity = one
gh login = one claim owner). Parallelism in distributed mode is achieved by
running N independent agent processes (each its own fork / identity),
serialised by the upstream claim ledger — not by N worktrees of one repo.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .fork import (
    ForkClone,
    gh_login,
    provision_fork,
    provision_toolchain_artifacts,
    push_function_branch,
    start_function_branch,
)
from .freeset import (
    IMAGE_BASE,
    discover_free_set,
    rva_to_va,
    solved_rvas_from_ref,
)
from .github_claim import GitHubClaimBackend
from .pr import build_pr_body, build_pr_title, create_pr
from .pr_gate import run_pr_gate
from .prompt_context import build_context, classify_tier
from .work_queue import Function, WorkQueue

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema" / "coordination.sql"


def shard_functions(
    functions: list[Function], *, index: int, count: int
) -> list[Function]:
    """Return the `index`-th of `count` disjoint shards of `functions`.

    Partitions by ``rva % count`` so every function lands in exactly one shard.
    N independent distributed processes (one per shard) therefore never attempt
    the same VA — even when their free-set snapshots drift apart in time, since
    a VA's shard is a pure function of its address, not of list position.
    ``count <= 1`` is the no-shard identity (single-process mode).
    """
    if count <= 1:
        return list(functions)
    return [fn for fn in functions if fn.rva % count == index]


class DistributedAgent:
    """One fork-side agent: claim → match → gate → PR, repeated."""

    def __init__(self, cfg: Config) -> None:
        if cfg.mode != "distributed":
            raise RuntimeError(
                "DistributedAgent requires DECOMP_MODE=distributed"
            )
        self.cfg = cfg
        # A local SQLite is still handy as the per-function context/cache
        # source (build_context reuses it indirectly via the YAML pool),
        # but it is NOT the claim authority in distributed mode.
        self.queue = WorkQueue(cfg.coordination_db, SCHEMA_PATH)
        self.login: str = ""
        self.clone: ForkClone | None = None
        self.backend: GitHubClaimBackend | None = None

    # --- setup ------------------------------------------------------------

    def provision(self) -> ForkClone:
        self.login = gh_login()
        self.clone = provision_fork(
            upstream=self.cfg.upstream,
            fork=self.cfg.fork,
            target=self.cfg.fork_root,
            upstream_branch=self.cfg.upstream_branch,
            agent_login=self.login,
        )
        # Symlink the user's copyright-derived toolchain artifacts
        # (orig/<bin>.exe + config/<bin>.symbols.json) into the fresh clone
        # so `make diff` (the GREEN grader) can run. These are gitignored in
        # every clone — they're never carried by `git clone` and are never
        # committed/pushed in a PR. Degrades gracefully: a missing source
        # only breaks grading, not claiming / free-set discovery, so we log a
        # WARNING and continue rather than crashing the whole run.
        artifacts = provision_toolchain_artifacts(
            self.clone.path, self.cfg.artifacts_dir, self.cfg.binary
        )
        if not artifacts.ok:
            log.warning(
                "toolchain artifacts NOT fully provisioned — PR-gate GREEN "
                "checks will fail until DECOMP_ARTIFACTS_DIR is set: %s",
                artifacts.warning,
            )
        self.backend = GitHubClaimBackend(
            clone_path=self.clone.path,
            upstream=self.cfg.upstream,
            claim_issue=self.cfg.claim_issue,
            login=self.login,
            binary=self.cfg.binary,
            poll_timeout_s=self.cfg.claim_poll_timeout_s,
            poll_interval_s=self.cfg.claim_poll_interval_s,
        )
        log.info(
            "distributed agent ready: login=%s upstream=%s fork_owner=%s clone=%s",
            self.login,
            self.cfg.upstream,
            self.clone.fork_owner,
            self.clone.path,
        )
        return self.clone

    def free_set(self) -> list[Function]:
        assert self.clone is not None
        # The YAML pool for distributed mode comes from the fork clone, not
        # the local DECOMP_REPO checkout.
        yaml_path = self.clone.path / "config" / f"{self.cfg.binary}.yaml"
        candidates = discover_free_set(
            clone_path=self.clone.path,
            upstream=self.cfg.upstream,
            binary=self.cfg.binary,
            base_branch=self.cfg.upstream_branch,
            queue=self.queue,
            yaml_path=yaml_path,
            max_size=self.cfg.max_function_size,
        )
        # When this process is one of N sharded workers, keep only our residue
        # class so we never contend with our own sibling processes for a VA.
        if self.cfg.shard_count > 1:
            before = len(candidates)
            candidates = shard_functions(
                candidates,
                index=self.cfg.shard_index,
                count=self.cfg.shard_count,
            )
            log.info(
                "shard %d/%d: %d of %d free function(s) in this residue class",
                self.cfg.shard_index,
                self.cfg.shard_count,
                len(candidates),
                before,
            )
        return candidates

    # --- per-function -----------------------------------------------------

    async def handle_one(self, fn: Function) -> str:
        """Claim → branch → match → gate → PR for one function.

        Returns a short status string: "pr_opened" | "lost_claim" |
        "blocked" | "gate_failed" | "pr_failed". Always releases the claim
        best-effort on any non-PR outcome.
        """
        assert self.clone is not None and self.backend is not None
        va = rva_to_va(fn.rva)

        if not self.backend.claim(va):
            return "lost_claim"

        try:
            branch = start_function_branch(self.clone, rva=fn.rva, symbol=fn.symbol)

            # Reuse the local context + tier machinery so the brief + model
            # choice match local mode exactly.
            ctx = build_context(fn, repo=self.clone.path)
            tier = classify_tier(fn, ctx)
            model = self.cfg.model_for_tier(tier)

            from .worker import run_match_loop  # lazy: SDK import

            # Heartbeat once before the (potentially long) match loop so the
            # lease covers the SDK turn budget. ``distributed=True`` selects
            # the fork/PR brief variant (single _rosetta/FUN_<va>.cpp, no
            # YAML, `make rosetta`) so the loop's output clears the PR-gate.
            self.backend.heartbeat(va)
            outcome, iterations = await run_match_loop(
                cfg=self.cfg,
                fn=fn,
                cwd=self.clone.path,
                model=model,
                ctx=ctx,
                distributed=True,
            )

            if outcome != "matched":
                self.backend.release(va)
                return "blocked"

            # The match loop already committed the new _rosetta file on the
            # per-function branch (the distributed brief instructs `git add`
            # of exactly that file + commit). Gate it before opening a PR.
            #
            # base_solved must come from the UPSTREAM BASE REF, not the
            # working tree: the tree now carries the file the agent just
            # added for THIS VA, so a working-tree scan would falsely report
            # it as "already solved" (a self-collision). Reading the ref
            # (ls-tree upstream/<branch>) excludes the in-flight file.
            base_solved = solved_rvas_from_ref(
                self.clone.path,
                upstream_ref=f"upstream/{self.cfg.upstream_branch}",
                binary=self.cfg.binary,
                image_base=IMAGE_BASE,
            )
            report = run_pr_gate(
                clone_path=self.clone.path,
                base_ref=f"upstream/{self.cfg.upstream_branch}",
                symbol=fn.symbol,
                binary=self.cfg.binary,
                base_solved_vas={rva_to_va(r) for r in base_solved},
            )
            log.info("%s", report.summary())
            if not report.ok:
                self.backend.release(va)
                return "gate_failed"

            # Push the branch to the fork, then open the upstream PR.
            ok, msg = push_function_branch(self.clone, branch)
            if not ok:
                log.warning("branch push failed: %s", msg)
                self.backend.release(va)
                return "pr_failed"

            result = create_pr(
                upstream=self.cfg.upstream,
                base_branch=self.cfg.upstream_branch,
                fork_owner=self.clone.fork_owner,
                branch=branch,
                title=build_pr_title(fn.symbol, fn.rva),
                body=build_pr_body(
                    binary=self.cfg.binary,
                    symbol=fn.symbol,
                    rva=fn.rva,
                    iterations=iterations,
                    gate_summary=report.summary(),
                ),
            )
            if not result.ok:
                self.backend.release(va)
                return "pr_failed"
            # PR open => upstream pins the claim; reconcile releases it on
            # merge. Nothing to release here.
            log.info("PR opened for FUN_%08x: %s", va, result.url)
            return "pr_opened"
        except Exception:
            # Any unexpected failure: release best-effort so the VA frees up.
            self.backend.release(va)
            raise

    # --- driver -----------------------------------------------------------

    async def run(self) -> int:
        self.provision()
        candidates = self.free_set()
        log.info("distributed run: %d eligible function(s)", len(candidates))
        attempts = 0
        opened = 0
        for fn in candidates:
            if attempts >= self.cfg.max_attempts_per_worker:
                break
            attempts += 1
            status = await self.handle_one(fn)
            log.info("FUN_%08x -> %s", rva_to_va(fn.rva), status)
            if status == "pr_opened":
                opened += 1
        log.info(
            "distributed run done: attempts=%d prs_opened=%d", attempts, opened
        )
        # 75 == EX_TEMPFAIL: nothing was claimable/attempted this pass (empty
        # free set, or every candidate lost its claim before an attempt). The
        # start.sh supervisor reads this to back off instead of hot-spinning
        # and re-provisioning the fork every few seconds. Any pass that did
        # work (or tried and failed) returns 0.
        if attempts == 0:
            return 75
        return 0


async def run_distributed(cfg: Config) -> int:
    """Entry point invoked by the orchestrator CLI when mode=distributed."""
    return await DistributedAgent(cfg).run()
