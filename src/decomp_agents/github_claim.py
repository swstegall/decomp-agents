"""GitHub-native claim backend for distributed mode (issue #11, D5).

In LOCAL mode, claims are atomic SQLite rows (work_queue.py). In
DISTRIBUTED mode the agent works from a FORK and has NO write access to
the upstream repo, so it cannot fire repository_dispatch / workflow_dispatch
on upstream. The portable claim path for forks is the **issue_comment** one:

  1. POST `/claim FUN_<va> <binary>` as a comment on a configured upstream
     coordination ISSUE (anyone can comment). That fires upstream's
     `claim.yml`, which binds the claim's `owner` to the AUTHENTICATED
     comment author (== the agent's own gh login), runs `tools/claim.py
     decide`, and commits the ledger mutation onto the orphan `claims`
     branch via a push-CAS.
  2. POLL the upstream `claims` branch: `git fetch upstream claims`, read
     `claims/<bin>/<decimal_rva>.json`. If `owner == my login` and
     `state in {active, expiring, pinned-by-PR}` => WON. Absent or another
     owner => LOST (pick a different VA).

Heartbeat = re-post `/claim` before the lease expires (an owner-held live
lease is an idempotent `extend`). Release is best-effort: the merge-time
reconcile (reconcile.yml::release-solved) is the real releaser once the PR
lands; on bail the agent posts `/unclaim` (if the upstream supports it) or
just lets the lease lapse.

`GitHubClaimBackend` exposes the same logical surface decomp-agents expects
from any claimer — `claim(va) -> bool`, `heartbeat(va)`, `release(va)` — so
the distributed orchestrator can drive it the way the local one drives the
SQLite queue.

The ledger-parse helpers (:func:`read_ledger_claim`, :func:`ledger_win`)
are PURE — they take a claims directory + a login and decide won/lost with
no network — so they are unit-testable against a fake ledger dir.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

IMAGE_BASE = 0x400000

# GitHub's hard per-issue/PR comment ceiling. Once a coordination issue holds
# this many comments, the REST API rejects every further `POST .../comments`
# with HTTP 422 and commenting is permanently disabled for that issue. When
# that happens, no `/claim` lands, `claim.yml` never fires, the ledger never
# updates, and every claim() silently times out — so we detect it and fail
# loudly with an actionable message instead of churning the free set.
GH_ISSUE_COMMENT_CAP = 2500


class ClaimIssueFullError(RuntimeError):
    """The coordination issue has hit GitHub's comment cap (rotate it).

    Raised by :meth:`GitHubClaimBackend.claim` when a `/claim` comment cannot
    be posted because the issue is at :data:`GH_ISSUE_COMMENT_CAP`. This is a
    permanent condition for that issue — the operator must open a fresh
    coordination issue and re-point ``DECOMP_CLAIM_ISSUE`` at it.
    """

# A ledger claim is "won" by us iff its owner matches AND its state is one
# of these. (`tools/claim.py` writes active|expiring|pinned-by-PR; an
# expiring claim still belongs to its owner during the grace window.)
WIN_STATES = ("active", "expiring", "pinned-by-PR")


def va_to_rva(va: int, image_base: int = IMAGE_BASE) -> int:
    return va - image_base


def rva_to_va(rva: int, image_base: int = IMAGE_BASE) -> int:
    return rva + image_base


# ---------------------------------------------------------------------------
# pure ledger parsing (no network) — unit-testable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerClaim:
    binary: str
    rva: int
    owner: str
    state: str
    lease_expires_at: str | None


def read_ledger_claim(
    ledger_dir: Path, binary: str, rva: int
) -> LedgerClaim | None:
    """Read claims/<binary>/<decimal_rva>.json from a checked-out ledger.

    Returns None when the file is absent (no live claim) or unreadable.
    The schema is tools/claim.py's: {binary,rva,va,owner,claimed_at,
    lease_expires_at,state}.
    """
    path = ledger_dir / binary / f"{rva}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return LedgerClaim(
        binary=str(data.get("binary", binary)),
        rva=int(data.get("rva", rva)),
        owner=str(data.get("owner", "")),
        state=str(data.get("state", "active")),
        lease_expires_at=data.get("lease_expires_at"),
    )


def ledger_win(
    ledger_dir: Path, binary: str, rva: int, login: str
) -> bool:
    """True iff the ledger shows `login` owns a live claim for (binary, rva).

    This is the win condition the poller checks after posting `/claim`:
    owner == my login AND state in WIN_STATES. Absent file or another
    owner => not a win (we lost the race / it's already solved).
    """
    claim = read_ledger_claim(ledger_dir, binary, rva)
    if claim is None:
        return False
    return claim.owner == login and claim.state in WIN_STATES


# ---------------------------------------------------------------------------
# the backend (network — git fetch + gh issue comment)
# ---------------------------------------------------------------------------


def _git(clone: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        check=check,
    )


class GitHubClaimBackend:
    """Issue-comment + claims-branch-poll claim backend for a fork agent.

    Parameters
    ----------
    clone_path:
        The fork working clone (has an `upstream` remote pointing at the
        canonical repo — see fork.py::provision_fork).
    upstream:
        "owner/repo" of the canonical repo whose claim.yml we fire.
    claim_issue:
        The upstream coordination issue number to comment on.
    login:
        The agent's own authenticated gh login (its identity / owner).
    binary:
        Binary stem these claims are scoped to (e.g. "ffxivgame").
    poll_timeout_s / poll_interval_s:
        Bounded wait for the upstream CAS to land the ledger commit.
    """

    def __init__(
        self,
        *,
        clone_path: Path,
        upstream: str,
        claim_issue: int,
        login: str,
        binary: str,
        poll_timeout_s: int = 180,
        poll_interval_s: int = 6,
        image_base: int = IMAGE_BASE,
    ) -> None:
        self.clone_path = Path(clone_path)
        self.upstream = upstream
        self.claim_issue = claim_issue
        self.login = login
        self.binary = binary
        self.poll_timeout_s = poll_timeout_s
        self.poll_interval_s = poll_interval_s
        self.image_base = image_base
        # Checked-out ledger location once `git fetch upstream claims` +
        # `git checkout` materialise it. We read it via `git show` to avoid
        # disturbing the working tree's per-function branch.
        self._ledger_cache: Path | None = None

    # --- comment posting --------------------------------------------------

    def _post_claim_comment(self, va: int, *, unclaim: bool = False) -> bool:
        """Post a `/claim` (or `/unclaim`) comment. Returns True iff it landed.

        Failures are NOT swallowed: a non-zero ``gh`` exit is logged at ERROR
        with stderr so a silently-dropped comment can't masquerade as a posted
        one (which previously turned every claim into a 180 s no-op timeout).
        """
        verb = "/unclaim" if unclaim else "/claim"
        # `:08x` (8 hex nibbles) is intentional: it matches both the
        # _rosetta filename convention (FUN_<va_hex>.cpp) and upstream
        # claim.yml's token regex `(FUN_)?0?x?[0-9a-f]{4,8}`. The 8-nibble
        # ceiling is shared with that parser and is safe for the 32-bit
        # PE images (no VA exceeds 0xFFFFFFFF) — keep the two in sync.
        body = f"{verb} FUN_{va:08x} {self.binary}"
        r = subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(self.claim_issue),
                "--repo",
                self.upstream,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            log.error(
                "FAILED to post `%s` on %s#%d (gh exit %d): %s",
                body,
                self.upstream,
                self.claim_issue,
                r.returncode,
                (r.stderr or "").strip() or "<no stderr>",
            )
            return False
        log.info("posted `%s` on %s#%d", body, self.upstream, self.claim_issue)
        return True

    def _issue_at_cap(self) -> bool:
        """True iff the coordination issue is at GitHub's comment cap.

        Queried only after a post FAILS, to classify the failure as the
        permanent cap (=> raise) vs a transient error (=> skip + retry next
        VA). Best-effort: a failed lookup returns False so we don't abort on
        an unrelated network blip.
        """
        r = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{self.upstream}/issues/{self.claim_issue}",
                "--jq",
                ".comments",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return False
        try:
            return int(r.stdout.strip()) >= GH_ISSUE_COMMENT_CAP
        except ValueError:
            return False

    # --- ledger reading ---------------------------------------------------

    def _fetch_ledger(self) -> None:
        """Refresh the local view of the upstream `claims` branch."""
        _git(self.clone_path, "fetch", "upstream", "claims")

    def _read_ledger_claim_via_git(self, rva: int):
        """Read claims/<bin>/<rva>.json out of upstream/claims without
        touching the working tree (uses `git show`)."""
        ref = f"upstream/claims:claims/{self.binary}/{rva}.json"
        r = _git(self.clone_path, "show", ref)
        if r.returncode != 0:
            return None
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
        return LedgerClaim(
            binary=str(data.get("binary", self.binary)),
            rva=int(data.get("rva", rva)),
            owner=str(data.get("owner", "")),
            state=str(data.get("state", "active")),
            lease_expires_at=data.get("lease_expires_at"),
        )

    def _won(self, rva: int) -> bool:
        claim = self._read_ledger_claim_via_git(rva)
        if claim is None:
            return False
        return claim.owner == self.login and claim.state in WIN_STATES

    def _lost_to_other(self, rva: int) -> bool:
        """True iff the ledger shows a LIVE claim owned by someone else.

        Used to short-circuit the poll: if another owner already holds it,
        further polling won't flip it to us — bail early.
        """
        claim = self._read_ledger_claim_via_git(rva)
        return (
            claim is not None
            and claim.owner != self.login
            and claim.state in WIN_STATES
        )

    # --- public surface (mirrors what the orchestrator expects) -----------

    def claim(self, va: int) -> bool:
        """Attempt to claim a VA. Returns True iff we won the ledger.

        Posts `/claim` on the coordination issue, then polls the upstream
        `claims` branch (bounded by ``poll_timeout_s``) for our win. Short-
        circuits to False the moment the ledger shows a live claim owned by
        someone else (we lost the race) — no point waiting out the window.
        """
        rva = va_to_rva(va, self.image_base)
        if not self._post_claim_comment(va):
            # The comment never landed, so claim.yml will never fire and the
            # ledger will never show our win — polling for poll_timeout_s would
            # just burn the window. Classify the failure: a full issue is
            # permanent (raise so the operator rotates DECOMP_CLAIM_ISSUE);
            # anything else is treated as transient (skip this VA, try next).
            if self._issue_at_cap():
                raise ClaimIssueFullError(
                    f"coordination issue {self.upstream}#{self.claim_issue} is "
                    f"at GitHub's {GH_ISSUE_COMMENT_CAP}-comment cap — no claim "
                    f"can be posted. Open a fresh issue and re-point "
                    f"DECOMP_CLAIM_ISSUE at it."
                )
            log.warning(
                "skipping claim FUN_%08x — comment post failed (transient?)", va
            )
            return False
        deadline = time.monotonic() + self.poll_timeout_s
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval_s)
            self._fetch_ledger()
            if self._won(rva):
                log.info("won claim FUN_%08x (%s)", va, self.binary)
                return True
            if self._lost_to_other(rva):
                log.info("lost claim FUN_%08x — held by another owner", va)
                return False
        log.warning(
            "claim FUN_%08x timed out after %ds with no ledger win",
            va,
            self.poll_timeout_s,
        )
        return False

    def heartbeat(self, va: int) -> None:
        """Re-post `/claim` to extend the lease (idempotent `extend`)."""
        self._post_claim_comment(va)

    def release(self, va: int) -> None:
        """Best-effort release. Posts `/unclaim`; if the upstream ignores it,
        the lease simply lapses (sweep.yml quarantines it) or the merge-time
        reconcile releases it once the PR lands. Never raises."""
        try:
            self._post_claim_comment(va, unclaim=True)
        except Exception as exc:  # noqa: BLE001 — release is always best-effort
            log.debug("release of FUN_%08x is best-effort; ignoring %r", va, exc)
