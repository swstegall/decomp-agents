"""Imports-and-shape smoke tests — no SDK call, no DB writes."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def test_imports():
    from decomp_agents import config, hooks, merge, orchestrator, prompts, worker, work_queue, worktree

    assert config.SUPPORTED_BINARIES
    assert hasattr(worker, "main")
    assert hasattr(orchestrator, "main")


def test_function_branch_naming():
    from decomp_agents.work_queue import Function

    fn = Function(
        binary="ffxivlogin",
        rva=0x004A1230,
        end=0x004A12A0,
        size=0x70,
        module="net/blowfish",
        symbol="Blowfish::Init",
        type="matching",
        status="unmatched",
        section=".text",
    )
    assert fn.rva_hex == "0x004a1230"
    assert fn.safe_branch_name == "ffxivlogin/0x004a1230_Blowfish__Init"


def test_coordination_schema_applies_cleanly():
    schema = (
        Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    ).read_text()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        assert "sessions" in tables
        assert "workers" in tables
        assert "claims" in tables
        assert "tool_events" in tables
        assert "merges" in tables
        conn.close()
    finally:
        path.unlink(missing_ok=True)


def test_config_rejects_bad_repo(monkeypatch):
    from decomp_agents import config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DECOMP_REPO", "/nonexistent")
    with pytest.raises(RuntimeError, match="not a git repository"):
        config.load_config()


def test_config_auth_mode_subscription_drops_api_key(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shouldnotleak")
    monkeypatch.setenv("DECOMP_AUTH_MODE", "subscription")
    cfg = config.load_config()
    assert cfg.auth_mode == "subscription"
    assert cfg.anthropic_api_key == ""
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_config_auth_mode_api_key_requires_key(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DECOMP_AUTH_MODE", "api_key")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is unset"):
        config.load_config()


def test_config_auth_mode_autodetect(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.delenv("DECOMP_AUTH_MODE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.load_config().auth_mode == "subscription"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    assert config.load_config().auth_mode == "api_key"


def test_config_autopush_default_and_validation(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.delenv("DECOMP_AUTOPUSH", raising=False)
    assert config.load_config().autopush == "branches+master"

    monkeypatch.setenv("DECOMP_AUTOPUSH", "branches")
    assert config.load_config().autopush == "branches"

    monkeypatch.setenv("DECOMP_AUTOPUSH", "none")
    assert config.load_config().autopush == "none"

    monkeypatch.setenv("DECOMP_AUTOPUSH", "bogus")
    with pytest.raises(RuntimeError, match="DECOMP_AUTOPUSH"):
        config.load_config()


def test_merge_uses_staging_branch_constant():
    from decomp_agents import merge

    assert merge.STAGING_BRANCH == "_merge/staging"
    assert "/" in merge.STAGING_BRANCH  # nested ref name, can't collide with `master`


def test_worker_model_aliases(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")

    monkeypatch.delenv("DECOMP_WORKER_MODEL", raising=False)
    # Default tier is the middle ("default") slot. Sonnet now, was opus
    # before tiering landed — see the model-tiering block in
    # .env.example for the rationale.
    assert config.load_config().worker_model == "claude-sonnet-4-6"

    monkeypatch.setenv("DECOMP_WORKER_MODEL", "sonnet")
    assert config.load_config().worker_model == "claude-sonnet-4-6"

    monkeypatch.setenv("DECOMP_WORKER_MODEL", "haiku")
    assert config.load_config().worker_model.startswith("claude-haiku-")

    monkeypatch.setenv("DECOMP_WORKER_MODEL", "opus")
    assert config.load_config().worker_model == "claude-opus-4-7"

    monkeypatch.setenv("DECOMP_WORKER_MODEL", "claude-opus-4-7-something")
    assert config.load_config().worker_model == "claude-opus-4-7-something"

    monkeypatch.setenv("DECOMP_WORKER_MODEL", "bogus")
    with pytest.raises(RuntimeError, match="DECOMP_WORKER_MODEL"):
        config.load_config()


def test_tier_model_aliases_and_defaults(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")

    # Defaults: haiku / sonnet / opus.
    for var in ("DECOMP_TRIAGE_MODEL", "DECOMP_WORKER_MODEL", "DECOMP_ESCALATION_MODEL"):
        monkeypatch.delenv(var, raising=False)
    cfg = config.load_config()
    assert cfg.triage_model.startswith("claude-haiku-")
    assert cfg.worker_model == "claude-sonnet-4-6"
    assert cfg.escalation_model == "claude-opus-4-7"

    # Per-tier env vars resolve aliases independently.
    monkeypatch.setenv("DECOMP_TRIAGE_MODEL", "sonnet")
    monkeypatch.setenv("DECOMP_ESCALATION_MODEL", "claude-opus-4-7-edge")
    cfg = config.load_config()
    assert cfg.triage_model == "claude-sonnet-4-6"
    assert cfg.escalation_model == "claude-opus-4-7-edge"

    # model_for_tier dispatch.
    assert cfg.model_for_tier("triage") == cfg.triage_model
    assert cfg.model_for_tier("default") == cfg.worker_model
    assert cfg.model_for_tier("escalation") == cfg.escalation_model
    # Unknown tier falls back to default rather than raising — a bad
    # classifier should not crash a worker mid-claim.
    assert cfg.model_for_tier("garbage") == cfg.worker_model

    # Error message points at the correct env var.
    monkeypatch.setenv("DECOMP_TRIAGE_MODEL", "bogus")
    with pytest.raises(RuntimeError, match="DECOMP_TRIAGE_MODEL"):
        config.load_config()


def test_classify_tier_branches():
    from decomp_agents.prompt_context import (
        ESCALATION_MIN_SIZE,
        PromptContext,
        TRIAGE_MAX_SIZE,
        classify_tier,
    )
    from decomp_agents.work_queue import Function

    def make_fn(size: int) -> Function:
        return Function(
            binary="ffxivlogin",
            rva=0x00400000,
            end=0x00400000 + size,
            size=size,
            module="net/blowfish",
            symbol="FUN_00400000",
            type="matching",
            status="unmatched",
            section=".text",
        )

    def make_ctx(
        *,
        size: int,
        siblings: list[str] = (),  # noqa: B006 — read-only default
        has_named: bool = False,
        pseudo_c: bool = False,
        blocked: bool = False,
    ) -> PromptContext:
        return PromptContext(
            asm_relpath="asm/ffxivlogin/x.s",
            pseudo_c_relpath="build/ghidra-decomp/ffxivlogin/x.c" if pseudo_c else None,
            rtti_class=None,
            rtti_vtable_rva_hex=None,
            sibling_paths=list(siblings),
            has_named_sibling=has_named,
            blocked_post_mortem_path=("decomp-notes/blocked/x.md" if blocked else None),
        )

    # Easy: small + named sibling + Ghidra pseudo-C → triage.
    fn = make_fn(TRIAGE_MAX_SIZE)
    ctx = make_ctx(
        size=TRIAGE_MAX_SIZE,
        siblings=["src/ffxivlogin/_rosetta/Blowfish__Init.cpp"],
        has_named=True,
        pseudo_c=True,
    )
    assert classify_tier(fn, ctx) == "triage"

    # Just-over-triage size → default (still has context, just bigger).
    fn = make_fn(TRIAGE_MAX_SIZE + 1)
    ctx = make_ctx(
        size=TRIAGE_MAX_SIZE + 1,
        siblings=["src/ffxivlogin/_rosetta/Blowfish__Init.cpp"],
        has_named=True,
        pseudo_c=True,
    )
    assert classify_tier(fn, ctx) == "default"

    # Big body → escalation.
    fn = make_fn(ESCALATION_MIN_SIZE + 1)
    ctx = make_ctx(size=ESCALATION_MIN_SIZE + 1, pseudo_c=True)
    assert classify_tier(fn, ctx) == "escalation"

    # No siblings AND no pseudo-C → escalation regardless of size.
    fn = make_fn(0x40)
    ctx = make_ctx(size=0x40)
    assert classify_tier(fn, ctx) == "escalation"

    # Prior bail on disk → escalation, even if otherwise easy.
    fn = make_fn(TRIAGE_MAX_SIZE)
    ctx = make_ctx(
        size=TRIAGE_MAX_SIZE,
        siblings=["src/ffxivlogin/_rosetta/Blowfish__Init.cpp"],
        has_named=True,
        pseudo_c=True,
        blocked=True,
    )
    assert classify_tier(fn, ctx) == "escalation"

    # Only FUN_-prefixed sibling (auto-passthrough) → no named-sibling
    # signal, so even an otherwise-easy fn falls to default.
    fn = make_fn(TRIAGE_MAX_SIZE)
    ctx = make_ctx(
        size=TRIAGE_MAX_SIZE,
        siblings=["src/ffxivlogin/_rosetta/FUN_004288ae.cpp"],
        has_named=False,
        pseudo_c=True,
    )
    assert classify_tier(fn, ctx) == "default"


def test_diff_compatible_regex():
    from decomp_agents.work_queue import DIFF_COMPATIBLE_SYMBOL_RE

    assert DIFF_COMPATIBLE_SYMBOL_RE.match("FUN_004288ae")
    assert DIFF_COMPATIBLE_SYMBOL_RE.match("FUN_00440a90")
    # Named symbols rejected (compare.py limitation today)
    assert not DIFF_COMPATIBLE_SYMBOL_RE.match("Blowfish::Init")
    # Thunks rejected
    assert not DIFF_COMPATIBLE_SYMBOL_RE.match("thunk_FUN_00440a90")
    # Mixed case not accepted (RVAs are always lowercase hex)
    assert not DIFF_COMPATIBLE_SYMBOL_RE.match("FUN_004288AE")


def test_naked_stub_filter():
    from decomp_agents.work_queue import Function, _is_likely_naked_stub

    stub = Function(
        binary="ffxivgame",
        rva=0x401010,
        end=0x401016,
        size=0x6,
        module="_unknown",
        symbol="FUN_00401010",
        type="matching",
        status="unmatched",
        section=".text",
    )
    named_small = Function(
        binary="ffxivgame",
        rva=0x4A1230,
        end=0x4A1236,
        size=0x6,
        module="net/blowfish",
        symbol="Blowfish::Init",
        type="matching",
        status="unmatched",
        section=".text",
    )
    big_unknown = Function(
        binary="ffxivgame",
        rva=0x500000,
        end=0x500200,
        size=0x200,
        module="_unknown",
        symbol="FUN_00500000",
        type="matching",
        status="unmatched",
        section=".text",
    )
    assert _is_likely_naked_stub(stub)
    assert not _is_likely_naked_stub(named_small)  # named module
    assert not _is_likely_naked_stub(big_unknown)  # too big


def test_int_env_supports_hex(monkeypatch):
    from decomp_agents.config import _int_env

    monkeypatch.setenv("FOO_HEX", "0x200")
    assert _int_env("FOO_HEX", 0) == 0x200
    monkeypatch.setenv("FOO_DEC", "256")
    assert _int_env("FOO_DEC", 0) == 256


def test_yaml_only_conflict_detection():
    from decomp_agents.merge import _yaml_only_conflicts

    assert _yaml_only_conflicts([Path("config/ffxivlogin.yaml")])
    assert _yaml_only_conflicts(
        [Path("config/ffxivlogin.yaml"), Path("config/ffxivgame.yaml")]
    )
    assert not _yaml_only_conflicts([Path("src/ffxivlogin/foo.cpp")])
    assert not _yaml_only_conflicts(
        [Path("config/ffxivlogin.yaml"), Path("src/ffxivlogin/foo.cpp")]
    )
    assert not _yaml_only_conflicts([])


def test_config_post_merge_stamp_default_and_toggle(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")

    monkeypatch.delenv("DECOMP_POST_MERGE_STAMP", raising=False)
    assert config.load_config().post_merge_stamp is True

    monkeypatch.setenv("DECOMP_POST_MERGE_STAMP", "0")
    assert config.load_config().post_merge_stamp is False

    monkeypatch.setenv("DECOMP_POST_MERGE_STAMP", "off")
    assert config.load_config().post_merge_stamp is False

    monkeypatch.setenv("DECOMP_POST_MERGE_STAMP", "1")
    assert config.load_config().post_merge_stamp is True


def test_try_stamp_cluster_skips_when_clusters_missing(tmp_path):
    """Fast path: no clusters_reloc.json → returns a stamp-skip note, never touches the worktree."""
    from decomp_agents.merge import _try_stamp_cluster

    repo = tmp_path / "repo"
    repo.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()

    notes = _try_stamp_cluster(
        repo=repo,
        staging_wt=staging,
        binary="ffxivlogin",
        matched_rva=0x0000a1b0,
        matched_symbol="FUN_0040a1b0",
    )
    assert len(notes) == 1
    assert "stamp-skip" in notes[0]
    assert "clusters_reloc.json missing" in notes[0]
    # No directories should have been created in staging.
    assert not (staging / "build").exists()


def test_try_stamp_cluster_singleton_silent_skip(tmp_path):
    """Matched RVA that lives in a single-member cluster → empty notes, no work."""
    import json
    from decomp_agents.merge import _try_stamp_cluster

    repo = tmp_path / "repo"
    (repo / "build" / "easy_wins").mkdir(parents=True)
    clusters_path = repo / "build" / "easy_wins" / "ffxivlogin.clusters_reloc.json"
    clusters_path.write_text(
        json.dumps({"shape_a": [{"rva": 0x0000a1b0}]})  # singleton cluster
    )
    staging = tmp_path / "staging"
    staging.mkdir()

    notes = _try_stamp_cluster(
        repo=repo,
        staging_wt=staging,
        binary="ffxivlogin",
        matched_rva=0x0000a1b0,
        matched_symbol="FUN_0040a1b0",
    )
    assert notes == []  # silent fast path — singleton matches are common


def test_try_stamp_cluster_all_siblings_already_stamped(tmp_path):
    """Multi-member cluster but every sibling .cpp already exists → skip-existing note."""
    import json
    from decomp_agents.merge import _try_stamp_cluster

    repo = tmp_path / "repo"
    (repo / "build" / "easy_wins").mkdir(parents=True)
    clusters_path = repo / "build" / "easy_wins" / "ffxivlogin.clusters_reloc.json"
    clusters_path.write_text(
        json.dumps(
            {
                "shape_a": [
                    {"rva": 0x0000a1b0},  # matched
                    {"rva": 0x0000a2c0},  # sibling
                    {"rva": 0x0000a3d0},  # sibling
                ]
            }
        )
    )
    staging = tmp_path / "staging"
    rosetta_dir = staging / "src" / "ffxivlogin" / "_rosetta"
    rosetta_dir.mkdir(parents=True)
    # Pre-create both sibling .cpp's so the pre-flight finds nothing to do.
    (rosetta_dir / "FUN_0040a2c0.cpp").write_text("// already stamped")
    (rosetta_dir / "FUN_0040a3d0.cpp").write_text("// already stamped")

    notes = _try_stamp_cluster(
        repo=repo,
        staging_wt=staging,
        binary="ffxivlogin",
        matched_rva=0x0000a1b0,
        matched_symbol="FUN_0040a1b0",
    )
    assert len(notes) == 1
    assert "already stamped" in notes[0]


def test_merge_one_accepts_post_merge_stamp_kwarg():
    """The new kwarg must be optional + default False so existing callers don't break."""
    import inspect
    from decomp_agents.merge import merge_one, run_merge_pass

    sig = inspect.signature(merge_one)
    assert "post_merge_stamp" in sig.parameters
    assert sig.parameters["post_merge_stamp"].default is False

    sig = inspect.signature(run_merge_pass)
    assert "post_merge_stamp" in sig.parameters
    assert sig.parameters["post_merge_stamp"].default is False


