-- decomp-agents/schema/coordination.sql
--
-- Single SQLite database driving claim/release between N parallel
-- workers. Lives at output/coordination.sqlite by default.
--
-- All updates happen in IMMEDIATE transactions so concurrent worker
-- processes serialise cleanly. SQLite gives us OS-level atomicity
-- without needing a separate coordination service.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  worker_count  INTEGER NOT NULL,
  binary        TEXT NOT NULL,
  repo_path     TEXT NOT NULL,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS workers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    INTEGER NOT NULL REFERENCES sessions(id),
  agent_id      INTEGER NOT NULL,
  worktree_path TEXT NOT NULL,
  branch_prefix TEXT NOT NULL,
  pid           INTEGER,
  status        TEXT NOT NULL DEFAULT 'idle',   -- idle | claiming | working | merging | stopped | error
  started_at    TEXT NOT NULL,
  last_heartbeat TEXT,
  UNIQUE (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS claims (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    INTEGER NOT NULL REFERENCES sessions(id),
  worker_id     INTEGER NOT NULL REFERENCES workers(id),
  binary        TEXT NOT NULL,
  rva           INTEGER NOT NULL,
  symbol        TEXT NOT NULL,
  module        TEXT,
  size_bytes    INTEGER NOT NULL,
  claimed_at    TEXT NOT NULL,
  released_at   TEXT,
  outcome       TEXT,                            -- matched | passthrough | blocked | abandoned | error
  iterations    INTEGER NOT NULL DEFAULT 0,
  branch        TEXT,
  merge_status  TEXT                             -- pending | merged | conflict | reverted
);

-- One active claim per (binary, rva). Partial UNIQUE index is needed
-- because plain UNIQUE constraints treat NULL as distinct in SQLite,
-- so (b, rva, NULL) twice doesn't violate uniqueness. WorkQueue
-- also serialises via BEGIN IMMEDIATE for defence-in-depth.
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_active_unique
  ON claims(binary, rva) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS tool_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id     INTEGER NOT NULL REFERENCES workers(id),
  claim_id      INTEGER REFERENCES claims(id),
  ts            TEXT NOT NULL,
  tool_name     TEXT NOT NULL,
  phase         TEXT NOT NULL,                   -- pre | post
  input_json    TEXT,
  output_excerpt TEXT,
  duration_ms   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tool_events_worker ON tool_events(worker_id, ts);

CREATE TABLE IF NOT EXISTS merges (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id      INTEGER NOT NULL REFERENCES claims(id),
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  base_sha      TEXT,
  branch_sha    TEXT,
  result        TEXT,                            -- merged | conflict_resolved | conflict_escalated | reverted
  notes         TEXT
);
