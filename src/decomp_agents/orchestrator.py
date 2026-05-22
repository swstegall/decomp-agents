"""Orchestrator entry point. Spawns N workers as subprocesses, runs merge passes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .config import Config, load_config
from .merge import run_merge_pass
from .work_queue import WorkQueue
from .worktree import provision

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema" / "coordination.sql"


@dataclass
class WorkerHandle:
    agent_id: int
    worker_id: int
    branch_prefix: str
    worktree_path: Path
    process: asyncio.subprocess.Process | None = None
    stdout_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None


class Orchestrator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.queue = WorkQueue(cfg.coordination_db, SCHEMA_PATH)
        self.session_id: int | None = None
        self.workers: list[WorkerHandle] = []
        self._shutdown = asyncio.Event()

    async def run(self) -> int:
        self._ensure_dirs()
        self.session_id = self.queue.begin_session(
            worker_count=self.cfg.worker_count,
            binary=self.cfg.binary,
            repo_path=self.cfg.repo,
            notes=f"binary={self.cfg.binary}, workers={self.cfg.worker_count}",
        )
        log.info(
            "session %d begun — repo=%s binary=%s workers=%d model=%s auth=%s push=%s "
            "post_merge_stamp=%s cross_session_merge=%s",
            self.session_id,
            self.cfg.repo,
            self.cfg.binary,
            self.cfg.worker_count,
            self.cfg.worker_model,
            self.cfg.auth_mode,
            self.cfg.autopush,
            "on" if self.cfg.post_merge_stamp else "off",
            "on" if self.cfg.cross_session_merge else "off",
        )
        if self.cfg.auth_mode == "subscription":
            log.info(
                "auth: subscription OAuth via macOS Keychain. "
                "If workers fail with 401, run `claude` once to (re-)login."
            )
        released = self.queue.release_stale_claims()
        if released:
            log.warning(
                "released %d orphaned active claim(s) from previously-ended sessions",
                released,
            )
        self._sanity_check_push()
        self._sanity_check_pool()
        self._sanity_check_build()

        self._verify_yaml_present()
        self._provision_worktrees()
        await self._spawn_workers()

        merge_task = asyncio.create_task(self._merge_loop())
        try:
            await self._wait_for_workers()
        finally:
            self._shutdown.set()
            with contextlib.suppress(Exception):
                await merge_task
            self.queue.end_session(self.session_id)
            log.info("session %d ended", self.session_id)
        return 0

    # --- setup ------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for d in (
            self.cfg.output_dir,
            self.cfg.transcripts_dir,
            self.cfg.logs_dir,
            self.cfg.claims_dir,
            self.cfg.merges_dir,
            self.cfg.merges_dir / "escalated",
        ):
            d.mkdir(parents=True, exist_ok=True)

    def _verify_yaml_present(self) -> None:
        if not self.cfg.yaml_path.exists():
            raise RuntimeError(
                f"{self.cfg.yaml_path} not found.\n"
                f"Run `make split BINARY={self.cfg.binary}.exe` in {self.cfg.repo} first."
            )

    def _provision_worktrees(self) -> None:
        # base_branch is whatever the repo is on; we don't force `master`
        # in case the user is working on a feature branch.
        for agent_id in range(self.cfg.worker_count):
            wt_path = self.cfg.worktree_for(agent_id)
            branch_prefix = self.cfg.branch_prefix_for(agent_id)
            holding_branch = f"{branch_prefix}/_holding"
            provision(
                repo=self.cfg.repo,
                target=wt_path,
                branch=holding_branch,
                agent_id=agent_id,
            )
            worker_id = self.queue.register_worker(
                session_id=self.session_id,  # type: ignore[arg-type]
                agent_id=agent_id,
                worktree_path=wt_path,
                branch_prefix=branch_prefix,
                pid=None,
            )
            self.workers.append(
                WorkerHandle(
                    agent_id=agent_id,
                    worker_id=worker_id,
                    branch_prefix=branch_prefix,
                    worktree_path=wt_path,
                )
            )
            log.info(
                "worktree ready: agent-%d → %s (worker_id=%d)",
                agent_id,
                wt_path,
                worker_id,
            )

    # --- workers ----------------------------------------------------------

    async def _spawn_workers(self) -> None:
        for h in self.workers:
            cmd = [
                sys.executable,
                "-m",
                "decomp_agents.worker",
                "--agent-id",
                str(h.agent_id),
                "--worker-id",
                str(h.worker_id),
                "--session-id",
                str(self.session_id),
                "--worktree",
                str(h.worktree_path),
                "--branch-prefix",
                h.branch_prefix,
            ]
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )
            h.process = proc
            log.info("spawned agent-%d pid=%d", h.agent_id, proc.pid)
            h.stdout_task = asyncio.create_task(self._consume_stream(h, proc.stdout, "stdout"))
            h.stderr_task = asyncio.create_task(self._consume_stream(h, proc.stderr, "stderr"))

    async def _consume_stream(
        self, handle: WorkerHandle, stream: asyncio.StreamReader | None, label: str
    ) -> None:
        if stream is None:
            return
        log_path = self.cfg.logs_dir / f"agent-{handle.agent_id}.{label}.log"
        with log_path.open("ab") as sink:
            while True:
                line = await stream.readline()
                if not line:
                    return
                sink.write(line)
                sink.flush()
                if self.cfg.live_tail:
                    if label == "stdout":
                        # Workers emit JSON lines on stdout — pretty-print
                        # the kind+key fields if we can.
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict):
                                kind = event.get("kind", "?")
                                extra = {k: v for k, v in event.items() if k != "kind"}
                                click.echo(
                                    f"[agent-{handle.agent_id}] {kind}: {extra}",
                                    err=False,
                                )
                                continue
                        except Exception:
                            pass
                    sys.stderr.write(
                        f"[agent-{handle.agent_id}/{label}] {line.decode(errors='replace')}"
                    )

    async def _wait_for_workers(self) -> None:
        for h in self.workers:
            if h.process is None:
                continue
            rc = await h.process.wait()
            log.info("agent-%d exited rc=%d", h.agent_id, rc)
            for t in (h.stdout_task, h.stderr_task):
                if t is not None:
                    with contextlib.suppress(Exception):
                        await t

    # --- merge loop -------------------------------------------------------

    async def _merge_loop(self) -> None:
        if self.session_id is None:
            return
        merge_worktree = self.cfg.worktree_root / "_merge"
        escalation_dir = self.cfg.merges_dir / "escalated"
        push_branches = self.cfg.autopush in ("branches", "branches+master")
        push_master = self.cfg.autopush == "branches+master"
        post_merge_stamp = self.cfg.post_merge_stamp
        cross_session = self.cfg.cross_session_merge
        while not self._shutdown.is_set():
            try:
                outcomes = run_merge_pass(
                    queue=self.queue,
                    session_id=self.session_id,
                    repo=self.cfg.repo,
                    base_branch=self._base_branch(),
                    merge_worktree_path=merge_worktree,
                    escalation_dir=escalation_dir,
                    push_branches=push_branches,
                    push_master=push_master,
                    post_merge_stamp=post_merge_stamp,
                    cross_session=cross_session,
                )
                if outcomes:
                    log.info(
                        "merge pass: %d outcomes %s",
                        len(outcomes),
                        [o.result for o in outcomes],
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception("merge pass failed: %s", exc)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=20.0)
            except asyncio.TimeoutError:
                pass
        # One final pass after shutdown signalled.
        try:
            run_merge_pass(
                queue=self.queue,
                session_id=self.session_id,
                repo=self.cfg.repo,
                base_branch=self._base_branch(),
                merge_worktree_path=merge_worktree,
                escalation_dir=escalation_dir,
                push_branches=push_branches,
                push_master=push_master,
                post_merge_stamp=post_merge_stamp,
                cross_session=cross_session,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("final merge pass failed: %s", exc)

    def _base_branch(self) -> str:
        # Use the branch the repo was on when the orchestrator started.
        import subprocess

        result = subprocess.run(
            ["git", "-C", str(self.cfg.repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _sanity_check_build(self) -> None:
        """Verify cl-wine.sh + objdiff are healthy before workers spawn.

        Picks an already-matched FUN_<addr> function (the only kind
        tools/compare.py can diff today) and re-runs the per-function
        build + diff. If this fails, no point launching N workers —
        they'll all fail the same way. Warning-only (not fatal) because
        the user might be running with a pre-built object cache.
        """
        import subprocess
        import yaml

        from .work_queue import DIFF_COMPATIBLE_SYMBOL_RE

        yaml_path = self.cfg.yaml_path
        try:
            rows = yaml.safe_load(yaml_path.read_text()) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("smoke test skipped — cannot read %s: %s", yaml_path, exc)
            return

        target = None
        for row in rows:
            sym = row.get("symbol") or ""
            if (
                row.get("status") == "matched"
                and row.get("type") == "matching"
                and DIFF_COMPATIBLE_SYMBOL_RE.match(sym)
            ):
                target = row
                break

        if target is None:
            log.info(
                "smoke test skipped — no matched FUN_<addr> functions in %s.yaml "
                "(compare.py can't diff named/thunk symbols today)",
                self.cfg.binary,
            )
            return

        symbol = target.get("symbol")
        log.info("build smoke test: make diff FUNC=%s", symbol)
        result = subprocess.run(
            ["make", "-C", str(self.cfg.repo), "diff", f"FUNC={symbol}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            log.info("build smoke test OK (FUNC=%s)", symbol)
        else:
            log.warning(
                "build smoke test FAILED (rc=%d, FUNC=%s): %s\n"
                "Workers will likely fail too. Check tools/cl-wine.sh, Wine, "
                "and the MSVC 2005 install under tools/.",
                result.returncode,
                symbol,
                (result.stderr or result.stdout)[-600:].strip(),
            )

    def _sanity_check_pool(self) -> None:
        """Warn loudly if the eligible-function pool is too small to feed the workers."""
        pool = self.queue.load_pool(self.cfg.yaml_path)
        eligible = self.queue.filter_eligible(pool, max_size=self.cfg.max_function_size)
        active = self.queue.count_active_claims()
        free = max(0, len(eligible) - active)
        log.info(
            "pool: total=%d eligible=%d active_claims=%d free=%d (cap size <= 0x%x)",
            len(pool),
            len(eligible),
            active,
            free,
            self.cfg.max_function_size,
        )
        if free == 0:
            log.warning(
                "No claimable functions left in %s. Workers will exit "
                "immediately. Try a different DECOMP_BINARY (e.g. ffxivgame "
                "has ~12k unmatched), or raise DECOMP_MAX_FUNCTION_SIZE.",
                self.cfg.binary,
            )
        elif free < self.cfg.worker_count:
            log.warning(
                "Only %d claimable function(s) for %d workers — %d worker(s) "
                "will exit with no work. Consider lowering DECOMP_AGENT_WORKERS.",
                free,
                self.cfg.worker_count,
                self.cfg.worker_count - free,
            )

    def _sanity_check_push(self) -> None:
        """Verify the remote + auth are reachable before workers run.

        We don't want N workers all bouncing off a missing SSH key or
        a stale 2FA token. A single ls-remote is a cheap upfront check
        that turns a confusing late failure into a clear startup error.
        """
        if self.cfg.autopush == "none":
            return
        import subprocess

        remote_check = subprocess.run(
            ["git", "-C", str(self.cfg.repo), "remote"],
            capture_output=True,
            text=True,
        )
        remotes = remote_check.stdout.split()
        if not remotes:
            log.warning(
                "autopush=%s but no git remote configured in %s — workers will skip push silently",
                self.cfg.autopush,
                self.cfg.repo,
            )
            return
        log.info("autopush sanity check: ls-remote origin …")
        check = subprocess.run(
            ["git", "-C", str(self.cfg.repo), "ls-remote", "--exit-code", "origin", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if check.returncode != 0:
            log.warning(
                "ls-remote origin failed (rc=%d): %s\n"
                "Push will be attempted anyway, but workers may fail. "
                "Confirm SSH key / 2FA, or set DECOMP_AUTOPUSH=none to disable push.",
                check.returncode,
                (check.stderr or check.stdout).strip(),
            )
        else:
            log.info("autopush sanity check OK")

    # --- shutdown ---------------------------------------------------------

    def request_shutdown(self) -> None:
        log.info("shutdown requested")
        self._shutdown.set()
        for h in self.workers:
            if h.process is not None and h.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    h.process.terminate()


# ---------------------------------------------------------------------------


@click.command()
@click.option("--dry-run", is_flag=True, help="Provision worktrees + DB, then exit.")
def main(dry_run: bool) -> None:
    """Run the decomp-agents orchestrator."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [orchestrator] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config()
    orch = Orchestrator(cfg)

    if dry_run:
        orch._ensure_dirs()
        orch.session_id = orch.queue.begin_session(
            worker_count=cfg.worker_count,
            binary=cfg.binary,
            repo_path=cfg.repo,
            notes="dry-run",
        )
        orch._verify_yaml_present()
        orch._provision_worktrees()
        released = orch.queue.release_stale_claims()
        if released:
            click.echo(f"  released_stale = {released}")
        click.echo(f"dry-run OK: session_id={orch.session_id}")
        click.echo(f"  repo            = {cfg.repo}")
        click.echo(f"  binary          = {cfg.binary}")
        click.echo(f"  worker_count    = {cfg.worker_count}")
        click.echo(f"  worker_model    = {cfg.worker_model}")
        click.echo(f"  auth_mode       = {cfg.auth_mode}")
        click.echo(f"  autopush        = {cfg.autopush}")
        click.echo(f"  post_merge_stamp= {'on' if cfg.post_merge_stamp else 'off'}")
        click.echo(f"  cross_session   = {'on' if cfg.cross_session_merge else 'off'}")
        click.echo(f"  worktree_root   = {cfg.worktree_root}")
        click.echo(f"  output_dir      = {cfg.output_dir}")
        click.echo(f"  coordination_db = {cfg.coordination_db}")
        orch._sanity_check_push()
        orch._sanity_check_pool()
        orch.queue.end_session(orch.session_id)
        return

    def _sig(signum, frame):  # noqa: ARG001
        log.warning("signal %d → shutdown", signum)
        orch.request_shutdown()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    rc = asyncio.run(orch.run())
    sys.exit(rc)


if __name__ == "__main__":
    main()
