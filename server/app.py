"""Shared agent workspace — MCP server (Phase 0: join_room + get_board)."""
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import db, sandbox
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

DEFAULT_BRIEF = "FastAPI todo service at the workspace root (app/main.py). Tests: pytest -q"


def _room_of(conn, agent_id: str) -> tuple[str, str | None]:
    """(room, sandbox_id) for an agent, or a clear error."""
    r = conn.execute(
        "SELECT a.room, r.sandbox_id FROM agents a JOIN rooms r ON r.id=a.room WHERE a.id=?",
        (agent_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown agent_id {agent_id!r}; call join_room first")
    return r["room"], r["sandbox_id"]


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
        # INSERT OR IGNORE: two simultaneous joins to a new room yield exactly one row,
        # and only the winner (rowcount == 1) creates the sandbox.
        created = conn.execute(
            "INSERT OR IGNORE INTO rooms(id, sandbox_id, brief, created_at) VALUES (?,?,?,?)",
            (room, None, os.environ.get("DEFAULT_BRIEF", DEFAULT_BRIEF), t),
        ).rowcount == 1
    if created:
        try:
            sandbox_id = sandbox.create_for_room(room)
        except sandbox.SandboxError as e:
            with db.tx() as conn:  # let the next joiner retry creation
                conn.execute("DELETE FROM rooms WHERE id=? AND sandbox_id IS NULL", (room,))
            raise ValueError(f"could not create the room's sandbox: {e}") from e
        with db.tx() as conn:
            conn.execute("UPDATE rooms SET sandbox_id=? WHERE id=?", (sandbox_id, room))
    with db.tx() as conn:
        r = conn.execute("SELECT brief FROM rooms WHERE id=?", (room,)).fetchone()
        if r is None:
            raise ValueError("room is being created by another agent and failed; try join_room again")
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


@mcp.tool()
@board_delta
def list_files(agent_id: str, dir: str = ".") -> dict:
    """List files in a directory of the shared workspace (relative to its root)."""
    with db.tx() as conn:
        _, sandbox_id = _room_of(conn, agent_id)
    try:
        return {"dir": dir, "entries": sandbox.list_files(sandbox_id, dir)}
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
@board_delta
def read_file(agent_id: str, path: str) -> dict:
    """Read a file from the shared workspace. No lease needed for reads."""
    with db.tx() as conn:
        _, sandbox_id = _room_of(conn, agent_id)
    try:
        return {"path": path, "content": sandbox.read_file(sandbox_id, path)}
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
@board_delta
def write_file(agent_id: str, path: str, content: str) -> dict:
    """Write (create or overwrite) a file in the shared workspace."""
    with db.tx() as conn:
        room, sandbox_id = _room_of(conn, agent_id)
    try:
        size = sandbox.write_file(sandbox_id, path, content)
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e
    with db.tx() as conn:
        db.emit(conn, room, agent_id, "file_written", {"path": path, "bytes": size})
    return {"ok": True, "path": path, "bytes": size}


@mcp.tool()
@board_delta
def run(agent_id: str, command: str) -> dict:
    """Run a shell command in the shared workspace (cwd = workspace root).
    Returns stdout, stderr, exit_code. Use this to run the tests."""
    with db.tx() as conn:
        room, sandbox_id = _room_of(conn, agent_id)
    try:
        result = sandbox.run(sandbox_id, command)
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e
    with db.tx() as conn:
        db.emit(conn, room, agent_id, "command_run", {
            "command": command, "exit_code": result["exit_code"],
            "stdout": result["stdout"][:4000], "stderr": result["stderr"][:4000],
        })
    return result


# --------------------------------------------------------------------------
# plain HTTP: health + admin reset
# --------------------------------------------------------------------------

async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "tools": sorted(t.name for t in await mcp.list_tools())})


async def admin_reset(request: Request) -> JSONResponse:
    """Wipe a room's ledger and destroy its sandbox (next join re-seeds a fresh one)."""
    room = request.path_params["room"].strip().lower()
    with db.tx() as conn:
        r = conn.execute("SELECT sandbox_id FROM rooms WHERE id=?", (room,)).fetchone()
    if r and r["sandbox_id"]:
        try:
            sandbox.get(r["sandbox_id"]).delete()
        except Exception:
            pass  # best effort; a stale sandbox is harmless
        sandbox._cache.pop(r["sandbox_id"], None)
    with db.tx() as conn:
        conn.execute("DELETE FROM worklog WHERE task_id IN (SELECT id FROM tasks WHERE room=?)", (room,))
        for table in ("tasks", "leases", "events", "agents", "rooms"):
            conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'rooms' else 'room'}=?", (room,))
    return JSONResponse({"ok": True, "room": room})


mcp_app = mcp.http_app(path="/mcp", stateless_http=True, allowed_hosts=["*"])

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/admin/reset/{room}", admin_reset, methods=["POST"]),
        Mount("/", app=mcp_app),  # serves /mcp exactly, no trailing-slash redirect
    ],
    lifespan=mcp_app.lifespan,
)

db.init_schema()
