# decomp-agents — parallel autonomous Claude agents for the meteor-decomp matching workflow
# Copyright (C) 2026  Samuel Stegall
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for distributed mode (issue #11, D5).

Covers the toolchain-free, network-free parts:
  - pr_gate: one-file scope, AGPL header, VA parse, already-solved.
  - freeset: solved / claimed / open-PR exclusion logic with injected
    inputs + filesystem fixtures.
  - github_claim: ledger-win parsing against a fake claims dir.
  - config: DECOMP_MODE handling (default local, distributed validation).
  - pr: title/body builders.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# module imports + shape
# ---------------------------------------------------------------------------


def test_distributed_modules_import():
    from decomp_agents import (
        distributed_orchestrator,
        fork,
        freeset,
        github_claim,
        pr,
        pr_gate,
    )

    assert hasattr(fork, "provision_fork")
    assert hasattr(github_claim, "GitHubClaimBackend")
    assert hasattr(freeset, "eligible_functions")
    assert hasattr(pr_gate, "run_pr_gate")
    assert hasattr(pr, "create_pr")
    assert hasattr(distributed_orchestrator, "run_distributed")


def test_new_py_files_match_module_docstring_convention():
    """Every new distributed-mode .py opens with a module docstring, matching
    the existing decomp_agents modules (per-file AGPL headers are NOT used in
    this repo — licensing is covered by the top-level LICENSE file)."""
    import decomp_agents

    pkg_dir = Path(decomp_agents.__file__).resolve().parent
    new_files = [
        "fork.py",
        "github_claim.py",
        "freeset.py",
        "pr_gate.py",
        "pr.py",
        "distributed_orchestrator.py",
    ]
    for name in new_files:
        text = (pkg_dir / name).read_text()
        assert text.lstrip().startswith('"""'), f"{name} must open with a module docstring"


# ---------------------------------------------------------------------------
# config: DECOMP_MODE
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_config_mode_defaults_to_local(monkeypatch, tmp_path):
    from decomp_agents import config

    _git_repo(tmp_path)
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.delenv("DECOMP_MODE", raising=False)
    monkeypatch.delenv("DECOMP_CLAIM_ISSUE", raising=False)
    cfg = config.load_config()
    assert cfg.mode == "local"
    # Local mode must not require any distributed knobs.
    assert cfg.claim_issue == 0


def test_config_mode_distributed_requires_claim_issue(monkeypatch, tmp_path):
    from decomp_agents import config

    _git_repo(tmp_path)
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.setenv("DECOMP_MODE", "distributed")
    monkeypatch.setenv("DECOMP_UPSTREAM", "owner/meteor-decomp")
    monkeypatch.delenv("DECOMP_CLAIM_ISSUE", raising=False)
    with pytest.raises(RuntimeError, match="DECOMP_CLAIM_ISSUE"):
        config.load_config()


def test_config_mode_distributed_happy(monkeypatch, tmp_path):
    from decomp_agents import config

    _git_repo(tmp_path)
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivgame")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.setenv("DECOMP_MODE", "distributed")
    monkeypatch.setenv("DECOMP_UPSTREAM", "octocat/meteor-decomp")
    monkeypatch.setenv("DECOMP_FORK", "me/meteor-decomp")
    monkeypatch.setenv("DECOMP_CLAIM_ISSUE", "42")
    cfg = config.load_config()
    assert cfg.mode == "distributed"
    assert cfg.upstream == "octocat/meteor-decomp"
    assert cfg.fork == "me/meteor-decomp"
    assert cfg.claim_issue == 42
    assert cfg.upstream_branch == "develop"


def test_config_mode_rejects_bad_value(monkeypatch, tmp_path):
    from decomp_agents import config

    _git_repo(tmp_path)
    monkeypatch.setenv("DECOMP_REPO", str(tmp_path))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.setenv("DECOMP_MODE", "hybrid")
    with pytest.raises(RuntimeError, match="DECOMP_MODE"):
        config.load_config()


# ---------------------------------------------------------------------------
# pr_gate: AGPL header
# ---------------------------------------------------------------------------

_GOOD_HEADER = "\n".join(
    [
        "// meteor-decomp — clean-room decompilation of FINAL FANTASY XIV 1.x client binaries",
        "// Copyright (C) 2026  Samuel Stegall",
        "//",
        "// This program is free software: you can redistribute it and/or modify",
        "// it under the terms of the GNU Affero General Public License as published",
        "// by the Free Software Foundation, either version 3 of the License, or",
        "// (at your option) any later version.",
        "//",
        "// SPDX-License-Identifier: AGPL-3.0-or-later",
        "",
        "extern \"C\" int the_global;",
    ]
)


# A real sibling's stamped provenance banner — tools/stamp_clusters.py
# prepends this 3-line `// [STAMPED]` block before the AGPL boilerplate.
# An agent that copies a stamped sibling's header verbatim must still pass.
_STAMPED_PREFIX = "\n".join(
    [
        "// [STAMPED] from FUN_00401670.cpp by tools/stamp_clusters.py",
        "//           sibling at orig RVA 0x00001730 (VA 0x00401730)",
        "//           same byte-shape cluster — see cluster_shapes.py output",
    ]
)


def test_agpl_header_present_passes():
    from decomp_agents.pr_gate import check_agpl_header

    c = check_agpl_header(_GOOD_HEADER)
    assert c.ok, c.reason


def test_agpl_header_with_stamped_prefix_passes():
    """A `// [STAMPED]` provenance banner before the AGPL block is benign."""
    from decomp_agents.pr_gate import check_agpl_header

    c = check_agpl_header(_STAMPED_PREFIX + "\n" + _GOOD_HEADER)
    assert c.ok, c.reason


def test_agpl_header_with_blank_lines_before_block_passes():
    from decomp_agents.pr_gate import check_agpl_header

    c = check_agpl_header("\n\n" + _GOOD_HEADER)
    assert c.ok, c.reason


def test_agpl_header_buried_below_code_fails():
    """Fail-closed: a license block under real code is not a top-of-file header."""
    from decomp_agents.pr_gate import check_agpl_header

    c = check_agpl_header("int x = 1;\n" + _GOOD_HEADER)
    assert not c.ok
    assert "opening" in c.reason


def test_agpl_header_block_comment_prefix_fails():
    """Fail-closed: a `/* */` block opener before the AGPL block is rejected."""
    from decomp_agents.pr_gate import check_agpl_header

    c = check_agpl_header("/* provenance */\n" + _GOOD_HEADER)
    assert not c.ok


def test_agpl_header_missing_spdx_fails():
    from decomp_agents.pr_gate import check_agpl_header

    body = _GOOD_HEADER.replace("// SPDX-License-Identifier: AGPL-3.0-or-later", "// nope")
    c = check_agpl_header(body)
    assert not c.ok
    assert "SPDX" in c.reason


def test_agpl_header_wrong_wording_fails():
    from decomp_agents.pr_gate import check_agpl_header

    # SPDX line present but a boilerplate line is altered: the verbatim
    # in-order block no longer matches, so the header check fails-closed.
    body = _GOOD_HEADER.replace(
        "// Copyright (C) 2026  Samuel Stegall", "// Copyright (C) 2026  Someone Else"
    )
    c = check_agpl_header(body)
    assert not c.ok
    assert "AGPL header" in c.reason


def test_agpl_header_absent_entirely_fails():
    from decomp_agents.pr_gate import check_agpl_header

    c = check_agpl_header("int main() { return 0; }\n")
    assert not c.ok


# ---------------------------------------------------------------------------
# pr_gate: one-file scope
# ---------------------------------------------------------------------------


def test_one_file_scope_single_added_rosetta_passes():
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope(
        [("A", "src/ffxivgame/_rosetta/FUN_00401030.cpp")], binary="ffxivgame"
    )
    assert c.ok, c.reason


def test_one_file_scope_extra_file_fails():
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope(
        [
            ("A", "src/ffxivgame/_rosetta/FUN_00401030.cpp"),
            ("M", "tools/Makefile"),
        ],
        binary="ffxivgame",
    )
    assert not c.ok
    assert "2 files" in c.reason


def test_one_file_scope_yaml_edit_fails():
    """Distributed PRs must NOT touch config/<bin>.yaml — the ledger owns it."""
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope(
        [
            ("A", "src/ffxivgame/_rosetta/FUN_00401030.cpp"),
            ("M", "config/ffxivgame.yaml"),
        ],
        binary="ffxivgame",
    )
    assert not c.ok


def test_one_file_scope_modify_not_add_fails():
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope(
        [("M", "src/ffxivgame/_rosetta/FUN_00401030.cpp")], binary="ffxivgame"
    )
    assert not c.ok
    assert "ADD" in c.reason


def test_one_file_scope_wrong_path_shape_fails():
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope([("A", "src/ffxivgame/net/blowfish.cpp")], binary="ffxivgame")
    assert not c.ok
    assert "_rosetta" in c.reason


def test_one_file_scope_wrong_binary_fails():
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope(
        [("A", "src/ffxivboot/_rosetta/FUN_00401030.cpp")], binary="ffxivgame"
    )
    assert not c.ok
    assert "ffxivboot" in c.reason


def test_one_file_scope_empty_fails():
    from decomp_agents.pr_gate import check_one_file_scope

    c = check_one_file_scope([], binary="ffxivgame")
    assert not c.ok


# ---------------------------------------------------------------------------
# pr_gate: VA parse + already-solved
# ---------------------------------------------------------------------------


def test_va_parse_ok_and_unsolved():
    from decomp_agents.pr_gate import check_va_parses_and_unsolved, parse_va_from_path

    path = "src/ffxivgame/_rosetta/FUN_0042fc1c.cpp"
    assert parse_va_from_path(path) == 0x0042FC1C
    c = check_va_parses_and_unsolved(path, base_solved_vas=set())
    assert c.ok, c.reason


def test_va_parse_already_solved_fails():
    from decomp_agents.pr_gate import check_va_parses_and_unsolved

    path = "src/ffxivgame/_rosetta/FUN_0042fc1c.cpp"
    c = check_va_parses_and_unsolved(path, base_solved_vas={0x0042FC1C})
    assert not c.ok
    assert "already solved" in c.reason


def test_va_parse_unparseable_fails():
    from decomp_agents.pr_gate import check_va_parses_and_unsolved, parse_va_from_path

    assert parse_va_from_path("README.md") is None
    c = check_va_parses_and_unsolved("README.md", base_solved_vas=set())
    assert not c.ok


# ---------------------------------------------------------------------------
# pr_gate: GateReport aggregation + run_pr_gate skip-on-scope-fail
# ---------------------------------------------------------------------------


def test_gate_report_ok_and_summary():
    from decomp_agents.pr_gate import GateCheck, GateReport

    r = GateReport(
        checks=[
            GateCheck("green", True, ""),
            GateCheck("one_file_scope", True, ""),
            GateCheck("agpl_header", True, ""),
            GateCheck("va_parse", True, ""),
        ]
    )
    assert r.ok
    assert "PASS" in r.summary()
    assert not r.failures

    r.checks[0] = GateCheck("green", False, "not GREEN")
    assert not r.ok
    assert "FAIL" in r.summary()
    assert len(r.failures) == 1


# ---------------------------------------------------------------------------
# freeset: exclusion-set parsing + eligibility
# ---------------------------------------------------------------------------


def test_solved_rvas_from_rosetta_dir(tmp_path):
    from decomp_agents.freeset import solved_rvas

    rosetta = tmp_path / "ffxivgame" / "_rosetta"
    rosetta.mkdir(parents=True)
    (rosetta / "FUN_00401030.cpp").write_text("// x")
    (rosetta / "FUN_0042fc1c.cpp").write_text("// x")
    (rosetta / "notes.txt").write_text("ignored")

    got = solved_rvas("ffxivgame", src_root=tmp_path)
    # VA - IMAGE_BASE(0x400000) => RVA
    assert got == {0x00401030 - 0x400000, 0x0042FC1C - 0x400000}


def test_live_claim_rvas_from_ledger(tmp_path):
    from decomp_agents.freeset import live_claim_rvas

    ledger = tmp_path / "claims" / "ffxivgame"
    ledger.mkdir(parents=True)
    (ledger / "4144.json").write_text("{}")
    (ledger / "196636.json").write_text("{}")
    (ledger / "junk.json").write_text("{}")  # non-numeric name ignored

    got = live_claim_rvas("ffxivgame", ledger_dir=tmp_path / "claims")
    assert got == {4144, 196636}


def test_open_pr_rvas_from_paths_filters_binary():
    from decomp_agents.freeset import open_pr_rvas_from_paths

    paths = [
        "src/ffxivgame/_rosetta/FUN_00401030.cpp",  # included
        "src/ffxivboot/_rosetta/FUN_00401030.cpp",  # other binary -> excluded
        "README.md",
        "config/ffxivgame.yaml",
    ]
    got = open_pr_rvas_from_paths("ffxivgame", paths)
    assert got == {0x00401030 - 0x400000}


def test_eligible_functions_subtracts_all_three(tmp_path):
    from decomp_agents.freeset import eligible_functions
    from decomp_agents.work_queue import Function, WorkQueue

    schema = Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    queue = WorkQueue(tmp_path / "coord.sqlite", schema)

    def mk(rva: int) -> Function:
        return Function(
            binary="ffxivgame",
            rva=rva,
            end=rva + 0x40,
            size=0x40,
            module="net/blowfish",  # non-_unknown so it's not a naked stub
            symbol=f"FUN_{rva + 0x400000:08x}",
            type="matching",
            status="unmatched",
            section=".text",
        )

    pool = [mk(0x1000), mk(0x2000), mk(0x3000), mk(0x4000)]

    out = eligible_functions(
        pool,
        queue=queue,
        max_size=0x200,
        solved={0x1000},      # excluded: solved
        claimed={0x2000},     # excluded: live claim
        open_pr={0x3000},     # excluded: open PR
    )
    rvas = {fn.rva for fn in out}
    assert rvas == {0x4000}


def test_eligible_functions_respects_base_filters(tmp_path):
    """A too-big or non-diff-compatible function is dropped by filter_eligible
    even if it's in none of the three exclusion sets."""
    from decomp_agents.freeset import eligible_functions
    from decomp_agents.work_queue import Function, WorkQueue

    schema = Path(__file__).resolve().parent.parent / "schema" / "coordination.sql"
    queue = WorkQueue(tmp_path / "coord.sqlite", schema)

    too_big = Function(
        binary="ffxivgame",
        rva=0x5000,
        end=0x5000 + 0x900,
        size=0x900,  # > max_size
        module="net/blowfish",
        symbol="FUN_00405000",
        type="matching",
        status="unmatched",
        section=".text",
    )
    named = Function(
        binary="ffxivgame",
        rva=0x6000,
        end=0x6040,
        size=0x40,
        module="net/blowfish",
        symbol="Blowfish::Init",  # not FUN_<addr> -> not diff-compatible
        type="matching",
        status="unmatched",
        section=".text",
    )
    out = eligible_functions(
        [too_big, named],
        queue=queue,
        max_size=0x200,
        solved=set(),
        claimed=set(),
        open_pr=set(),
    )
    assert out == []


# ---------------------------------------------------------------------------
# github_claim: ledger-win parsing against a fake claims dir
# ---------------------------------------------------------------------------


def _write_claim(ledger_dir: Path, binary: str, rva: int, *, owner: str, state: str):
    d = ledger_dir / binary
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rva}.json").write_text(
        json.dumps(
            {
                "binary": binary,
                "rva": rva,
                "va": f"0x{rva + 0x400000:08x}",
                "owner": owner,
                "claimed_at": "2026-05-31T00:00:00+00:00",
                "lease_expires_at": "2026-06-02T00:00:00+00:00",
                "state": state,
            }
        )
    )