def test_classify_conflicts_known_categories():
    """Every supported path category maps to its strategy; everything else escalates."""
    from decomp_agents.merge import classify_conflicts

    plan = classify_conflicts([
        Path("config/ffxivgame.yaml"),
        Path("src/ffxivgame/_rosetta/FUN_00401030.cpp"),
        Path("decomp-notes/blocked/ffxivgame/00001460_FUN_00401460.md"),
        Path("decomp-notes/types/ffxivgame/00001460.md"),
        Path("include/ffxivgame/net/gam_registry.h"),  # not auto-resolvable
        Path("decomp-notes/idioms/ffxivgame.md"),  # curator-only → escalate
    ])

    strategies_by_path = {str(p): s for p, s in plan.strategies}
    assert strategies_by_path["config/ffxivgame.yaml"] == "theirs"
    assert (
        strategies_by_path["src/ffxivgame/_rosetta/FUN_00401030.cpp"]
        == "prefer-handwritten"
    )
    assert (
        strategies_by_path["decomp-notes/blocked/ffxivgame/00001460_FUN_00401460.md"]
        == "append-md"
    )
    assert strategies_by_path["decomp-notes/types/ffxivgame/00001460.md"] == "theirs"

    escalated = {str(p) for p, _ in plan.escalate}
    assert "include/ffxivgame/net/gam_registry.h" in escalated
    assert "decomp-notes/idioms/ffxivgame.md" in escalated


