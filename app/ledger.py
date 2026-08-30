from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY,
  sandbox_id TEXT,
  brief TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  room TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  joined_at REAL,
  last_seen REAL,
  active INTEGER DEFAULT 1,
  cursor_event_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL,
  claimed_by TEXT,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS worklog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  agent_id TEXT,
  ts REAL,
  note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
  path TEXT NOT NULL,
  room TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  task_id INTEGER,
  acquired_at REAL,
  expires_at REAL,
  PRIMARY KEY (room, path)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room TEXT NOT NULL,
  ts REAL,
  agent_id TEXT,
  kind TEXT NOT NULL,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_room_id ON events(room, id);
CREATE INDEX IF NOT EXISTS idx_agents_room_active ON agents(room, active);
"""


class Ledger:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def now() -> float:
        return time.time()

    @staticmethod
    def rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    @staticmethod
    def emit(db: sqlite3.Connection, room: str, kind: str, agent_id: str | None,
             payload: dict[str, Any] | None = None) -> int:
        cursor = db.execute(
            "INSERT INTO events(room, ts, agent_id, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (room, time.time(), agent_id, kind, json.dumps(payload or {})),
        )
        return int(cursor.lastrowid)

    def create_room_if_missing(self, room: str, sandbox_id: str | None, brief: str) -> bool:
        """Atomically create a room. The boolean is false if another caller won."""
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO rooms(id, sandbox_id, brief, created_at) "
                "SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM rooms WHERE id = ?)",
                (room, sandbox_id, brief, self.now(), room),
            )
            return cursor.rowcount == 1

    def claim_task_conditionally(self, room: str, task_id: int, agent_id: str) -> bool:
        """Phase-2 primitive: a single conditional write, never read-then-write."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE tasks SET status='claimed', claimed_by=?, updated_at=? "
                "WHERE id=? AND room=? AND status='open'",
                (agent_id, self.now(), task_id, room),
            )
            return cursor.rowcount == 1

    def board(self, room: str) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as db:
            return {
                "tasks": self.rows(db.execute("SELECT * FROM tasks WHERE room=? ORDER BY id", (room,)).fetchall()),
                "leases": self.rows(db.execute("SELECT * FROM leases WHERE room=? ORDER BY path", (room,)).fetchall()),
                "agents": self.rows(db.execute("SELECT * FROM agents WHERE room=? ORDER BY joined_at", (room,)).fetchall()),
            }

