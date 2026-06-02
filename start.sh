#!/usr/bin/env bash
# Boot decomp-agents: ensure venv exists, deps are installed, .env is loaded,
# then exec the orchestrator. Idempotent — safe to re-run.
#
# Usage:
#   ./start.sh                  # run orchestrator
#   ./start.sh --dry-run        # dry run (no agents spawned)
#   FORCE_REINSTALL=1 ./start.sh   # re-run `pip install -e .`
#   PYTHON=python3.12 ./start.sh   # pin interpreter when creating venv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON="${PYTHON:-python3}"
STAMP="$VENV_DIR/.deps-installed"

have_uv() { command -v uv >/dev/null 2>&1; }

# 1. Create venv if missing.
if [[ ! -d "$VENV_DIR" ]]; then
  echo "[start.sh] creating venv at $VENV_DIR"
  if have_uv; then
    uv venv "$VENV_DIR"
  else
    "$PYTHON" -m venv "$VENV_DIR"
  fi
fi

# 2. Activate.
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 3. Install deps if first run, if pyproject.toml is newer than the stamp,
#    or if FORCE_REINSTALL=1.
needs_install=0
if [[ ! -f "$STAMP" ]] || [[ "$SCRIPT_DIR/pyproject.toml" -nt "$STAMP" ]] || [[ "${FORCE_REINSTALL:-0}" == "1" ]]; then
  needs_install=1
fi

if [[ "$needs_install" == "1" ]]; then
  echo "[start.sh] installing dependencies (editable)"
  if have_uv; then
    uv pip install -e .
  else
    python -m pip install --upgrade pip >/dev/null
    python -m pip install -e .
  fi
  touch "$STAMP"
fi

# 4. Load .env if present (export every assignment).
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

# 4.5. Keep meteor-decomp's develop current before handing off. Safe ff-only:
#       the script no-ops on a dirty tree or local-ahead commits, so it can
#       never clobber work — it just avoids starting a run on a stale develop.
#       DECOMP_REPO (from .env) points at the checkout the workers build from.
SYNC_REPO="${DECOMP_REPO:-../meteor-decomp}"
if [[ -x "$SYNC_REPO/tools/sync-develop.sh" ]]; then
  echo "[start.sh] syncing $SYNC_REPO develop (ff-only)"
  "$SYNC_REPO/tools/sync-develop.sh" || true
fi

# 4.6. Drain any GREEN matches a prior LOCAL-mode run stranded on develop.
#       Skip entirely in distributed mode: there the main checkout is never a
#       push target (matches ship via output/fork PRs), and push-matches.sh's
#       Phase 0 would `git add`+commit a stray _rosetta file onto develop — a
#       commit it then can't push, leaving it stranded ("develop ahead 1").
if [[ "${DECOMP_MODE:-local}" == "local" ]]; then
  if [[ -x "$SYNC_REPO/tools/push-matches.sh" ]]; then
    echo "[start.sh] pushing any stranded GREEN matches on $SYNC_REPO develop"
    "$SYNC_REPO/tools/push-matches.sh" || true
  fi
else
  echo "[start.sh] skipping push-matches.sh (DECOMP_MODE=${DECOMP_MODE:-local}, not local)"
fi

# 5. Hand off to the orchestrator.
#
#    LOCAL mode: orchestrator.main() blocks for the whole session (it owns the
#    workers + merge loop and installs its own signal handling), so keep the
#    original single `exec` — run-once-then-stop is preserved byte-for-byte.
#
#    DISTRIBUTED mode: one DistributedAgent.run() pass is intentionally bounded
#    (discover free set, attempt <= MAX_ATTEMPTS functions, exit). Supervise it
#    in a loop so work keeps draining across passes until the operator hits
#    Ctrl+C.
if [[ "${DECOMP_MODE:-local}" != "distributed" ]]; then
  exec decomp-agents "$@"
fi

BUSY_SLEEP="${DECOMP_SUPERVISE_BUSY_SLEEP:-5}"    # gap after a pass that did work
IDLE_SLEEP="${DECOMP_SUPERVISE_IDLE_SLEEP:-60}"   # backoff when the free set was empty
MAX_SLEEP="${DECOMP_SUPERVISE_MAX_SLEEP:-300}"    # cap on escalating idle backoff

_stop=0
_child=0
on_signal() {
  _stop=1
  [[ "$_child" -ne 0 ]] && kill -TERM "$_child" 2>/dev/null || true
}
trap on_signal INT TERM

# Interruptible sleep: a Ctrl+C during the gap returns immediately instead of
# blocking the full duration.
nap() {
  local secs="$1"
  [[ "$secs" -le 0 ]] && return 0
  sleep "$secs" &
  _child=$!
  wait "$_child" 2>/dev/null || true
  _child=0
}

echo "[start.sh] supervising decomp-agents (distributed) — Ctrl+C to stop"
idle_backoff="$IDLE_SLEEP"
while [[ "$_stop" -eq 0 ]]; do
  # Run one bounded pass as a backgrounded child so the bash trap fires on
  # Ctrl+C even mid-pass. Disable -e around it so a non-zero pass (a crash, a
  # transient gh hiccup) doesn't abort the supervisor.
  set +e
  decomp-agents "$@" &
  _child=$!
  wait "$_child"
  rc=$?
  _child=0
  set -e

  [[ "$_stop" -ne 0 ]] && break
  if [[ "$rc" -eq 130 || "$rc" -eq 143 ]]; then
    echo "[start.sh] orchestrator interrupted (rc=$rc) — stopping"
    break
  fi

  if [[ "$rc" -eq 75 ]]; then
    # No claimable work this pass: back off (escalating up to MAX_SLEEP) so an
    # empty free set doesn't hot-spin / re-provision the fork every few seconds.
    echo "[start.sh] no claimable work — sleeping ${idle_backoff}s"
    nap "$idle_backoff"
    idle_backoff=$(( idle_backoff * 2 ))
    [[ "$idle_backoff" -gt "$MAX_SLEEP" ]] && idle_backoff="$MAX_SLEEP"
  else
    [[ "$rc" -ne 0 ]] && echo "[start.sh] pass exited rc=$rc — retrying after ${BUSY_SLEEP}s"
    idle_backoff="$IDLE_SLEEP"   # reset backoff after a productive/normal pass
    nap "$BUSY_SLEEP"
  fi
done

echo "[start.sh] supervisor stopped"
exit 0
