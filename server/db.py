"""SQLite access: one connection per call, WAL mode, explicit transactions.

All times are UTC epoch floats (time.time()).
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "./data/agenthub.db")
SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def now() -> float:
    return time.time()


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_schema() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # migration for DBs created before suggested_files existed
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "suggested_files" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN suggested_files TEXT")
    finally:
        conn.close()


@contextmanager
def tx():
    """BEGIN IMMEDIATE: concurrent writers serialise, so the conditional
    UPDATE/INSERT-ON-CONFLICT patterns inside are atomic w.r.t. each other."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def emit(conn: sqlite3.Connection, room: str, agent_id: str | None, kind: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO events(room, ts, agent_id, kind, payload) VALUES (?,?,?,?,?)",
        (room, now(), agent_id, kind, json.dumps(payload)),
    )
    return cur.lastrowid


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def event_dict(r) -> dict:
    d = dict(r)
    try:
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        pass
    return d
