# decomp-agents

Parallel autonomous Claude agents that grind through meteor-decomp's
per-function matching workflow. Spawns 1–8 worker subprocesses, each
pinned to its own git worktree of `meteor-decomp/`, claims functions
from a shared SQLite queue, and merges completed branches back to the
base branch with auto-resolution for trivial YAML row conflicts.

## What it does

1. **Provisions N git worktrees** of meteor-decomp (`.worktrees/agent-0`,
   `agent-1`, …) — shared `.git/`, no full re-clone.
2. **Spawns N worker subprocesses**, each running the Claude Agent SDK
   in `acceptEdits` permission mode, pointed at its own worktree.
3. **Hands each worker one function at a time** via a SQLite-backed
   atomic claim. SQLite is the source of truth because
   `meteor-decomp/config/*.yaml` is gitignored (regenerated from
   `make split`) — git commits can't be the claim mechanism.
4. **Each worker drives the canonical loop** (claim → asm → cpp →
   `make .obj` → `make diff FUNC=…` → iterate → commit or bail).
   See `meteor-decomp/docs/matching-workflow.md` and
   `meteor-decomp/AGENTS.md`.
5. **An orchestrator-side merge loop** periodically pulls completed
   per-function branches back into the base branch, auto-resolving
   YAML-only conflicts and escalating anything else to
   `output/merges/escalated/<claim>.json` for human review.

## Why meteor-decomp specifically

The workflow has the rare combination of properties that make
autonomous agents safe to run in parallel:

- **Atomic unit of work**: one function per YAML row, ~12k unmatched
  in `ffxivgame.exe` alone.
- **Programmatic ground truth**: `make diff FUNC=...` returns OK /
  PARTIAL / MISMATCH — no human judgment needed to grade the work.
- **Low cross-function coupling**: functions compile to independent
  `.obj` files; the linker handles `.text$X<rva>` subsection merging.
- **Reproducible build**: frozen MSVC 2005 SP1 + Wine.
- **Cheap recovery**: a bad branch is just a `git branch -D` away.

## Install

Requires Python ≥ 3.11. The project uses [uv](https://docs.astral.sh/uv/)
or pip — either works.

```sh
cd server-workspace/decomp-agents
uv venv && source .venv/bin/activate
uv pip install -e .                 # or:  pip install -e .
cp .env.example .env
```

## Authentication

Two paths, pick one. The SDK auto-detects.

**A) Use your Claude Pro / Max subscription (no per-token billing).**
One-time OAuth via the Claude Code CLI; credentials land in macOS
Keychain and every worker subprocess picks them up automatically.

```sh
claude              # opens browser; sign in once
claude /status      # confirm "Logged in via subscription"
unset ANTHROPIC_API_KEY    # make sure no key shadows OAuth
# optionally pin intent so a stray key in some other shell can't sneak in:
echo "DECOMP_AUTH_MODE=subscription" >> .env
```

Quota draws against your plan's monthly Agent-SDK credit pool (Max 5×
$100/mo, Max 20× $200/mo, Pro $20/mo as of 2026-06-15). Track at
your Claude account `/usage` page. When exhausted you'll see HTTP
429 / `quota_exceeded` and the worker will release the claim with
`outcome=error`.

**B) Use a metered API key (pay-as-you-go).**
Get a key at <https://console.anthropic.com/settings/keys>, add a
payment method, then:

```sh
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
# optionally pin intent:
echo "DECOMP_AUTH_MODE=api_key" >> .env
```

Budget ballpark: ~$0.10–0.50 per attempted function. Five workers ×
eight attempts ≈ $4–20 per session.

You also need:

- a working meteor-decomp checkout at `../meteor-decomp/` (override
  with `DECOMP_REPO=`) that has already run `make split BINARY=<X>.exe`
  for the binary you want to chew on (so `config/<X>.yaml` exists)
- the meteor-decomp build toolchain working
  (`tools/cl-wine.sh`, Wine, MSVC 2005 SP1) — see
  `meteor-decomp/docs/msvc-setup.md`
- **Recommended**: `make decompile-headless BINARY=<X>.exe` in
  meteor-decomp before running this. That dumps Ghidra's pseudo-C to
  `build/ghidra-decomp/<binary>/<rva>_<sym>.c` so workers have a
  structural hint per function. Without it they work from raw asm
  only (still possible but match rate drops).

## Run

Dry run first — provisions worktrees + DB, no agents spawned:

```sh
python orchestrator.py --dry-run
```

Then for real:

```sh
python orchestrator.py
```

Or via the installed script:

```sh
decomp-agents
```

