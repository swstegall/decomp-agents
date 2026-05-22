"""One-shot CLI: flip conflict-escalated claims back to pending.

When the merge auto-resolver gets smarter (e.g. a new path category
joins the strategy table), previously-escalated claims should get a
second chance to merge automatically. This helper reads the
coordination DB, flips matching claims from ``merge_status='conflict'``
back to ``'pending'``, and archives their escalation JSONs into a
sibling ``_retried/`` dir so they survive in case we need to re-inspect.

Usage:

    python -m decomp_agents.retry_escalated                # current session, all binaries
    python -m decomp_agents.retry_escalated --all-sessions  # cross-session drain
    python -m decomp_agents.retry_escalated --binary ffxivgame
    python -m decomp_agents.retry_escalated --dry-run       # report only
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import click

from .config import load_config
from .merge import reset_escalated_to_pending
from .work_queue import WorkQueue

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema" / "coordination.sql"


@click.command()
@click.option(
    "--all-sessions",
    "all_sessions",
    is_flag=True,
    help="Reset conflict claims from every session, not just the most recent one.",
)
@click.option(
    "--binary",
    default=None,
    help="Limit to one binary stem (ffxivgame, ffxivlogin, etc.).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would change without touching the DB or filesystem.",
)
def main(all_sessions: bool, binary: str | None, dry_run: bool) -> None:
    """Flip conflict-escalated claims back to pending for the next merge pass."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [retry-escalated] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config()
    queue = WorkQueue(cfg.coordination_db, SCHEMA_PATH)

    if dry_run:
        # Cheap dry-run: count matching rows without flipping anything.
        sql_where = ["merge_status = 'conflict'", "released_at IS NOT NULL"]
        params: list = []
        if binary is not None:
            sql_where.append("binary = ?")
            params.append(binary)
        if not all_sessions:
            sql_where.append("session_id = (SELECT MAX(id) FROM sessions)")
        sql = f"SELECT COUNT(*) AS n FROM claims WHERE {' AND '.join(sql_where)}"
        with queue._conn() as conn:  # noqa: SLF001
            row = conn.execute(sql, params).fetchone()
        click.echo(f"[dry-run] would reset {row['n']} claim(s) to pending")
        return

    flipped = reset_escalated_to_pending(
        queue,
        cross_session=all_sessions,
        binary=binary,
    )
    if not flipped:
        click.echo("no escalated claims matched — nothing to do")
        return

    # Archive any escalation JSONs that correspond to flipped claims.
    archive_dir = cfg.merges_dir / "escalated" / "_retried"
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for claim in flipped:
        fname = (
            f"claim-{claim.id:06d}_{claim.function.binary}_{claim.function.rva_hex}.json"
        )
        src = cfg.merges_dir / "escalated" / fname
        if src.exists():
            dst = archive_dir / fname
            shutil.move(str(src), str(dst))
            moved += 1

    click.echo(
        f"reset {len(flipped)} claim(s) to pending; archived {moved} escalation JSON(s) → {archive_dir}"
    )
    click.echo("Re-run `python orchestrator.py` to drain them.")


if __name__ == "__main__":
    main()
