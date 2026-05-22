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
# (e.g. "claude-opus-4-7") and it'll be passed through unchanged.
WORKER_MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _resolve_worker_model(raw: str) -> str:
    raw_lower = raw.lower()
    if raw_lower in WORKER_MODEL_ALIASES:
        return WORKER_MODEL_ALIASES[raw_lower]
    # Allow pass-through of full model ids so the user isn't locked
    # into the alias list when a new family ships.
    if raw.startswith("claude-"):
        return raw
    raise RuntimeError(
        f"DECOMP_WORKER_MODEL={raw!r} must be one of {sorted(WORKER_MODEL_ALIASES)} "
        f"or a full 'claude-...' model id"
    )


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
    worker_model: str  # "opus" | "sonnet" | "haiku" | full model id
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

    worker_model_raw = os.environ.get("DECOMP_WORKER_MODEL", "opus").strip()
    worker_model = _resolve_worker_model(worker_model_raw)

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
        worker_model=worker_model,
        post_merge_stamp=_bool_env("DECOMP_POST_MERGE_STAMP", True),
        cross_session_merge=_bool_env("DECOMP_CROSS_SESSION_MERGE", True),
    )