def test_ledger_win_owner_active(tmp_path):
    from decomp_agents.github_claim import ledger_win

    _write_claim(tmp_path, "ffxivgame", 4144, owner="octocat", state="active")
    assert ledger_win(tmp_path, "ffxivgame", 4144, "octocat") is True


def test_ledger_win_pinned_by_pr(tmp_path):
    from decomp_agents.github_claim import ledger_win

    _write_claim(tmp_path, "ffxivgame", 4144, owner="octocat", state="pinned-by-PR")
    assert ledger_win(tmp_path, "ffxivgame", 4144, "octocat") is True


def test_ledger_win_other_owner_is_loss(tmp_path):
    from decomp_agents.github_claim import ledger_win

    _write_claim(tmp_path, "ffxivgame", 4144, owner="someone-else", state="active")
    assert ledger_win(tmp_path, "ffxivgame", 4144, "octocat") is False


def test_ledger_win_absent_file_is_loss(tmp_path):
    from decomp_agents.github_claim import ledger_win

    assert ledger_win(tmp_path, "ffxivgame", 9999, "octocat") is False


def test_ledger_win_unknown_state_is_loss(tmp_path):
    from decomp_agents.github_claim import ledger_win

    _write_claim(tmp_path, "ffxivgame", 4144, owner="octocat", state="quarantined")
    assert ledger_win(tmp_path, "ffxivgame", 4144, "octocat") is False


