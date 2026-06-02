"""Env-var-driven configuration. One source of truth for orchestrator + workers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_BINARIES = (
    "ffxivgame",
    "ffxivboot",
    "ffxivlogin",
    "ffxivconfig",
    "ffxivupdater",
)

# Map convenience aliases to current model ids. Update when a new
# Claude family ships. Users can also pass a full model id verbatim
# (e.g. "claude-opus-4-8") and it'll be passed through unchanged.
WORKER_MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

TIER_NAMES = ("triage", "default", "escalation")


def _resolve_model(raw: str, *, env_name: str) -> str:
    raw_lower = raw.lower()
    if raw_lower in WORKER_MODEL_ALIASES:
        return WORKER_MODEL_ALIASES[raw_lower]
    # Allow pass-through of full model ids so the user isn't locked
    # into the alias list when a new family ships.
    if raw.startswith("claude-"):
        return raw
    raise RuntimeError(
        f"{env_name}={raw!r} must be one of {sorted(WORKER_MODEL_ALIASES)} "
        f"or a full 'claude-...' model id"
    )


def _resolve_worker_model(raw: str) -> str:
    # Kept for backwards compatibility with anything that imports it
    # directly. New code should use _resolve_model with an explicit
    # env_name so the error message points at the right variable.
    return _resolve_model(raw, env_name="DECOMP_WORKER_MODEL")


@dataclass(frozen=True)
class Config:
    repo: Path
    binary: str
    worker_count: int
    worktree_root: Path
    output_dir: Path
    coordination_db: Path
    max_attempts_per_worker: int
    max_iterations_per_function: int
    max_function_size: int
    live_tail: bool
    # Either an explicit API key (pay-as-you-go) or empty string meaning
    # "fall back to subscription OAuth from macOS Keychain via `claude login`".
    # The SDK auto-detects when ANTHROPIC_API_KEY is unset.
    anthropic_api_key: str
    auth_mode: str  # "api_key" | "subscription"
    autopush: str   # "none" | "branches" | "branches+master"
    # Per-tier models. classify_tier() in prompt_context picks one of
    # {triage, default, escalation} per function based on size + sibling
    # availability + prior bail history; model_for_tier() maps that to
    # a concrete model id. To disable tiering, set all three to the same
    # value.
    triage_model: str       # easy: small + named sibling + Ghidra pseudo-C
    worker_model: str       # default tier — the "middle" model
    escalation_model: str   # hard: large, no siblings, or prior bail
    # Post-merge cluster stamping: after a worker's match merges cleanly,
    # check meteor-decomp's reloc-aware cluster JSON for sibling .cpp's
    # that could be byte-identical, stamp them, validate via
    # compile-once-share-many, flip YAML rows, and fold the result into
    # the same merge advance. No-op when the matched function is a
    # cluster singleton or `build/easy_wins/<bin>.clusters_reloc.json`
    # is missing (i.e. `make stamp-reloc` was never run).
    post_merge_stamp: bool
    # Cross-session merge drain: also process matched claims from
    # prior sessions whose merge_status is still 'pending'. Each prior
    # orchestrator may have exited before its final merge pass landed
    # 100 % of work; this picks up the strays so the queue actually
    # drains.
    cross_session_merge: bool
    # --- distributed mode (issue #11, D5) --------------------------------
    # "local" (default) is the original N-worktree + SQLite-queue + local
    # merge-loop topology. "distributed" switches to the fork-based
    # GitHub-native flow: provision a fork of meteor-decomp, claim VAs via
    # the upstream `claims` branch (driven by an issue-comment `/claim`),
    # run the SAME per-function match loop + GREEN gate in the fork, then
    # open a PR upstream. Backward-compatible: nothing below is read in
    # local mode.
    mode: str  # "local" | "distributed"
    # The canonical upstream repo (owner/repo) the agent contributes to.
    upstream: str
    # The contributor's FORK of the upstream: either "owner/repo" or a
    # full clone URL. Empty => `gh repo fork` will create/derive one under
    # the agent's own gh identity.
    fork: str
    # The coordination ISSUE number on the upstream whose comments fire
    # claim.yml. The agent posts `/claim FUN_<va> <binary>` there.
    claim_issue: int
    # The branch on upstream that PRs target (D1 ledger model uses
    # `develop`). Also the branch the fork tracks for the solved set.
    upstream_branch: str
    # Where the fork working clone is checked out for distributed mode.
    fork_root: Path
    # Bounded poll for a claim win against the upstream `claims` branch:
    # how long to wait, and the per-poll sleep (seconds). The upstream
    # claim.yml CAS takes a few seconds to land the ledger commit.
    claim_poll_timeout_s: int
    claim_poll_interval_s: int
    # Path to a LOCAL meteor-decomp checkout that already holds the user's
    # copyright-derived toolchain artifacts — `orig/<bin>.exe` (the SE game
    # binary) and `config/<bin>.symbols.json` (the Ghidra dump derived from
    # it). Both are gitignored in every meteor-decomp clone, so a fresh fork
    # clone never carries them, yet `make diff` (the GREEN grader) needs
    # both. Distributed mode SYMLINKS them from here into the fork clone so
    # grading works; they are NEVER copied, committed, or distributed (we
    # cannot ship copyrighted material — the user supplies their own).
    # Defaults to `repo` when DECOMP_ARTIFACTS_DIR is unset AND that path
    # already has an `orig/` dir (the common single-operator case where the
    # local-mode checkout already holds the artifacts); else None. Local
    # mode ignores this entirely.
    artifacts_dir: Path | None
    # Distributed-mode horizontal sharding. To run N independent agent
    # processes for throughput (each its own fork clone + output dir), each
    # process claims ONLY the VAs in its residue class — `rva % shard_count ==
    # shard_index` — so two of OUR OWN same-identity processes never attempt
    # the same VA (the upstream claim ledger only dedups across DIFFERENT gh
    # logins, and treats a same-owner re-claim as a lease extension). The
    # ledger still coordinates against other contributors. Defaults 0/1 mean
    # "no sharding" (single process), so existing runs are unchanged. Read
    # only in distributed mode.
    shard_index: int
    shard_count: int

    @property
    def yaml_path(self) -> Path:
        return self.repo / "config" / f"{self.binary}.yaml"

    @property
    def transcripts_dir(self) -> Path:
        return self.output_dir / "transcripts"

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def claims_dir(self) -> Path:
        return self.output_dir / "claims"

    @property
    def merges_dir(self) -> Path:
        return self.output_dir / "merges"

    def worktree_for(self, agent_id: int) -> Path:
        return self.worktree_root / f"agent-{agent_id}"

    def branch_prefix_for(self, agent_id: int) -> str:
        return f"agents/agent-{agent_id}"

    def model_for_tier(self, tier: str) -> str:
        if tier == "triage":
            return self.triage_model
        if tier == "escalation":
            return self.escalation_model
        # "default" — and a safety net for anything unrecognised so a
        # bad classifier never crashes a worker mid-claim.
        return self.worker_model


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    raw = raw.strip()
    base = 16 if raw.lower().startswith("0x") else 10
    return int(raw, base)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _here() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE .env file. Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> Config:
    project_root = _here()
    _load_dotenv(project_root / ".env")

    repo_env = os.environ.get("DECOMP_REPO")
    repo = (
        Path(repo_env).resolve()
        if repo_env
        else (project_root.parent / "meteor-decomp").resolve()
    )
    if not (repo / ".git").exists():
        raise RuntimeError(
            f"DECOMP_REPO {repo} is not a git repository — point this at "
            f"the meteor-decomp checkout."
        )

    binary = os.environ.get("DECOMP_BINARY", "ffxivlogin").strip()
    if binary not in SUPPORTED_BINARIES:
        raise RuntimeError(
            f"DECOMP_BINARY={binary!r} is not one of {SUPPORTED_BINARIES}"
        )

    worker_count = _int_env("DECOMP_AGENT_WORKERS", 5)
    if not (1 <= worker_count <= 8):
        raise RuntimeError(
            f"DECOMP_AGENT_WORKERS={worker_count} out of range — pick 1..8"
        )

    worktree_root_env = os.environ.get("DECOMP_WORKTREE_ROOT")
    worktree_root = (
        Path(worktree_root_env).resolve()
        if worktree_root_env
        else (repo / ".worktrees").resolve()
    )

    output_dir_env = os.environ.get("DECOMP_OUTPUT_DIR")
    output_dir = (
        Path(output_dir_env).resolve()
        if output_dir_env
        else (project_root / "output").resolve()
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    forced_mode = os.environ.get("DECOMP_AUTH_MODE", "").strip().lower()
    if forced_mode and forced_mode not in ("api_key", "subscription"):
        raise RuntimeError(
            f"DECOMP_AUTH_MODE={forced_mode!r} must be 'api_key' or 'subscription'"
        )

    if forced_mode == "subscription":
        # User explicitly asked for subscription auth. Drop any stray
        # API key from the inherited env so it doesn't shadow OAuth.
        api_key = ""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        auth_mode = "subscription"
    elif forced_mode == "api_key":
        if not api_key:
            raise RuntimeError(
                "DECOMP_AUTH_MODE=api_key but ANTHROPIC_API_KEY is unset"
            )
        auth_mode = "api_key"
    else:
        # Auto-detect: API key wins if present, else assume subscription.
        # The SDK reads from macOS Keychain when ANTHROPIC_API_KEY is unset.
        auth_mode = "api_key" if api_key else "subscription"

    autopush = os.environ.get("DECOMP_AUTOPUSH", "branches+master").strip().lower()
    if autopush not in ("none", "branches", "branches+master"):
        raise RuntimeError(
            f"DECOMP_AUTOPUSH={autopush!r} must be 'none', 'branches', or 'branches+master'"
        )

    # Three-tier model selection. Defaults reflect the cost/quality
    # split documented in CLAUDE.md: haiku for the easy cases (small
    # function + named sibling + Ghidra pseudo-C), sonnet for the
    # middle of the pool, opus reserved for the hard cases (no
    # siblings, large, or a prior bail). Set all three equal to
    # disable tiering.
    worker_model_raw = os.environ.get("DECOMP_WORKER_MODEL", "sonnet").strip()
    worker_model = _resolve_model(worker_model_raw, env_name="DECOMP_WORKER_MODEL")

    triage_model_raw = os.environ.get("DECOMP_TRIAGE_MODEL", "haiku").strip()
    triage_model = _resolve_model(triage_model_raw, env_name="DECOMP_TRIAGE_MODEL")

    escalation_model_raw = os.environ.get("DECOMP_ESCALATION_MODEL", "opus").strip()
    escalation_model = _resolve_model(escalation_model_raw, env_name="DECOMP_ESCALATION_MODEL")

    # --- distributed mode (issue #11, D5) --------------------------------
    # Default is "local" so every existing invocation, test, and .env keeps
    # the original N-worktree topology untouched. Distributed mode is opt-in.
    mode = os.environ.get("DECOMP_MODE", "local").strip().lower()
    if mode not in ("local", "distributed"):
        raise RuntimeError(
            f"DECOMP_MODE={mode!r} must be 'local' or 'distributed'"
        )

    # No baked-in default: this repo is public, so we never hardcode a
    # personal GitHub handle. Distributed mode requires it explicitly (the
    # `if not upstream` guard below fires); local mode never reads it.
    upstream = os.environ.get("DECOMP_UPSTREAM", "").strip()
    fork = os.environ.get("DECOMP_FORK", "").strip()
    upstream_branch = os.environ.get("DECOMP_UPSTREAM_BRANCH", "develop").strip()

    fork_root_env = os.environ.get("DECOMP_FORK_ROOT")
    fork_root = (
        Path(fork_root_env).resolve()
        if fork_root_env
        else (output_dir / "fork").resolve()
    )

    claim_issue_raw = os.environ.get("DECOMP_CLAIM_ISSUE", "").strip()
    if mode == "distributed":
        # These are only load-bearing in distributed mode; validate them
        # there so local mode never trips on an unset coordination issue.
        if not upstream:
            raise RuntimeError(
                "DECOMP_MODE=distributed requires DECOMP_UPSTREAM=<owner/repo>"
            )
        if not claim_issue_raw:
            raise RuntimeError(
                "DECOMP_MODE=distributed requires DECOMP_CLAIM_ISSUE=<issue number> "
                "(the upstream coordination issue whose `/claim` comments fire claim.yml)"
            )
        try:
            claim_issue = int(claim_issue_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"DECOMP_CLAIM_ISSUE={claim_issue_raw!r} must be an integer issue number"
            ) from exc
    else:
        claim_issue = int(claim_issue_raw) if claim_issue_raw.isdigit() else 0

    # Toolchain-artifact source for distributed-mode grading. Explicit env
    # wins; otherwise fall back to the local `repo` checkout IFF it actually
    # carries the artifacts (`orig/` present) — this keeps the common
    # single-operator case zero-config while staying overridable. Never
    # synthesised in local mode (the worker reads orig/symbols straight from
    # `repo` there; nothing to symlink).
    artifacts_dir_env = os.environ.get("DECOMP_ARTIFACTS_DIR", "").strip()
    if artifacts_dir_env:
        artifacts_dir: Path | None = Path(artifacts_dir_env).resolve()
    elif (repo / "orig").is_dir():
        artifacts_dir = repo
    else:
        artifacts_dir = None

    # Distributed-mode sharding (0/1 = single process; harmless in local mode).
    shard_index = _int_env("DECOMP_SHARD_INDEX", 0)
    shard_count = _int_env("DECOMP_SHARD_COUNT", 1)
    if shard_count < 1:
        raise RuntimeError(f"DECOMP_SHARD_COUNT={shard_count} must be >= 1")
    if not (0 <= shard_index < shard_count):
        raise RuntimeError(
            f"DECOMP_SHARD_INDEX={shard_index} out of range for "
            f"DECOMP_SHARD_COUNT={shard_count} (need 0 <= index < count)"
        )

    return Config(
        repo=repo,
        binary=binary,
        worker_count=worker_count,
        worktree_root=worktree_root,
        output_dir=output_dir,
        coordination_db=output_dir / "coordination.sqlite",
        max_attempts_per_worker=_int_env("DECOMP_MAX_ATTEMPTS_PER_WORKER", 8),
        max_iterations_per_function=_int_env(
            "DECOMP_MAX_ITERATIONS_PER_FUNCTION", 12
        ),
        max_function_size=_int_env("DECOMP_MAX_FUNCTION_SIZE", 0x200),
        live_tail=_bool_env("DECOMP_LIVE_TAIL", True),
        anthropic_api_key=api_key,
        auth_mode=auth_mode,
        autopush=autopush,
        triage_model=triage_model,
        worker_model=worker_model,
        escalation_model=escalation_model,
        post_merge_stamp=_bool_env("DECOMP_POST_MERGE_STAMP", True),
        cross_session_merge=_bool_env("DECOMP_CROSS_SESSION_MERGE", True),
        mode=mode,
        upstream=upstream,
        fork=fork,
        claim_issue=claim_issue,
        upstream_branch=upstream_branch,
        fork_root=fork_root,
        claim_poll_timeout_s=_int_env("DECOMP_CLAIM_POLL_TIMEOUT_S", 180),
        claim_poll_interval_s=_int_env("DECOMP_CLAIM_POLL_INTERVAL_S", 6),
        artifacts_dir=artifacts_dir,
        shard_index=shard_index,
        shard_count=shard_count,
    )
