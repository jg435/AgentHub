"""Tasks, claims, leases, worklog, handoff. All mutations are conditional
UPDATE / INSERT-ON-CONFLICT statements whose rowcount decides the outcome —
never read-then-write. Every function here takes an open tx() connection.
"""
import json
import time
from datetime import datetime, timezone

from . import db
from .board import expire_leases

LEASE_TTL_S = 300.0


class Denied(Exception):
    """Raised inside a tx() to roll it back; carries the denial payload."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("reason") or "denied")
        self.payload = payload


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def agent_row(conn, agent_id: str):
    r = conn.execute(
        "SELECT a.id, a.room, a.name, a.active, r.sandbox_id "
        "FROM agents a JOIN rooms r ON r.id=a.room WHERE a.id=?", (agent_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown agent_id {agent_id!r}; call join_room first")
    return r


def task_row(conn, room: str, task_id: int):
    return conn.execute("SELECT * FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()


def worklog(conn, task_id: int) -> list[dict]:
    return db.rows(conn.execute(
        "SELECT w.id, w.ts, w.agent_id, a.name AS agent_name, w.note FROM worklog w "
        "LEFT JOIN agents a ON a.id=w.agent_id WHERE w.task_id=? ORDER BY w.id", (task_id,)))


def add_worklog(conn, task_id: int, agent_id: str | None, note: str) -> None:
    conn.execute("INSERT INTO worklog(task_id, agent_id, ts, note) VALUES (?,?,?,?)",
                 (task_id, agent_id, db.now(), note))


def claimed_task_id(conn, room: str, agent_id: str) -> int | None:
    r = conn.execute(
        "SELECT id FROM tasks WHERE room=? AND claimed_by=? AND status='claimed' ORDER BY updated_at DESC LIMIT 1",
        (room, agent_id)).fetchone()
    return r["id"] if r else None


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _ago(ts: float) -> str:
    s = max(0, int(db.now() - ts))
    return f"{s // 60}m ago" if s >= 60 else f"{s}s ago"


def _files(t) -> list[str]:
    try:
        return json.loads(t["suggested_files"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def create_task(conn, room: str, agent_id: str, title: str, description: str,
                suggested_files: list[str]) -> int:
    t = db.now()
    cur = conn.execute(
        "INSERT INTO tasks(room, title, description, status, claimed_by, created_at, updated_at, suggested_files) "
        "VALUES (?,?,?,'open',NULL,?,?,?)",
        (room, title, description, t, t, json.dumps(list(suggested_files or []))))
    db.emit(conn, room, agent_id, "task_created",
            {"task_id": cur.lastrowid, "title": title, "suggested_files": list(suggested_files or [])})
    return cur.lastrowid


def claim_task(conn, room: str, agent_id: str, task_id: int) -> dict:
    """Atomic: UPDATE ... WHERE status='open'; rowcount decides."""
    cur = conn.execute(
        "UPDATE tasks SET status='claimed', claimed_by=?, updated_at=? "
        "WHERE id=? AND room=? AND status='open'",
        (agent_id, db.now(), task_id, room))
    if cur.rowcount == 0:
        t = task_row(conn, room, task_id)
        if t is None:
            reason = f"task #{task_id} does not exist in this room"
        elif t["status"] == "claimed":
            holder = conn.execute("SELECT name FROM agents WHERE id=?", (t["claimed_by"],)).fetchone()
            who = holder["name"] if holder else t["claimed_by"]
            reason = (f"task #{task_id} \"{t['title']}\" is already claimed by \"{who}\". "
                      f"Pick another open task from the board.")
        else:
            reason = f"task #{task_id} \"{t['title']}\" is {t['status']}, not open"
        return {"granted": False, "reason": reason}
    t = task_row(conn, room, task_id)
    db.emit(conn, room, agent_id, "task_claimed", {"task_id": task_id, "title": t["title"]})
    return {"granted": True, "task": dict(t), "worklog": worklog(conn, task_id)}


def set_task_status(conn, room: str, agent_id: str, task_id: int, status: str, note: str) -> bool:
    """done/blocked only by the claimer; open (revert) only from blocked/claimed by claimer."""
    cur = conn.execute(
        "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND room=? AND claimed_by=? AND status IN ('claimed','blocked')",
        (status, db.now(), task_id, room, agent_id))
    if cur.rowcount == 0:
        return False
    add_worklog(conn, task_id, agent_id, f"[{status}] {note}")
    t = task_row(conn, room, task_id)
    db.emit(conn, room, agent_id, "task_done" if status == "done" else "update_posted",
            {"task_id": task_id, "title": t["title"], "status": status, "message": note})
    return True


# --------------------------------------------------------------------------
# leases
# --------------------------------------------------------------------------

def _denial_text(conn, room: str, path: str, holder, open_tasks: list) -> str:
    holder_task = task_row(conn, room, holder["task_id"]) if holder["task_id"] else None
    task_txt = (f",\nworking on task #{holder_task['id']} \"{holder_task['title']}\"." if holder_task else ".")
    lines = [
        f"DENIED: {path} is leased by \"{holder['name']}\" since {_hms(holder['acquired_at'])} "
        f"({_ago(holder['acquired_at'])}){task_txt}",
        "",
        "Options:",
    ]
    # an unclaimed task whose suggested files don't include the contested path
    alt = next((t for t in open_tasks if path not in _files(t)), None)
    if alt:
        files = _files(alt)
        touches = f" touches {', '.join(files)} only and" if files else ""
        lines.append(f"  - task #{alt['id']} \"{alt['title']}\"{touches} is unclaimed")
    else:
        lines.append("  - create_task for something that does not touch this file")
    lines.append("  - call wait_for_event to block until the lease frees")
    lines.append(f"  - the lease expires automatically at {_hms(holder['expires_at'])} if that agent goes silent")
    lines.append("")
    lines.append("Do not retry in a loop.")
    return "\n".join(lines)


def acquire_lease(conn, room: str, agent_id: str, paths: list[str], task_id: int | None) -> dict:
    """All-or-nothing. Raises Denied (rolling the tx back) if any path is held
    by someone else. INSERT ... ON CONFLICT DO UPDATE ... WHERE expired-or-mine
    means two simultaneous acquires produce exactly one winner."""
    # Ported from the Codex branch: a lease must belong to a task you have claimed,
    # so agents cannot lock files for work nobody can see on the board.
    if task_id is None or conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND room=? AND claimed_by=? AND status='claimed'",
            (task_id, room, agent_id)).fetchone() is None:
        raise Denied({"granted": False, "denials": [{
            "path": p, "held_by": None, "task_id": task_id,
            "message": (f"DENIED: you must claim a task before leasing {p}. Call claim_task on an "
                        f"open task from the board, then acquire_lease with that task_id.")} for p in paths]})
    expire_leases(conn, room)  # lazy expiry; emits lease_expired
    t = db.now()
    expires = t + LEASE_TTL_S
    denials = []
    for path in paths:
        cur = conn.execute(
            "INSERT INTO leases(path, room, agent_id, task_id, acquired_at, expires_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(room, path) DO UPDATE SET agent_id=excluded.agent_id, task_id=excluded.task_id, "
            "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at "
            "WHERE leases.expires_at < excluded.acquired_at OR leases.agent_id = excluded.agent_id",
            (path, room, agent_id, task_id, t, expires))
        if cur.rowcount == 0:
            holder = conn.execute(
                "SELECT l.path, l.task_id, l.acquired_at, l.expires_at, a.name FROM leases l "
                "JOIN agents a ON a.id=l.agent_id WHERE l.room=? AND l.path=?", (room, path)).fetchone()
            open_tasks = db.rows(conn.execute(
                "SELECT id, title, suggested_files FROM tasks WHERE room=? AND status='open' ORDER BY id", (room,)))
            denials.append({"path": path, "held_by": holder["name"], "task_id": holder["task_id"],
                            "expires_at": holder["expires_at"],
                            "message": _denial_text(conn, room, path, holder, open_tasks)})
    if denials:
        raise Denied({"granted": False, "denials": denials})
    for path in paths:
        db.emit(conn, room, agent_id, "lease_granted", {"path": path, "task_id": task_id, "expires_at": expires})
    return {"granted": True, "expires_at": expires, "paths": list(paths)}


def release_lease(conn, room: str, agent_id: str, paths: list[str]) -> dict:
    released, not_held = [], []
    for path in paths:
        cur = conn.execute("DELETE FROM leases WHERE room=? AND path=? AND agent_id=?", (room, path, agent_id))
        (released if cur.rowcount else not_held).append(path)
        if cur.rowcount:
            db.emit(conn, room, agent_id, "lease_released", {"path": path})
    return {"ok": True, "released": released, "not_held": not_held}


def release_all(conn, room: str, agent_id: str) -> list[str]:
    paths = [r["path"] for r in conn.execute(
        "DELETE FROM leases WHERE room=? AND agent_id=? RETURNING path", (room, agent_id)).fetchall()]
    for p in paths:
        db.emit(conn, room, agent_id, "lease_released", {"path": p})
    return paths


def check_and_renew_lease(conn, room: str, agent_id: str, path: str) -> dict | None:
    """For write_file: renew if this agent holds a live lease, else return a denial payload."""
    expire_leases(conn, room)
    now = db.now()
    cur = conn.execute(
        "UPDATE leases SET expires_at=? WHERE room=? AND path=? AND agent_id=? AND expires_at>=?",
        (now + LEASE_TTL_S, room, path, agent_id, now))
    if cur.rowcount:
        return None
    holder = conn.execute(
        "SELECT l.path, l.task_id, l.acquired_at, l.expires_at, a.name FROM leases l "
        "JOIN agents a ON a.id=l.agent_id WHERE l.room=? AND l.path=?", (room, path)).fetchone()
    if holder is None:
        msg = (f"DENIED: you do not hold a lease on {path}. Call acquire_lease(paths=[\"{path}\"], "
               f"task_id=<your task>) first, then write. Do not retry write_file without a lease.")
        return {"granted": False, "denials": [{"path": path, "held_by": None, "message": msg}]}
    open_tasks = db.rows(conn.execute(
        "SELECT id, title, suggested_files FROM tasks WHERE room=? AND status='open' ORDER BY id", (room,)))
    msg = _denial_text(conn, room, path, holder, open_tasks) + "\nCall acquire_lease first once it is free."
    return {"granted": False, "denials": [{"path": path, "held_by": holder["name"], "task_id": holder["task_id"],
                                           "expires_at": holder["expires_at"], "message": msg}]}


# --------------------------------------------------------------------------
# handoff
# --------------------------------------------------------------------------

def handoff(conn, room: str, agent_id: str, summary: str, next_steps: str, blockers: str) -> dict:
    released = release_all(conn, room, agent_id)
    conn.execute("UPDATE agents SET active=0 WHERE id=?", (agent_id,))
    note = f"[handoff] {summary} Next steps: {next_steps}" + (f" Blockers: {blockers}" if blockers else "")
    tasks = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE room=? AND claimed_by=? AND status='claimed'", (room, agent_id)).fetchall()]
    for tid in tasks:
        add_worklog(conn, tid, agent_id, note)
    db.emit(conn, room, agent_id, "handoff",
            {"summary": summary, "next_steps": next_steps, "blockers": blockers, "tasks": tasks})
    return {"ok": True, "released": released, "tasks_still_claimed": tasks}


# --------------------------------------------------------------------------
# wait_for_event (no transaction held open; polls every 500ms)
# --------------------------------------------------------------------------

def wait_for_event(agent_id: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, min(float(timeout_s), 60.0))
    while True:
        conn = db.connect()
        try:
            r = conn.execute(
                "SELECT (SELECT COALESCE(MAX(id),0) FROM events WHERE room=a.room) > a.cursor_event_id AS fresh "
                "FROM agents a WHERE a.id=?", (agent_id,)).fetchone()
        finally:
            conn.close()
        if r is None:
            raise ValueError(f"unknown agent_id {agent_id!r}; call join_room first")
        if r["fresh"]:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