def test_read_ledger_claim_roundtrip(tmp_path):
    from decomp_agents.github_claim import read_ledger_claim

    _write_claim(tmp_path, "ffxivgame", 4144, owner="octocat", state="active")
    c = read_ledger_claim(tmp_path, "ffxivgame", 4144)
    assert c is not None
    assert c.owner == "octocat"
    assert c.state == "active"
    assert c.rva == 4144


def test_va_rva_roundtrip():
    from decomp_agents.github_claim import rva_to_va, va_to_rva

    assert rva_to_va(0x1030) == 0x401030
    assert va_to_rva(0x401030) == 0x1030


# ---------------------------------------------------------------------------
# pr: title + body builders (pure)
# ---------------------------------------------------------------------------


def test_build_pr_title():
    from decomp_agents.pr import build_pr_title

    assert build_pr_title("FUN_0042fc1c", 0x2FC1C) == "decomp: match FUN_0042fc1c @0x0002fc1c"


def test_build_pr_body_mentions_va_and_attribution():
    from decomp_agents.pr import build_pr_body

    body = build_pr_body(
        binary="ffxivgame",
        symbol="FUN_0042fc1c",
        rva=0x2FC1C,
        iterations=3,
        gate_summary="PR-gate PASS: green=ok",
    )
    assert "ffxivgame" in body
    assert "0x0042fc1c" in body  # VA = rva + image base
    assert "FUN_0042fc1c.cpp" in body  # _rosetta filename keyed by VA (0x2fc1c + 0x400000)
    assert "Claude Code" in body


