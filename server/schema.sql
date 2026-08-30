CREATE TABLE IF NOT EXISTS rooms (
  id            TEXT PRIMARY KEY,        -- short room code, e.g. "amber-fox"
  sandbox_id    TEXT,                    -- Daytona sandbox id
  brief         TEXT,                    -- project description shown to joiners
  created_at    REAL
);

CREATE TABLE IF NOT EXISTS agents (
  id              TEXT PRIMARY KEY,      -- uuid
  room            TEXT NOT NULL,
  name            TEXT NOT NULL,         -- "Alex's Claude Code"
  kind            TEXT NOT NULL,         -- "claude-code" | "codex" | "sim"
  joined_at       REAL,
  last_seen       REAL,                  -- heartbeat, updated on EVERY tool call
  active          INTEGER DEFAULT 1,
  cursor_event_id INTEGER DEFAULT 0      -- last event this agent has been told about
);

CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room        TEXT NOT NULL,
  title       TEXT NOT NULL,
  description TEXT,
  status      TEXT NOT NULL,             -- open | claimed | done | blocked
  claimed_by  TEXT,                      -- agent id
  created_at  REAL,
  updated_at  REAL,
  suggested_files TEXT                   -- JSON list; used to suggest alternatives in lease denials
);

CREATE TABLE IF NOT EXISTS worklog (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id  INTEGER NOT NULL,
  agent_id TEXT,
  ts       REAL,
  note     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
  path        TEXT NOT NULL,
  room        TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  task_id     INTEGER,
  acquired_at REAL,
  expires_at  REAL,
  PRIMARY KEY (room, path)
);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  room      TEXT NOT NULL,
  ts        REAL,
  agent_id  TEXT,
  kind      TEXT NOT NULL,
  payload   TEXT                          -- JSON
);

CREATE INDEX IF NOT EXISTS events_room_id ON events(room, id);
CREATE INDEX IF NOT EXISTS agents_room_active ON agents(room, active);
