"""board_delta decorator, heartbeat reaping, board queries, resume briefing."""
import functools
import inspect
import json
from typing import Any, Callable

from . import db

HEARTBEAT_TIMEOUT_S = 180.0
RECENT_EVENTS = 50


class AgentError(Exception):
    """Raised for unknown/inactive agents; surfaced to the caller as a tool error."""


# --------------------------------------------------------------------------
# board_delta wrapper
# --------------------------------------------------------------------------

def _advance_cursor(conn, agent_id: str) -> list[dict]:
    """Heartbeat + fetch unseen events + advance cursor. Must run inside tx()."""
    agent = conn.execute(
        "SELECT room, cursor_event_id FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    if agent is None:
        raise AgentError(f"unknown agent_id {agent_id!r}; call join_room first")
    conn.execute("UPDATE agents SET last_seen=? WHERE id=?", (db.now(), agent_id))
    events = [
        db.event_dict(r)
        for r in conn.execute(
            "SELECT id, ts, agent_id, kind, payload FROM events "
            "WHERE room=? AND id>? ORDER BY id",
            (agent["room"], agent["cursor_event_id"]),
        ).fetchall()
    ]
    if events:
        conn.execute(
            "UPDATE agents SET cursor_event_id=MAX(cursor_event_id, ?) WHERE id=?",
            (events[-1]["id"], agent_id),
        )
    return events


def board_delta(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Wrap a tool handler so EVERY response carries `board_delta`.

    After the handler returns, in one transaction:
      1. agents.last_seen = now()
      2. fetch events for the agent's room with id > cursor_event_id
      3. cursor_event_id = newest id
      4. attach events as response["board_delta"]

    agent_id comes from the call's `agent_id` argument, or — for join_room —
    from the returned dict. Handlers never touch cursor logic themselves.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        result = fn(*args, **kwargs)
        if not isinstance(result, dict):
            raise TypeError(f"{fn.__name__} must return a dict")
        bound = sig.bind_partial(*args, **kwargs)
        agent_id = bound.arguments.get("agent_id") or result.get("agent_id")
        with db.tx() as conn:
            result["board_delta"] = _advance_cursor(conn, agent_id) if agent_id else []
        return result

    return wrapper


# --------------------------------------------------------------------------
# heartbeat reaping
# --------------------------------------------------------------------------

def reap(conn, room: str) -> list[str]:
    """Mark silent agents inactive, release their leases, revert their claimed
    tasks to open (emit task_reverted). Worklog rows are never touched.
    Must run inside tx()."""
    cutoff = db.now() - HEARTBEAT_TIMEOUT_S
    reaped = [
        r["id"]
        for r in conn.execute(
            "UPDATE agents SET active=0 WHERE room=? AND active=1 AND last_seen<? RETURNING id",
            (room, cutoff),
        ).fetchall()
    ]
    for agent_id in reaped:
        released = db.rows(conn.execute(
            "DELETE FROM leases WHERE room=? AND agent_id=? RETURNING path", (room, agent_id)))
        for lease in released:
            db.emit(conn, room, agent_id, "lease_released",
                    {"path": lease["path"], "reason": "agent reaped"})
        reverted = db.rows(conn.execute(
            "UPDATE tasks SET status='open', claimed_by=NULL, updated_at=? "
            "WHERE room=? AND claimed_by=? AND status='claimed' RETURNING id, title",
            (db.now(), room, agent_id)))
        for t in reverted:
            db.emit(conn, room, agent_id, "task_reverted",
                    {"task_id": t["id"], "title": t["title"], "reason": "agent went silent"})
        db.emit(conn, room, agent_id, "left", {"reason": "heartbeat timeout"})
    return reaped


# --------------------------------------------------------------------------
# board queries
# --------------------------------------------------------------------------

def expire_leases(conn, room: str) -> None:
    """Lazy expiry: drop leases whose expires_at has passed, emitting lease_expired."""
    expired = db.rows(conn.execute(
        "DELETE FROM leases WHERE room=? AND expires_at<? RETURNING path, agent_id, task_id",
        (room, db.now())))
    for l in expired:
        db.emit(conn, room, l["agent_id"], "lease_expired",
                {"path": l["path"], "task_id": l["task_id"]})


def board(conn, room: str) -> dict:
    return {
        "tasks": db.rows(conn.execute(
            "SELECT id, title, description, status, claimed_by, created_at, updated_at "
            "FROM tasks WHERE room=? ORDER BY id", (room,))),
        "leases": db.rows(conn.execute(
            "SELECT path, agent_id, task_id, acquired_at, expires_at "
            "FROM leases WHERE room=? AND expires_at>=? ORDER BY path", (room, db.now()))),
        "agents": db.rows(conn.execute(
            "SELECT id, name, kind, joined_at, last_seen, active "
            "FROM agents WHERE room=? ORDER BY joined_at", (room,))),
    }


def recent_events(conn, room: str, limit: int = RECENT_EVENTS) -> list[dict]:
    evs = [db.event_dict(r) for r in conn.execute(
        "SELECT id, ts, agent_id, kind, payload FROM events WHERE room=? ORDER BY id DESC LIMIT ?",
        (room, limit)).fetchall()]
    evs.reverse()
    return evs


# --------------------------------------------------------------------------
# resume briefing — prose for someone with zero context
# --------------------------------------------------------------------------

def resume_briefing(conn, room: str, brief: str | None, b: dict) -> str:
    names = {a["id"]: a["name"] for a in b["agents"]}
    active = [a for a in b["agents"] if a["active"]]
    tasks = b["tasks"]
    parts = [f"Project: {brief or 'no brief has been set for this room yet'}."]

    if not tasks:
        parts.append("There are no tasks on the board yet. Create one with create_task "
                     "before doing any work, so others can see what you're doing.")
    else:
        done = [t for t in tasks if t["status"] == "done"]
        claimed = [t for t in tasks if t["status"] == "claimed"]
        blocked = [t for t in tasks if t["status"] == "blocked"]
        opened = [t for t in tasks if t["status"] == "open"]
        if done:
            parts.append("Done so far: " + "; ".join(f"#{t['id']} {t['title']}" for t in done) + ".")
        if claimed:
            parts.append("In flight: " + "; ".join(
                f"#{t['id']} {t['title']} (held by {names.get(t['claimed_by'], t['claimed_by'])})"
                for t in claimed) + ".")
        if blocked:
            parts.append("Blocked: " + "; ".join(
                f"#{t['id']} {t['title']} — {_last_note(conn, t['id']) or 'no reason recorded'}"
                for t in blocked) + ".")
        if opened:
            parts.append("Open and unclaimed: " + "; ".join(
                f"#{t['id']} {t['title']}" for t in opened) + ".")
            for t in opened:
                log = db.rows(conn.execute(
                    "SELECT agent_id, note FROM worklog WHERE task_id=? ORDER BY id", (t["id"],)))
                if log:
                    parts.append(
                        f"Task #{t['id']} has prior history you inherit: " + " | ".join(
                            f"{names.get(w['agent_id'], 'someone')}: {w['note']}" for w in log))

    handoff = conn.execute(
        "SELECT agent_id, payload FROM events WHERE room=? AND kind='handoff' "
        "ORDER BY id DESC LIMIT 1", (room,)).fetchone()
    if handoff:
        p = json.loads(handoff["payload"] or "{}")
        who = names.get(handoff["agent_id"], "the previous agent")
        parts.append(f"{who} handed off with: {p.get('summary', '')} "
                     f"Their stated next steps: {p.get('next_steps', 'none given')}."
                     + (f" Blockers: {p['blockers']}." if p.get("blockers") else ""))

    if active:
        parts.append("Currently active here: " + ", ".join(
            f"{a['name']} ({a['kind']})" for a in active) + ".")
    else:
        parts.append("Nobody else is active right now.")
    return " ".join(parts)


def _last_note(conn, task_id: int) -> str | None:
    r = conn.execute(
        "SELECT note FROM worklog WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return r["note"] if r else None
