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

# 5. Hand off to the orchestrator. Use the console script so the venv's
#    entry point is what actually runs.
exec decomp-agents "$@"
