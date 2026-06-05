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
if [[ "${DECOMP_MODE:-local}" != "distributed" ]]; then
  exec decomp-agents "$@"
fi

#    DISTRIBUTED mode: one DistributedAgent.run() pass is intentionally bounded
#    (discover free set, attempt <= MAX_ATTEMPTS functions, exit). Run
#    DECOMP_AGENT_WORKERS such processes concurrently for throughput, each with
#    its OWN fork clone + output dir and its OWN VA residue class (shard), and
#    supervise each so work keeps draining across passes until Ctrl+C. The
#    shards never contend for the same VA (rva % N partition); the upstream
#    claim ledger still coordinates against other contributors.
NPROC="${DECOMP_AGENT_WORKERS:-1}"
[[ "$NPROC" =~ ^[0-9]+$ ]] && [[ "$NPROC" -ge 1 ]] || NPROC=1

BUSY_SLEEP="${DECOMP_SUPERVISE_BUSY_SLEEP:-5}"    # gap after a pass that did work
IDLE_SLEEP="${DECOMP_SUPERVISE_IDLE_SLEEP:-60}"   # backoff when the free set was empty
MAX_SLEEP="${DECOMP_SUPERVISE_MAX_SLEEP:-300}"    # cap on escalating idle backoff
STAGGER="${DECOMP_SHARD_STAGGER_S:-3}"            # per-shard startup stagger (claim politeness)
BASE_OUTPUT_DIR="${DECOMP_OUTPUT_DIR:-$SCRIPT_DIR/output}"

# One shard's persistent supervisor (runs backgrounded; reaped by the parent).
# Re-runs its bounded distributed pass until the parent SIGTERMs it. Args:
#   $1 = shard index, $2 = shard count, then the original CLI args ("$@").
supervise_shard() {
  local idx="$1" count="$2"; shift 2
  local tag="[start.sh shard ${idx}/${count}]"
  local s_stop=0 child=0 rc backoff="$IDLE_SLEEP" shard_out

  # Backgrounded shells ignore SIGINT under `set -m`-off job control, so the
  # parent forwards a SIGTERM; handle it by killing the current pass and exiting.
  shard_stop() { s_stop=1; [[ "$child" -ne 0 ]] && kill -TERM "$child" 2>/dev/null || true; }
  trap shard_stop TERM INT

  # N==1 keeps the original single-process output dir (and reuses output/fork);
  # N>1 gives each shard its own clone + DB + transcripts under output/agent-N.
  if [[ "$count" -le 1 ]]; then shard_out="$BASE_OUTPUT_DIR"; else shard_out="$BASE_OUTPUT_DIR/agent-${idx}"; fi

  # Stagger startup so N shards don't post their first /claim at the same instant.
  if [[ "$idx" -gt 0 && "$STAGGER" -gt 0 ]]; then
    sleep $(( idx * STAGGER )) & child=$!; wait "$child" 2>/dev/null || true; child=0
    [[ "$s_stop" -ne 0 ]] && { echo "$tag stopped"; return 0; }
  fi

  echo "$tag starting (output=$shard_out)"
  while [[ "$s_stop" -eq 0 ]]; do
    # Background the pass + wait so the trap fires on signal even mid-pass.
    # Disable -e so a non-zero pass (crash / transient gh hiccup) doesn't abort.
    set +e
    DECOMP_SHARD_INDEX="$idx" DECOMP_SHARD_COUNT="$count" DECOMP_OUTPUT_DIR="$shard_out" \
      decomp-agents "$@" &
    child=$!
    wait "$child"; rc=$?
    child=0
    set -e

    [[ "$s_stop" -ne 0 ]] && break
    if [[ "$rc" -eq 130 || "$rc" -eq 143 ]]; then echo "$tag interrupted (rc=$rc)"; break; fi

    if [[ "$rc" -eq 78 ]]; then
      # EX_CONFIG: the coordination issue is at GitHub's 2,500-comment cap, so
      # no shard can claim. Hot-looping is pointless — stop this shard and let
      # the operator open a fresh issue + re-point DECOMP_CLAIM_ISSUE.
      echo "$tag claim issue is full (rc=78) — rotate DECOMP_CLAIM_ISSUE; stopping shard"
      break
    fi

    if [[ "$rc" -eq 75 ]]; then
      # No claimable work in this shard: back off (escalating up to MAX_SLEEP).
      echo "$tag no claimable work — sleeping ${backoff}s"
      sleep "$backoff" & child=$!; wait "$child" 2>/dev/null || true; child=0
      backoff=$(( backoff * 2 )); [[ "$backoff" -gt "$MAX_SLEEP" ]] && backoff="$MAX_SLEEP"
    else
      [[ "$rc" -ne 0 ]] && echo "$tag pass exited rc=$rc — retrying after ${BUSY_SLEEP}s"
      backoff="$IDLE_SLEEP"   # reset backoff after a productive/normal pass
      sleep "$BUSY_SLEEP" & child=$!; wait "$child" 2>/dev/null || true; child=0
    fi
  done
  echo "$tag stopped"
}

# Parent: launch N shard supervisors and forward Ctrl+C / SIGTERM to all of them.
shard_pids=()
parent_stop() {
  echo "[start.sh] stopping ${#shard_pids[@]} shard supervisor(s)…"
  for p in "${shard_pids[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
}
trap parent_stop INT TERM

echo "[start.sh] supervising $NPROC distributed shard(s) — Ctrl+C to stop"
for (( i=0; i<NPROC; i++ )); do
  supervise_shard "$i" "$NPROC" "$@" &
  shard_pids+=("$!")
done

# Block until every shard supervisor has exited. A trapped Ctrl+C interrupts
# `wait` and runs parent_stop (which SIGTERMs the shards); we then reap each.
for p in "${shard_pids[@]}"; do
  while kill -0 "$p" 2>/dev/null; do
    wait "$p" 2>/dev/null && break || true
  done
done

echo "[start.sh] all shard supervisors stopped"
exit 0