def test_function_branch_name():
    from decomp_agents.fork import function_branch_name

    assert function_branch_name(0x2FC1C, "FUN_0042fc1c") == "agents/0x0002fc1c_FUN_0042fc1c"
    # namespace + slash sanitising
    assert function_branch_name(0x1230, "Blowfish::Init") == "agents/0x00001230_Blowfish__Init"


# ---------------------------------------------------------------------------
# worker brief: distributed vs local variant
# ---------------------------------------------------------------------------


def _min_config(repo: Path, *, binary: str = "ffxivgame", max_iters: int = 12):
    """A Config with only the fields the brief reads populated.

    The brief touches cfg.repo, cfg.repo.parent, cfg.binary, and
    cfg.max_iterations_per_function — everything else can be None for the
    brief-rendering path (we never call load_config / the SDK here).
    """
    from decomp_agents.config import Config

    fields = {f.name: None for f in dataclasses.fields(Config)}
    fields.update(
        repo=repo,
        binary=binary,
        max_iterations_per_function=max_iters,
    )
    return Config(**fields)


def _fn(rva: int = 0x2FC1C, *, binary: str = "ffxivgame", module: str = "_unknown"):
    from decomp_agents.work_queue import Function

    return Function(
        binary=binary,
        rva=rva,
        end=rva + 0x44,
        size=0x44,
        module=module,
        symbol=f"FUN_{rva + 0x400000:08x}",
        type="matching",
        status="unmatched",
        section=".text",
    )


