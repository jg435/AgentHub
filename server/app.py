"""Shared agent workspace — MCP server (Phase 0: join_room + get_board)."""
import os
import uuid

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import db
from .board import board, board_delta, reap, expire_leases, recent_events, resume_briefing

PROTOCOL = (
    "You share this workspace and its single branch with other agents. Call join_room "
    "first and read the resume_briefing. Read the board before choosing work and never "
    "work on a task you have not claimed. Acquire a lease before ANY write and release it "
    "when done; if a lease is denied, take the suggested alternative or wait_for_event — "
    "never retry in a loop. Verify with run, log_work as you learn things (the next agent "
    "inherits your notes), post_update when finished or blocked, and call handoff before "
    "you stop. Every response includes board_delta: read it, it is what the others did."
)

mcp = FastMCP(
    "agenthub",
    instructions="Shared agent workspace. Call join_room first; every response carries "
                 "board_delta with what other agents did since your last call.",
)


@mcp.tool()
@board_delta
def join_room(room: str, agent_name: str, agent_kind: str) -> dict:
    """Join (or create) a shared workspace room. Call this FIRST. Returns your agent_id,
    the project brief, a prose resume_briefing of the current state, and the board."""
    room = room.strip().lower()
    agent_id = str(uuid.uuid4())
    t = db.now()
    with db.tx() as conn:
        # INSERT OR IGNORE: two simultaneous joins to a new room yield exactly one row.
        conn.execute(
            "INSERT OR IGNORE INTO rooms(id, sandbox_id, brief, created_at) VALUES (?,?,?,?)",
            (room, None, os.environ.get("DEFAULT_BRIEF"), t),
        )
        r = conn.execute("SELECT brief FROM rooms WHERE id=?", (room,)).fetchone()
        reap(conn, room)
        expire_leases(conn, room)
        b = board(conn, room)  # state as seen before this agent joined
        briefing = resume_briefing(conn, room, r["brief"], b)
        # Emit first, then create the agent with its cursor at the head, so the
        # joiner's own `joined` event is not echoed back to it.
        db.emit(conn, room, agent_id, "joined", {"name": agent_name, "kind": agent_kind})
        conn.execute(
            "INSERT INTO agents(id, room, name, kind, joined_at, last_seen, active, cursor_event_id) "
            "VALUES (?,?,?,?,?,?,1,(SELECT COALESCE(MAX(id),0) FROM events WHERE room=?))",
            (agent_id, room, agent_name, agent_kind, t, t, room),
        )
        b["agents"].append({"id": agent_id, "name": agent_name, "kind": agent_kind,
                            "joined_at": t, "last_seen": t, "active": 1})
    return {
        "agent_id": agent_id,
        "room": room,
        "brief": r["brief"],
        "resume_briefing": briefing,
        "board": b,
        "protocol": PROTOCOL,
    }


@mcp.tool()
@board_delta
def get_board(agent_id: str) -> dict:
    """Current state of the room: tasks, live leases, agents, recent events."""
    with db.tx() as conn:
        a = conn.execute("SELECT room FROM agents WHERE id=?", (agent_id,)).fetchone()
        if a is None:
            raise ValueError(f"unknown agent_id {agent_id!r}; call join_room first")
        room = a["room"]
        reap(conn, room)
        expire_leases(conn, room)
        b = board(conn, room)
        b["recent_events"] = recent_events(conn, room)
    return b


# --------------------------------------------------------------------------
# plain HTTP: health + admin reset
# --------------------------------------------------------------------------

async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "tools": ["join_room", "get_board"]})


async def admin_reset(request: Request) -> JSONResponse:
    room = request.path_params["room"].strip().lower()
    with db.tx() as conn:
        conn.execute("DELETE FROM worklog WHERE task_id IN (SELECT id FROM tasks WHERE room=?)", (room,))
        for table in ("tasks", "leases", "events", "agents", "rooms"):
            conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'rooms' else 'room'}=?", (room,))
    return JSONResponse({"ok": True, "room": room})


mcp_app = mcp.http_app(path="/", stateless_http=True, allowed_hosts=["*"])

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/admin/reset/{room}", admin_reset, methods=["POST"]),
        Mount("/mcp", app=mcp_app),
    ],
    lifespan=mcp_app.lifespan,
)

db.init_schema()