## Environment variables

| Name | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | _unset_ | Pay-as-you-go API key. Leave unset to use subscription OAuth (via `claude login`). |
| `DECOMP_AUTH_MODE` | _autodetect_ | Pin auth path: `subscription` or `api_key`. Auto-detect = `api_key` if `ANTHROPIC_API_KEY` set, else `subscription`. |
| `DECOMP_AGENT_WORKERS` | `5` | Parallel worker count, 1–8. |
| `DECOMP_REPO` | `../meteor-decomp` | Path to the meteor-decomp checkout. |
| `DECOMP_BINARY` | `ffxivlogin` | One of `ffxivgame`, `ffxivboot`, `ffxivlogin`, `ffxivconfig`, `ffxivupdater`. |
| `DECOMP_WORKTREE_ROOT` | `<repo>/.worktrees` | Where the per-agent worktrees live. |
| `DECOMP_OUTPUT_DIR` | `./output` | Unified output directory (transcripts, logs, claims, merges, SQLite DB). |
| `DECOMP_MAX_ATTEMPTS_PER_WORKER` | `8` | Hard cap per session per worker. |
| `DECOMP_MAX_ITERATIONS_PER_FUNCTION` | `12` | Approximate turn budget. Worker bails past this. |
| `DECOMP_MAX_FUNCTION_SIZE` | `0x200` | Skip functions bigger than this; small ones match fastest. |
| `DECOMP_LIVE_TAIL` | `1` | Pretty-print worker JSON events on stderr. |
| `DECOMP_AUTOPUSH` | `branches+master` | `none` / `branches` / `branches+master`. Whether (and how aggressively) to `git push origin` after successful matches. |
| `DECOMP_WORKER_MODEL` | `opus` | `opus` / `sonnet` / `haiku` or a full `claude-...` model id. Opus is the strongest at codegen reasoning. |
| `DECOMP_POST_MERGE_STAMP` | `1` | After each successful match-branch merge, run meteor-decomp's `stamp_clusters.py --reloc` + `validate_clusters.py` + `update_yaml_status.py` against the matched RVA's cluster (read from `build/easy_wins/<bin>.clusters_reloc.json`). Stamped + GREEN siblings are committed on top of the merge before the base ref is advanced. Singleton matches are a silent no-op. Requires that `make stamp-reloc` has been run at least once so the cluster JSON exists. Set to `0` to disable. |
| `DECOMP_CROSS_SESSION_MERGE` | `1` | Drain pending merges from prior sessions in addition to the current one. Prior orchestrators sometimes exited before their final merge pass landed every match; this picks up the strays. Set to `0` to restrict the merge loop to the current session's claims only. |

## Git topology — what actually moves to GitHub

After a worker matches a function:

1. Worker commits to its per-function branch `agents/agent-N/<binary>/<rva>_<symbol>`.
2. Worker pushes that branch to `origin` (if `DECOMP_AUTOPUSH ∈ {branches, branches+master}`).
3. Every 20s, the merge orchestrator (in a dedicated `_merge/staging`
   worktree to avoid colliding with whatever branch your main checkout
   is on) merges the per-function branch into the local base branch
   via `git update-ref`. YAML-only conflicts auto-resolve; everything
   else escalates to `output/merges/escalated/`.
4. The merge orchestrator pushes the advanced base branch to `origin`
   (if `DECOMP_AUTOPUSH == branches+master`).
5. Your main meteor-decomp checkout sees the new base SHA the next
   time it reads the ref. No worktree gets clobbered.

If `DECOMP_AUTOPUSH=none`: steps 1, 3 happen; 2 and 4 are skipped. You
push manually when you're ready.

If `DECOMP_POST_MERGE_STAMP=1` (the default), step 3 also chains a
`stamp:` commit on the staging branch *before* the base ref advances —
so each match landing brings its cluster's GREEN siblings with it.
The staging worktree is the only place stamping runs; agent worktrees
stay tight to one .cpp + one YAML row per branch.

The orchestrator runs an `ls-remote` sanity check at startup; if SSH
auth is broken you'll see a warning before workers spawn rather than a
flood of push failures mid-run.

## Output layout

```
output/
├── coordination.sqlite          # claim queue + tool-use log + merge log
├── transcripts/agent-N.jsonl    # per-worker tool-use timeline
├── logs/agent-N.stdout.log      # raw worker stdout
├── logs/agent-N.stderr.log      # raw worker stderr
├── claims/                      # reserved for future audit dumps
└── merges/
    └── escalated/               # JSON for conflicts the resolver can't auto-handle
```