def test_classify_conflicts_yaml_only_still_resolvable():
    """Pure YAML conflicts (the legacy case) end up with no escalations."""
    from decomp_agents.merge import classify_conflicts

    plan = classify_conflicts([Path("config/ffxivlogin.yaml")])
    assert plan.escalate == []
    assert plan.strategies == [(Path("config/ffxivlogin.yaml"), "theirs")]


def test_is_stamped_marker_detection():
    """The first-line marker `// [STAMPED]` is the discriminator."""
    from decomp_agents.merge import _is_stamped

    stamped = "// [STAMPED] from FUN_00401030.cpp\n// SPDX-..."
    handwritten = "// meteor-decomp — clean-room decompilation...\n// SPDX-..."
    assert _is_stamped(stamped)
    assert not _is_stamped(handwritten)
    assert not _is_stamped(None)
    assert not _is_stamped("")


def test_pending_merges_cross_session_drains_prior(tmp_path):
    """With cross_session=True, prior-session pending merges come back into play."""
    from decomp_agents.work_queue import WorkQueue

    schema = (
        Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    )
    db = tmp_path / "coord.sqlite"
    queue = WorkQueue(db, schema)

    prior_session = queue.begin_session(
        worker_count=1,
        binary="ffxivgame",
        repo_path=tmp_path,
        notes="prior",
    )
    queue.end_session(prior_session)
    # Insert a stranded matched-but-unmerged claim from the prior session.
    with queue._conn() as conn:  # noqa: SLF001
        worker_id = conn.execute(
            "INSERT INTO workers(session_id, agent_id, worktree_path, branch_prefix, "
            "pid, status, started_at) VALUES (?, 0, ?, ?, NULL, 'idle', ?) RETURNING id",
            (prior_session, str(tmp_path / "wt"), "agents/agent-0", "2026-05-19T00:00:00+00:00"),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO claims("
            "  session_id, worker_id, binary, rva, symbol, module, size_bytes, "
            "  claimed_at, released_at, outcome, branch, merge_status"
            ") VALUES (?, ?, 'ffxivgame', 0x9999, 'FUN_00409999', 'mod', 0x40, "
            "?, ?, 'matched', 'agents/agent-0/ffxivgame/0x00009999_FUN_00409999', 'pending')",
            (prior_session, worker_id, "2026-05-19T00:00:00+00:00", "2026-05-19T00:00:10+00:00"),
        )

    # Restart with a fresh session.
    current_session = queue.begin_session(
        worker_count=1,
        binary="ffxivgame",
        repo_path=tmp_path,
        notes="current",
    )

    # Without cross_session, the stranded claim stays invisible.
    assert queue.pending_merges(current_session) == []

    # With cross_session=True, the prior claim surfaces.
    cross = queue.pending_merges(current_session, cross_session=True)
    assert len(cross) == 1
    assert cross[0].function.rva == 0x9999


def test_reset_escalated_to_pending_round_trip(tmp_path):
    """reset_escalated_to_pending flips merge_status='conflict' rows back to 'pending'."""
    from decomp_agents.merge import reset_escalated_to_pending
    from decomp_agents.work_queue import WorkQueue

    schema = (
        Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    )
    db = tmp_path / "coord.sqlite"
    queue = WorkQueue(db, schema)

    sid = queue.begin_session(
        worker_count=1, binary="ffxivgame", repo_path=tmp_path
    )
    with queue._conn() as conn:  # noqa: SLF001
        worker_id = conn.execute(
            "INSERT INTO workers(session_id, agent_id, worktree_path, branch_prefix, "
            "pid, status, started_at) VALUES (?, 0, ?, ?, NULL, 'idle', ?) RETURNING id",
            (sid, str(tmp_path / "wt"), "agents/agent-0", "2026-05-19T00:00:00+00:00"),
        ).fetchone()["id"]
        for rva in (0x1000, 0x1460, 0x1750):
            conn.execute(
                "INSERT INTO claims("
                "  session_id, worker_id, binary, rva, symbol, module, size_bytes, "
                "  claimed_at, released_at, outcome, branch, merge_status"
                ") VALUES (?, ?, 'ffxivgame', ?, ?, 'mod', 0x80, "
                "?, ?, 'matched', ?, 'conflict')",
                (
                    sid,
                    worker_id,
                    rva,
                    f"FUN_{rva + 0x400000:08x}",
                    "2026-05-19T00:00:00+00:00",
                    "2026-05-19T00:00:10+00:00",
                    f"agents/agent-0/ffxivgame/0x{rva:08x}",
                ),
            )

    flipped = reset_escalated_to_pending(queue, cross_session=True)
    assert len(flipped) == 3
    # And the DB now sees them as pending merges.
    pending = queue.pending_merges(sid, cross_session=True)
    assert {c.function.rva for c in pending} == {0x1000, 0x1460, 0x1750}


def test_retry_escalated_cli_module_imports():
    """The retry CLI module must import + expose a click main."""
    from decomp_agents import retry_escalated

    assert callable(retry_escalated.main)


def test_config_cross_session_merge_default_and_toggle(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")

    monkeypatch.delenv("DECOMP_CROSS_SESSION_MERGE", raising=False)
    assert config.load_config().cross_session_merge is True

    monkeypatch.setenv("DECOMP_CROSS_SESSION_MERGE", "0")
    assert config.load_config().cross_session_merge is False


def _fn(binary: str, rva: int) -> "Function":  # noqa: F821
    from decomp_agents.work_queue import Function

    return Function(
        binary=binary,
        rva=rva,
        end=rva + 0x40,
        size=0x40,
        module="mod",
        symbol=f"FUN_{rva + 0x400000:08x}",
        type="matching",
        status="unmatched",
        section=".text",
    )


def _seed_session_and_worker(queue, binary: str, tmp_path: Path) -> tuple[int, int]:
    sid = queue.begin_session(worker_count=1, binary=binary, repo_path=tmp_path)
    with queue._conn() as conn:  # noqa: SLF001
        worker_id = conn.execute(
            "INSERT INTO workers(session_id, agent_id, worktree_path, branch_prefix, "
            "pid, status, started_at) VALUES (?, 0, ?, ?, NULL, 'idle', ?) RETURNING id",
            (sid, str(tmp_path / "wt"), "agents/agent-0", "2026-05-19T00:00:00+00:00"),
        ).fetchone()["id"]
    return sid, worker_id


def test_claim_next_excludes_historically_matched_rva(tmp_path):
    """A matched + released claim must not be re-handed-out on the next claim_next.

    Regression for the 88% duplicate-claim leak (2026-05-21): the YAML
    `status` field is gitignored across worktrees and therefore can't be
    the dedupe source. Without this check, a successful match on a
    singleton (which never makes it into validate_results.json + never
    triggers update_yaml_status.py) would stay `unmatched` in YAML
    forever and the next worker would re-claim it.
    """
    from decomp_agents.work_queue import WorkQueue

    schema = Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    queue = WorkQueue(tmp_path / "coord.sqlite", schema)
    sid, worker_id = _seed_session_and_worker(queue, "ffxivgame", tmp_path)

    fn = _fn("ffxivgame", 0x14B0)

    first = queue.claim_next(
        session_id=sid,
        worker_id=worker_id,
        candidates=[fn],
        branch_prefix="agents/agent-0",
    )
    assert first is not None
    queue.release(first.id, outcome="matched", iterations=4, merge_status="merged")

    # The yaml row is still status='unmatched' (gitignored, lives only in
    # the worker's worktree). filter_eligible would still hand us `fn` as
    # a candidate. SQLite must be the one to refuse it.
    second = queue.claim_next(
        session_id=sid,
        worker_id=worker_id,
        candidates=[fn],
        branch_prefix="agents/agent-0",
    )
    assert second is None, "matched RVA should not be re-claimable"


def test_claim_next_excludes_matched_even_when_merge_conflicted(tmp_path):
    """`merge_status='conflict'` is still a successful match.

    The auto-resolver gave up on landing the branch, but the match work
    itself is done — the branch exists on disk. Re-matching just produces
    another conflicting branch. retry_escalated.py is the correct path
    for a human to flip it back to `pending` for re-merge attempts.
    """
    from decomp_agents.work_queue import WorkQueue

    schema = Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    queue = WorkQueue(tmp_path / "coord.sqlite", schema)
    sid, worker_id = _seed_session_and_worker(queue, "ffxivgame", tmp_path)

    fn = _fn("ffxivgame", 0x1460)

    first = queue.claim_next(
        session_id=sid,
        worker_id=worker_id,
        candidates=[fn],
        branch_prefix="agents/agent-0",
    )
    assert first is not None
    queue.release(first.id, outcome="matched", iterations=6, merge_status="conflict")

    second = queue.claim_next(
        session_id=sid,
        worker_id=worker_id,
        candidates=[fn],
        branch_prefix="agents/agent-0",
    )
    assert second is None


def test_claim_next_re_eligible_after_non_success_outcomes(tmp_path):
    """`error` / `abandoned` / `blocked` outcomes must remain re-claimable.

    These represent "this attempt didn't succeed" (rate-limit, SDK crash,
    worker timeout, worker gave up after N iterations) — a future worker
    might succeed. Historical data: RVA 0x14b0 was blocked 6× before
    being matched 23× by different workers/sessions.
    """
    from decomp_agents.work_queue import WorkQueue

    schema = Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    queue = WorkQueue(tmp_path / "coord.sqlite", schema)
    sid, worker_id = _seed_session_and_worker(queue, "ffxivgame", tmp_path)

    fn = _fn("ffxivgame", 0x1650)

    for outcome in ("error", "abandoned", "blocked"):
        claim = queue.claim_next(
            session_id=sid,
            worker_id=worker_id,
            candidates=[fn],
            branch_prefix="agents/agent-0",
        )
        assert claim is not None, f"expected re-claim after outcome={outcome}"
        queue.release(claim.id, outcome=outcome, iterations=1)
