"""SQLite-backed claim queue. Source of truth: meteor-decomp's config/<binary>.yaml.

Why SQLite instead of git-committed YAML:
  config/*.yaml is gitignored in meteor-decomp (regenerated from
  `make split`), so we can't use git commits as the claim mechanism.
  SQLite gives us OS-level atomic claims with WAL-mode concurrency.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

# Symbols compare.py can diff today. The matching-workflow.md docs
# imply named symbols (e.g. `Blowfish::Init`) work too, but
# tools/compare.py line 393 ("only FUN_<address> names supported")
# rejects everything else. Workers claiming non-matching symbols would
# burn their iteration budget on undiffable targets. Filter at the
# source. Loosen this regex once compare.py learns name→RVA mapping.
DIFF_COMPATIBLE_SYMBOL_RE = re.compile(r"^FUN_[0-9a-f]+$")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Function:
    binary: str
    rva: int
    end: int
    size: int
    module: str
    symbol: str
    type: str
    status: str
    section: str

    @property
    def rva_hex(self) -> str:
        return f"0x{self.rva:08x}"

    @property
    def safe_branch_name(self) -> str:
        safe_sym = self.symbol.replace("::", "__").replace("/", "_").replace(".", "_")
        return f"{self.binary}/{self.rva_hex}_{safe_sym}"


@dataclass(frozen=True)
class ClaimRecord:
    id: int
    function: Function
    worker_id: int
    branch: str
    iterations: int = 0


class WorkQueue:
    def __init__(self, db_path: Path, schema_path: Path) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(self.schema_path.read_text())

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=15.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            yield conn
        finally:
            conn.close()

    # --- session / worker bookkeeping --------------------------------------

    def begin_session(
        self,
        *,
        worker_count: int,
        binary: str,
        repo_path: Path,
        notes: str | None = None,
    ) -> int:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions(started_at, worker_count, binary, repo_path, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, worker_count, binary, str(repo_path), notes),
            )
            return cur.lastrowid

    def end_session(self, session_id: int) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (now, session_id),
            )
            # Release any in-flight claims this session still owns so
            # they don't block future sessions if we exited mid-claim.
            conn.execute(
                "UPDATE claims SET released_at = ?, outcome = 'abandoned' "
                "WHERE session_id = ? AND released_at IS NULL",
                (now, session_id),
            )

    def release_stale_claims(self) -> int:
        """Release every active claim that belongs to a session which has already ended.

        Run this at the top of every new session. If a prior orchestrator
        crashed (kill -9, OOM, power loss) before `end_session` ran, its
        claims would stay `released_at IS NULL` forever and silently
        block their RVAs in all future sessions.

        Returns the number of claims released.
        """
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE claims SET released_at = ?, outcome = 'abandoned' "
                "WHERE released_at IS NULL "
                "  AND session_id IN (SELECT id FROM sessions WHERE ended_at IS NOT NULL)",
                (now,),
            )
            return cur.rowcount or 0

    def count_active_claims(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM claims WHERE released_at IS NULL"
            ).fetchone()
            return row["n"]

    def register_worker(
        self,
        *,
        session_id: int,
        agent_id: int,
        worktree_path: Path,
        branch_prefix: str,
        pid: int | None,
    ) -> int:
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO workers(session_id, agent_id, worktree_path, branch_prefix, "
                "pid, status, started_at, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?, 'idle', ?, ?) "
                "ON CONFLICT(session_id, agent_id) DO UPDATE SET "
                "  worktree_path=excluded.worktree_path, "
                "  branch_prefix=excluded.branch_prefix, "
                "  pid=excluded.pid, "
                "  status='idle', "
                "  started_at=excluded.started_at, "
                "  last_heartbeat=excluded.last_heartbeat "
                "RETURNING id",
                (session_id, agent_id, str(worktree_path), branch_prefix, pid, now, now),
            )
            row = cur.fetchone()
            return row["id"]

    def set_worker_status(self, worker_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE workers SET status = ?, last_heartbeat = ? WHERE id = ?",
                (status, _utcnow_iso(), worker_id),
            )

    def heartbeat(self, worker_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE id = ?",
                (_utcnow_iso(), worker_id),
            )

    # --- function pool -----------------------------------------------------

    def load_pool(self, yaml_path: Path) -> list[Function]:
        """Read the meteor-decomp YAML and return every row as a Function."""
        with yaml_path.open() as f:
            rows = yaml.safe_load(f) or []
        binary = yaml_path.stem
        out: list[Function] = []
        for row in rows:
            out.append(
                Function(
                    binary=binary,
                    rva=int(row["rva"], 16) if isinstance(row["rva"], str) else int(row["rva"]),
                    end=int(row["end"], 16) if isinstance(row["end"], str) else int(row["end"]),
                    size=int(row["size"], 16) if isinstance(row["size"], str) else int(row["size"]),
                    module=row.get("module") or "_unknown",
                    symbol=row.get("symbol") or f"FUN_{int(row['rva'], 16):08x}",
                    type=row.get("type") or "matching",
                    status=row.get("status") or "unmatched",
                    section=row.get("section") or ".text",
                )
            )
        return out

    def filter_eligible(
        self,
        pool: list[Function],
        *,
        max_size: int,
        allowed_types: tuple[str, ...] = ("matching",),
        allowed_statuses: tuple[str, ...] = ("unmatched",),
        skip_naked_stubs: bool = True,
        require_diff_compatible: bool = True,
    ) -> list[Function]:
        out: list[Function] = []
        for fn in pool:
            if fn.type not in allowed_types:
                continue
            if fn.status not in allowed_statuses:
                continue
            if fn.size > max_size:
                continue
            if skip_naked_stubs and _is_likely_naked_stub(fn):
                # Tiny `_unknown`-module functions are usually __declspec(naked)
                # thunks — these are deterministically handled by the
                # passthrough path (`make passthrough`), not by matching.
                # Attempting to match them wastes turns.
                continue
            if require_diff_compatible and not DIFF_COMPATIBLE_SYMBOL_RE.match(fn.symbol):
                # tools/compare.py currently only handles FUN_<addr>
                # symbols. Named/thunk symbols (~7% of ffxivgame's pool)
                # would burn the entire iteration budget on a make-diff
                # call that returns rc=3 every time. Skip until compare.py
                # learns name→RVA resolution.
                continue
            out.append(fn)
        return out

    # --- claim / release ---------------------------------------------------

    def claim_next(
        self,
        *,
        session_id: int,
        worker_id: int,
        candidates: list[Function],
        branch_prefix: str,
    ) -> ClaimRecord | None:
        """Atomically grant the worker exclusive access to one candidate.

        Walks the candidate list in order, taking the first RVA that
        has neither an active claim nor a prior successful claim. SQLite's
        IMMEDIATE transaction serialises concurrent claimers.

        Why "prior successful claim" matters: config/<bin>.yaml's `status`
        field gets flipped to `matched` only after the post-merge stamping
        pipeline runs validate_clusters + update_yaml_status — and that
        pipeline silently fast-paths cluster singletons, so a singleton
        match leaves YAML status stuck at `unmatched`. The worker that
        loads `candidates` from YAML therefore sees the RVA as still
        eligible. SQLite is the canonical record of work-done; check both
        active-claim and historical-success here so duplicate matches
        can't slip through. `blocked` / `abandoned` / `error` outcomes
        stay re-eligible — those are "this attempt didn't succeed", not
        "this function is done".
        """
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                claimed: set[tuple[str, int]] = {
                    (row["binary"], row["rva"])
                    for row in conn.execute(
                        "SELECT binary, rva FROM claims "
                        "WHERE released_at IS NULL "
                        "   OR outcome IN ('matched', 'functional', 'passthrough')"
                    )
                }
                for fn in candidates:
                    if (fn.binary, fn.rva) in claimed:
                        continue
                    branch = f"{branch_prefix}/{fn.safe_branch_name}"
                    cur = conn.execute(
                        "INSERT INTO claims("
                        "  session_id, worker_id, binary, rva, symbol, module, "
                        "  size_bytes, claimed_at, branch, merge_status"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending') "
                        "RETURNING id",
                        (
                            session_id,
                            worker_id,
                            fn.binary,
                            fn.rva,
                            fn.symbol,
                            fn.module,
                            fn.size,
                            now,
                            branch,
                        ),
                    )
                    claim_id = cur.fetchone()["id"]
                    conn.execute("COMMIT;")
                    return ClaimRecord(
                        id=claim_id, function=fn, worker_id=worker_id, branch=branch
                    )
                conn.execute("COMMIT;")
                return None
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def release(
        self,
        claim_id: int,
        *,
        outcome: str,
        iterations: int,
        merge_status: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE claims SET released_at = ?, outcome = ?, iterations = ?, "
                "merge_status = COALESCE(?, merge_status) "
                "WHERE id = ? AND released_at IS NULL",
                (_utcnow_iso(), outcome, iterations, merge_status, claim_id),
            )

    def update_iterations(self, claim_id: int, iterations: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE claims SET iterations = ? WHERE id = ?",
                (iterations, claim_id),
            )

    # --- observability -----------------------------------------------------

    def log_tool_event(
        self,
        *,
        worker_id: int,
        claim_id: int | None,
        tool_name: str,
        phase: str,
        input_payload: dict | None,
        output_excerpt: str | None,
        duration_ms: int | None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tool_events("
                "  worker_id, claim_id, ts, tool_name, phase, "
                "  input_json, output_excerpt, duration_ms"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    worker_id,
                    claim_id,
                    _utcnow_iso(),
                    tool_name,
                    phase,
                    json.dumps(input_payload, default=str) if input_payload else None,
                    output_excerpt,
                    duration_ms,
                ),
            )

    def log_merge(
        self,
        *,
        claim_id: int,
        base_sha: str,
        branch_sha: str,
        result: str,
        notes: str | None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO merges(claim_id, started_at, finished_at, base_sha, "
                "branch_sha, result, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (claim_id, _utcnow_iso(), _utcnow_iso(), base_sha, branch_sha, result, notes),
            )

    def pending_merges(
        self, session_id: int, *, cross_session: bool = False
    ) -> list[ClaimRecord]:
        """Claims marked `outcome=matched` (or functional/passthrough) that haven't been merged.

        By default returns only the current session's pending merges
        (legacy behaviour). With ``cross_session=True``, also drains
        prior-session pending merges — their branches still exist on
        disk and just never got processed before the prior orchestrator
        ended. That recovers stranded work without re-running matching.
        """
        with self._conn() as conn:
            if cross_session:
                rows = conn.execute(
                    "SELECT c.id, c.binary, c.rva, c.symbol, c.module, c.size_bytes, "
                    "       c.branch, c.iterations, c.worker_id "
                    "FROM claims c "
                    "WHERE c.released_at IS NOT NULL "
                    "  AND c.outcome IN ('matched', 'functional', 'passthrough') "
                    "  AND c.merge_status = 'pending' "
                    # Prefer the current session's claims first so the
                    # active workers see their work merge promptly,
                    # then drain back-pressure from prior sessions.
                    "ORDER BY (c.session_id = ?) DESC, c.id ASC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.id, c.binary, c.rva, c.symbol, c.module, c.size_bytes, "
                    "       c.branch, c.iterations, c.worker_id "
                    "FROM claims c "
                    "WHERE c.session_id = ? "
                    "  AND c.released_at IS NOT NULL "
                    "  AND c.outcome IN ('matched', 'functional', 'passthrough') "
                    "  AND c.merge_status = 'pending'",
                    (session_id,),
                ).fetchall()
        out: list[ClaimRecord] = []
        for row in rows:
            fn = Function(
                binary=row["binary"],
                rva=row["rva"],
                end=row["rva"] + row["size_bytes"],
                size=row["size_bytes"],
                module=row["module"] or "_unknown",
                symbol=row["symbol"],
                type="matching",
                status="matched",
                section=".text",
            )
            out.append(
                ClaimRecord(
                    id=row["id"],
                    function=fn,
                    worker_id=row["worker_id"],
                    branch=row["branch"],
                    iterations=row["iterations"],
                )
            )
        return out

    def mark_merge(self, claim_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE claims SET merge_status = ? WHERE id = ?",
                (status, claim_id),
            )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_likely_naked_stub(fn: Function) -> bool:
    """Heuristic: identify thunks the matching path can't handle.

    Functions with size ≤ 0x10 bytes AND module=_unknown are usually
    one of:
      - PE-import thunks (5-byte JMP DWORD PTR [iat])
      - __declspec(naked) helpers (PUSH N; CALL X; POP ECX; RET)
      - Compiler-emitted CRT helpers (alloca probe, security cookie)

    None of these match through normal C++ — they're deterministically
    handled by `make passthrough` which wraps the raw bytes.
    """
    return fn.size <= 0x10 and fn.module == "_unknown"