Inspect what happened:

```sh
sqlite3 output/coordination.sqlite "
  SELECT outcome, COUNT(*) FROM claims
  WHERE released_at IS NOT NULL
  GROUP BY outcome;"
```

```sh
sqlite3 output/coordination.sqlite "
  SELECT c.symbol, c.iterations, c.outcome, m.result
  FROM claims c LEFT JOIN merges m ON m.claim_id = c.id
  ORDER BY c.claimed_at DESC LIMIT 20;"
```

## Cost expectations

A single function match is typically 5–20 tool calls (Read asm, Read
2–3 siblings, Write cpp, Bash make .obj, Bash make diff, possibly 1–3
edit/recompile/rediff cycles). At Sonnet pricing that's ~$0.10–$0.50
per attempted function. **Five workers running 8 attempts each ≈
$4–20/session.** Set `DECOMP_MAX_ATTEMPTS_PER_WORKER` lower for a
shorter test run.

## Kill switches

- **SIGINT** (Ctrl-C) the orchestrator → terminates all workers,
  flushes one final merge pass, closes the session row.
- **Kill one worker** without stopping the others:
  `kill <pid_of_agent-N>`. Other agents keep going.
- **Re-run the merge pass only** (no new claims):
  the orchestrator's merge loop runs every 20s. To bring a manual
  resolution back into the rotation after fixing a conflict, just
  set the row's `merge_status` back to `pending`:
  ```sh
  sqlite3 output/coordination.sqlite "
    UPDATE claims SET merge_status='pending' WHERE id=<n>;"
  ```

## Auto-resolved merge conflicts

The merge orchestrator now classifies every conflicted path against a
strategy table. A merge auto-resolves iff **every** path has a strategy;
a single un-categorised path escalates the whole merge.

| Path category | Strategy | Why |
|---|---|---|
| `config/<bin>.yaml` | take theirs | each claim's only YAML edit is flipping its own row |
| `src/<bin>/_rosetta/FUN_<rva>.cpp` (add/add) | prefer hand-written over stamped | workers occasionally rewrite a sibling .cpp; both versions are GREEN by construction, so either compiles to byte-identical bytes — but hand-written wins over the auto-stamper's `// [STAMPED]` header version |
| `decomp-notes/blocked/<bin>/<rva>_<sym>.md` | append-merge | post-mortems are additive — preserve both attempts separated by `---` |
| `decomp-notes/types/<bin>/<rva>.md` | take theirs | newer type discovery overwrites older |
| `include/<bin>/...`, `decomp-notes/idioms/`, any other path | escalate | shared interface / curator-only edits need a human |

Escalation payloads now capture per-path snapshots (ours/theirs/diff,
truncated to 8 KB each) so an offline reader can act without re-running
the merge.

If you broaden the classifier in code and want previously-escalated
claims to get another try, run:

```sh
python -m decomp_agents.retry_escalated --all-sessions
# or via the console-script alias:
decomp-agents-retry-escalated --all-sessions
```

That flips every `merge_status='conflict'` claim back to `'pending'`
and archives the existing escalation JSONs under
`output/merges/escalated/_retried/`. Re-run the orchestrator afterwards
to drain them.

## Limitations / future work

- **No human-in-the-loop merge resolution UI.** Truly-unresolvable
  conflicts (shared headers, curator-only files) still drop a JSON;
  `prompts/merge_resolver_system.md` is the placeholder for a future
  LLM resolver.
- **Header coupling not protected.** Workers are told not to edit
  shared headers, but nothing structurally prevents it. The first time
  it bites, add a pre-commit hook in meteor-decomp's `.git/hooks/`.
- **Single-binary per session.** Workers only chew through one
  `DECOMP_BINARY` per orchestrator run. To grind multiple binaries,
  run the orchestrator multiple times.
- **No automatic rate-limit backoff.** If the SDK hits a rate limit,
  workers will surface the error and release the claim with
  `outcome=error`. They keep retrying.
- **Subprocess-per-worker is heavier than asyncio tasks.** Switched
  from in-process tasks for true `cwd` isolation and clean signal
  handling. Cost: ~50MB RSS per worker on top of the SDK.

## Design notes

See `meteor-decomp/AGENTS.md` for the per-function discipline rules
agents follow (one-file scope, narrow YAML edits, the 13 canonical
fixes table).

The orchestrator's design mirrors `jayminwest/overstory` — multi-agent
with worktree isolation and a coordination database — adapted for
meteor-decomp's specific build/diff loop.

## License

AGPL-3.0-or-later, matching `meteor-decomp/`.