def _ctx():
    from decomp_agents.prompt_context import PromptContext

    return PromptContext(
        asm_relpath="asm/ffxivgame/0042fc1c_FUN_0042fc1c.s",
        pseudo_c_relpath=None,
        rtti_class=None,
        rtti_vtable_rva_hex=None,
    )


def test_distributed_brief_targets_rosetta_no_yaml(tmp_path):
    """The distributed brief points at _rosetta/FUN_<va>.cpp, no YAML, make rosetta."""
    from decomp_agents.worker import _function_brief

    fn = _fn(module="engine_cdev")  # a REAL module value, not _rosetta
    brief = _function_brief(fn, _min_config(tmp_path), _ctx(), distributed=True)

    assert "src/ffxivgame/_rosetta/FUN_0042fc1c.cpp" in brief
    assert "make rosetta BINARY=ffxivgame.exe" in brief
    # Must NOT instruct the module/symbol.cpp path or a YAML edit.
    assert "<module>/<symbol>.cpp" not in brief
    # The real module value must never be used as a PATH component (it
    # appears once, informationally, in the Target header — never in a
    # `src/...` write target).
    assert "src/ffxivgame/engine_cdev" not in brief
    assert "Do NOT edit `config/ffxivgame.yaml`" in brief


def test_local_brief_unchanged(tmp_path):
    """The LOCAL brief still uses module/symbol.cpp + a YAML row edit."""
    from decomp_agents.worker import _function_brief

    fn = _fn()
    brief = _function_brief(fn, _min_config(tmp_path), _ctx(), distributed=False)

    assert "src/ffxivgame/<module>/<symbol>.cpp" in brief
    assert "Update `config/ffxivgame.yaml`" in brief
    assert "make rosetta" not in brief


# ---------------------------------------------------------------------------
# END-TO-END: real distributed brief output -> run_pr_gate (toolchain-free)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout


def _init_fork_clone(tmp_path: Path, binary: str = "ffxivgame") -> Path:
    """A temp git repo with an `upstream/<branch>` ref (a base, no _rosetta).

    Simulates a fork clone whose `upstream/develop` carries the committed
    solved tree. We stand in a local branch named `upstream/develop` so the
    gate's `git diff upstream/develop...HEAD` and `git ls-tree
    upstream/develop` both resolve without a network remote.
    """
    repo = tmp_path / "fork"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "agent@example.com")
    _git(repo, "config", "user.name", "agent")
    # Seed a base commit with an existing (unrelated) solved file so the
    # _rosetta dir exists upstream but our VA is NOT in it.
    base = repo / "src" / binary / "_rosetta"
    base.mkdir(parents=True)
    (base / "FUN_00401010.cpp").write_text("// existing\n")
    (repo / "config").mkdir()
    (repo / "config" / f"{binary}.yaml").write_text("- rva: 0x1010\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # Name the base ref `upstream/develop` so the gate's refs resolve.
    _git(repo, "branch", "upstream/develop")
    return repo


# A stamped AGPL header, exactly as a sibling-copying agent would produce.
_STAMPED_AGPL_CPP = (
    "// [STAMPED] from FUN_00401670.cpp by tools/stamp_clusters.py\n"
    "// meteor-decomp — clean-room decompilation of FINAL FANTASY XIV 1.x client binaries\n"
    "// Copyright (C) 2026  Samuel Stegall\n"
    "//\n"
    "// This program is free software: you can redistribute it and/or modify\n"
    "// it under the terms of the GNU Affero General Public License as published\n"
    "// by the Free Software Foundation, either version 3 of the License, or\n"
    "// (at your option) any later version.\n"
    "//\n"
    "// SPDX-License-Identifier: AGPL-3.0-or-later\n"
    "\n"
    "extern \"C\" void FUN_0042fc1c(void) {}\n"
)


def _extract_rosetta_path_from_brief(brief: str, binary: str, va: int) -> str:
    """The single new-file path the distributed brief instructs."""
    want = f"src/{binary}/_rosetta/FUN_{va:08x}.cpp"
    assert want in brief, "distributed brief did not name the _rosetta path"
    return want


def test_e2e_distributed_brief_output_passes_gate(tmp_path, monkeypatch):
    """Materialise the EXACT file the distributed brief instructs, commit it
    on a per-function branch, and confirm run_pr_gate PASSES end-to-end.

    The GREEN check is the only toolchain-bound step (it shells `make
    diff`), so we stub worker._grade_outcome to "matched" — every OTHER
    gate (one-file scope, AGPL header on a STAMPED file, VA parse +
    not-already-solved against the UPSTREAM ref) runs for real against a
    real git tree.
    """
    from decomp_agents import worker
    from decomp_agents.freeset import solved_rvas_from_ref
    from decomp_agents.pr_gate import run_pr_gate
    from decomp_agents.worker import _function_brief

    binary = "ffxivgame"
    va = 0x0042FC1C
    rva = va - 0x400000
    repo = _init_fork_clone(tmp_path, binary)

    # Build the real distributed brief and read the path it names.
    fn = _fn(rva=rva, binary=binary, module="engine_cdev")
    brief = _function_brief(fn, _min_config(repo, binary=binary), _ctx(), distributed=True)
    rel = _extract_rosetta_path_from_brief(brief, binary, va)

    # Simulate the agent: cut a per-function branch off the upstream base,
    # write EXACTLY that one file (stamped AGPL header), git add ONLY it.
    _git(repo, "checkout", "-q", "-B", "agents/work", "upstream/develop")
    target = repo / rel
    target.write_text(_STAMPED_AGPL_CPP)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"decomp: match FUN_{va:08x} @0x{rva:08x}")

    # The GREEN check is the only toolchain-bound step — stub the grader.
    monkeypatch.setattr(worker, "_grade_outcome", lambda *a, **k: "matched")

    # base_solved from the UPSTREAM REF (the fix): must NOT include our VA
    # even though the working tree now carries it on the branch.
    base_solved = solved_rvas_from_ref(
        repo, upstream_ref="upstream/develop", binary=binary
    )
    assert rva not in base_solved  # the agent's own file is excluded

    report = run_pr_gate(
        clone_path=repo,
        base_ref="upstream/develop",
        symbol=fn.symbol,
        binary=binary,
        base_solved_vas={r + 0x400000 for r in base_solved},
    )
    assert report.ok, report.summary() + " :: " + "; ".join(
        f"{c.name}:{c.reason}" for c in report.failures
    )


def test_e2e_brief_violation_yaml_edit_fails_gate(tmp_path, monkeypatch):
    """If the agent ALSO edits the YAML (the old/local brief behaviour), the
    one-file-scope gate must FAIL — proving the gate guards the convention."""
    from decomp_agents import worker
    from decomp_agents.pr_gate import run_pr_gate

    binary = "ffxivgame"
    va = 0x0042FC1C
    rva = va - 0x400000
    repo = _init_fork_clone(tmp_path, binary)

    _git(repo, "checkout", "-q", "-B", "agents/work", "upstream/develop")
    (repo / "src" / binary / "_rosetta" / f"FUN_{va:08x}.cpp").write_text(_STAMPED_AGPL_CPP)
    # The local-brief mistake: also bump the YAML row.
    (repo / "config" / f"{binary}.yaml").write_text("- rva: 0x1010\n- rva: 0x2fc1c\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "decomp + yaml")

    monkeypatch.setattr(worker, "_grade_outcome", lambda *a, **k: "matched")

    report = run_pr_gate(
        clone_path=repo,
        base_ref="upstream/develop",
        symbol=f"FUN_{va:08x}",
        binary=binary,
        base_solved_vas=set(),
    )
    assert not report.ok
    scope = next(c for c in report.checks if c.name == "one_file_scope")
    assert not scope.ok


# ---------------------------------------------------------------------------
# toolchain-artifact provisioning (distributed-mode GREEN grader)
# ---------------------------------------------------------------------------


def _fake_artifacts_dir(tmp_path: Path, binary: str = "ffxivgame") -> Path:
    """A stand-in DECOMP_ARTIFACTS_DIR: a dir with fake orig/ + symbols.json.

    No real SE binary is needed — the provisioning only symlinks files, it
    never reads their contents.
    """
    src = tmp_path / "artifacts"
    (src / "orig").mkdir(parents=True)
    (src / "config").mkdir(parents=True)
    (src / "orig" / f"{binary}.exe").write_bytes(b"MZ-fake-binary\x00")
    (src / "config" / f"{binary}.symbols.json").write_text('[{"rva": 0, "size": 4}]')
    return src


def _empty_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    return clone


def test_provision_toolchain_symlinks_both_files(tmp_path):
    """Given an artifacts dir with orig + symbols, provisioning symlinks both
    into the clone at the gitignored paths `make diff` reads."""
    from decomp_agents.fork import provision_toolchain_artifacts

    binary = "ffxivgame"
    src = _fake_artifacts_dir(tmp_path, binary)
    clone = _empty_clone(tmp_path)

    result = provision_toolchain_artifacts(clone, src, binary)

    assert result.ok, result.warning
    orig_link = clone / "orig" / f"{binary}.exe"
    sym_link = clone / "config" / f"{binary}.symbols.json"
    assert orig_link.is_symlink()
    assert sym_link.is_symlink()
    # Symlinks resolve to the artifacts-dir sources (never copies).
    assert orig_link.resolve() == (src / "orig" / f"{binary}.exe").resolve()
    assert sym_link.resolve() == (src / "config" / f"{binary}.symbols.json").resolve()
    # The configured binary's pair is present and readable through the link.
    assert orig_link.exists() and sym_link.exists()


def test_provision_toolchain_is_idempotent(tmp_path):
    """Re-running provisioning replaces stale symlinks without error and
    converges to the same final links."""
    from decomp_agents.fork import provision_toolchain_artifacts

    binary = "ffxivgame"
    src = _fake_artifacts_dir(tmp_path, binary)
    clone = _empty_clone(tmp_path)

    first = provision_toolchain_artifacts(clone, src, binary)
    assert first.ok

    # Plant a STALE symlink (points at a now-bogus path) to prove it's
    # replaced rather than tripping on "already exists".
    stale = clone / "orig" / f"{binary}.exe"
    stale.unlink()
    stale.symlink_to(tmp_path / "does-not-exist.exe")
    assert stale.is_symlink() and not stale.exists()  # broken on purpose

    second = provision_toolchain_artifacts(clone, src, binary)
    assert second.ok, second.warning
    assert stale.is_symlink()
    assert stale.resolve() == (src / "orig" / f"{binary}.exe").resolve()


def test_provision_toolchain_none_artifacts_dir_warns_no_crash(tmp_path):
    """artifacts_dir=None degrades gracefully: a warning + ok=False, no
    exception, no symlinks created."""
    from decomp_agents.fork import provision_toolchain_artifacts

    clone = _empty_clone(tmp_path)
    result = provision_toolchain_artifacts(clone, None, "ffxivgame")

    assert result.ok is False
    assert result.warning is not None
    assert "DECOMP_ARTIFACTS_DIR" in result.warning
    # Nothing was symlinked.
    assert not (clone / "orig").exists()


def test_provision_toolchain_missing_source_files_warns_no_crash(tmp_path):
    """An artifacts dir that lacks the configured binary's files degrades
    gracefully: warning + ok=False, no crash."""
    from decomp_agents.fork import provision_toolchain_artifacts

    # An artifacts dir with empty orig/ + config/ — no <bin>.exe / symbols.
    src = tmp_path / "artifacts"
    (src / "orig").mkdir(parents=True)
    (src / "config").mkdir(parents=True)
    clone = _empty_clone(tmp_path)

    result = provision_toolchain_artifacts(clone, src, "ffxivgame")

    assert result.ok is False
    assert result.warning is not None
    assert "ffxivgame.exe" in result.warning
    # No symlink was created for the missing source.
    assert not (clone / "orig" / "ffxivgame.exe").exists()


def test_provision_toolchain_partial_only_symbols_present_is_not_ok(tmp_path):
    """Grading needs BOTH artifacts; only one present ⇒ ok=False with a
    warning naming the missing file."""
    from decomp_agents.fork import provision_toolchain_artifacts

    binary = "ffxivgame"
    src = tmp_path / "artifacts"
    (src / "orig").mkdir(parents=True)
    (src / "config").mkdir(parents=True)
    # Only symbols.json is present; orig/<bin>.exe is missing.
    (src / "config" / f"{binary}.symbols.json").write_text("[]")
    clone = _empty_clone(tmp_path)

    result = provision_toolchain_artifacts(clone, src, binary)

    assert result.ok is False
    assert result.warning is not None and f"orig/{binary}.exe" in result.warning
    # The present one is still symlinked (so a later fix only needs orig).
    assert (clone / "config" / f"{binary}.symbols.json").is_symlink()


def test_provision_toolchain_links_extra_binaries_when_present(tmp_path):
    """All five binaries' artifacts are symlinked when present, but only the
    configured binary's pair gates ok."""
    from decomp_agents.config import SUPPORTED_BINARIES
    from decomp_agents.fork import provision_toolchain_artifacts

    src = tmp_path / "artifacts"
    (src / "orig").mkdir(parents=True)
    (src / "config").mkdir(parents=True)
    for stem in SUPPORTED_BINARIES:
        (src / "orig" / f"{stem}.exe").write_bytes(b"MZ")
        (src / "config" / f"{stem}.symbols.json").write_text("[]")
    clone = _empty_clone(tmp_path)

    result = provision_toolchain_artifacts(clone, src, "ffxivlogin")

    assert result.ok
    for stem in SUPPORTED_BINARIES:
        assert (clone / "orig" / f"{stem}.exe").is_symlink()
        assert (clone / "config" / f"{stem}.symbols.json").is_symlink()


# ---------------------------------------------------------------------------
# config: DECOMP_ARTIFACTS_DIR resolution
# ---------------------------------------------------------------------------


def test_config_artifacts_dir_explicit_env_wins(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / "repo").mkdir()
    repo = _git_repo(tmp_path / "repo")
    art = tmp_path / "art"
    (art / "orig").mkdir(parents=True)
    monkeypatch.setenv("DECOMP_REPO", str(repo))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.setenv("DECOMP_ARTIFACTS_DIR", str(art))
    cfg = config.load_config()
    assert cfg.artifacts_dir == art.resolve()


def test_config_artifacts_dir_defaults_to_repo_when_orig_present(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / "repo").mkdir()
    repo = _git_repo(tmp_path / "repo")
    (repo / "orig").mkdir()
    monkeypatch.setenv("DECOMP_REPO", str(repo))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.delenv("DECOMP_ARTIFACTS_DIR", raising=False)
    cfg = config.load_config()
    assert cfg.artifacts_dir == repo.resolve()


def test_config_artifacts_dir_none_when_no_orig_and_unset(monkeypatch, tmp_path):
    from decomp_agents import config

    (tmp_path / "repo").mkdir()
    repo = _git_repo(tmp_path / "repo")  # no orig/ dir
    monkeypatch.setenv("DECOMP_REPO", str(repo))
    monkeypatch.setenv("DECOMP_BINARY", "ffxivlogin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foo")
    monkeypatch.delenv("DECOMP_ARTIFACTS_DIR", raising=False)
    cfg = config.load_config()
    assert cfg.artifacts_dir is None


# ---------------------------------------------------------------------------
# COPYRIGHT-LEAK GUARD: symlinked orig/symbols are gitignored in the clone
# ---------------------------------------------------------------------------


def test_symlinked_artifacts_are_gitignored_cannot_leak(tmp_path):
    """Prove copyrighted material can't leak into a commit/PR: a symlinked
    `orig/<bin>.exe` and `config/<bin>.symbols.json` inside a clone whose
    `.gitignore` carries `orig/*` + `config/*.symbols.json` are NOT staged
    by `git add -A` and report as ignored to `git check-ignore`."""
    from decomp_agents.fork import provision_toolchain_artifacts

    binary = "ffxivgame"
    src = _fake_artifacts_dir(tmp_path, binary)

    # A real git repo standing in for the fork clone, carrying the SAME
    # ignore patterns meteor-decomp's .gitignore uses for these paths.
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "agent@example.com")
    _git(clone, "config", "user.name", "agent")
    (clone / ".gitignore").write_text(
        "orig/*\n!orig/README.md\n!orig/.gitkeep\nconfig/*.symbols.json\n"
    )
    _git(clone, "add", ".gitignore")
    _git(clone, "commit", "-q", "-m", "base with gitignore")

    result = provision_toolchain_artifacts(clone, src, binary)
    assert result.ok, result.warning

    rel_orig = f"orig/{binary}.exe"
    rel_sym = f"config/{binary}.symbols.json"

    # git check-ignore exits 0 (and echoes the path) when the path IS ignored.
    for rel in (rel_orig, rel_sym):
        r = subprocess.run(
            ["git", "-C", str(clone), "check-ignore", rel],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"{rel} is NOT gitignored — copyright could leak!"
        assert rel in r.stdout

    # `git add -A` must NOT stage either symlink. Only the (already-committed)
    # .gitignore is tracked; the artifacts stay invisible to git.
    _git(clone, "add", "-A")
    staged = _git(clone, "diff", "--cached", "--name-only")
    assert rel_orig not in staged
    assert rel_sym not in staged
    # `git status --porcelain` (no --ignored) shows NOTHING untracked: the
    # symlinks never surface as candidates to add, so they cannot be
    # accidentally committed. (`git add -A` above also left the index clean.)
    assert _git(clone, "status", "--porcelain").strip() == ""
    # And with --ignored, git reports them as ignored (collapsed to the
    # whole-directory level, `!! orig/` / `!! config/`, since the entire
    # dirs are ignored+untracked — that still proves the files can't leak).
    ignored = _git(clone, "status", "--porcelain", "--ignored")
    assert "!! orig/" in ignored
    assert "!! config/" in ignored
