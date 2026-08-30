"""Shared agent workspace — MCP server + plain-HTTP shim for the simulator."""
import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route

from . import db, ledger, sandbox
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


def _seed_tasks() -> list[dict]:
    """Tasks pre-created in every new room (seed/tasks.json). SEED_TASKS=0 disables."""
    if os.environ.get("SEED_TASKS", "1") == "0":
        return []
    p = sandbox.SEED_DIR / "tasks.json"
    return json.loads(p.read_text()) if p.exists() else []


def _room_of(conn, agent_id: str) -> tuple[str, str | None]:
    a = ledger.agent_row(conn, agent_id)
    return a["room"], a["sandbox_id"]


# ==========================================================================
# tool handlers — plain functions wrapped by @board_delta; registered with
# FastMCP below AND exposed over POST /tools/{name} for the simulator, so
# both paths exercise exactly this code.
# ==========================================================================

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
            for st in _seed_tasks():
                ledger.create_task(conn, room, None, st["title"], st.get("description", ""),
                                   st.get("suggested_files", []))
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


@board_delta
def get_board(agent_id: str) -> dict:
    """Current state of the room: tasks, live leases, agents, recent events."""
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        reap(conn, room)
        expire_leases(conn, room)
        b = board(conn, room)
        b["recent_events"] = recent_events(conn, room)
    return b


@board_delta
def create_task(agent_id: str, title: str, description: str = "", suggested_files: list[str] | None = None) -> dict:
    """Add a task to the board. suggested_files lists the paths it will likely touch."""
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        task_id = ledger.create_task(conn, room, agent_id, title, description, suggested_files or [])
    return {"task_id": task_id}


@board_delta
def claim_task(agent_id: str, task_id: int) -> dict:
    """Claim an open task. Atomic: first caller wins. On success returns the task and its
    worklog — the notes previous agents left on it are your inherited context."""
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        return ledger.claim_task(conn, room, agent_id, task_id)


@board_delta
def acquire_lease(agent_id: str, paths: list[str], task_id: int | None = None) -> dict:
    """Lease files before writing them. All-or-nothing. Leases last 300s and renew on
    every write. If denied, READ the denial: it names the holder and suggests other work."""
    try:
        with db.tx() as conn:
            room, _ = _room_of(conn, agent_id)
            for p in paths:
                sandbox.safe_path(p)
            return ledger.acquire_lease(conn, room, agent_id, list(paths), task_id)
    except ledger.Denied as d:
        with db.tx() as conn:
            room, _ = _room_of(conn, agent_id)
            for den in d.payload["denials"]:
                db.emit(conn, room, agent_id, "lease_denied",
                        {"path": den["path"], "held_by": den["held_by"], "task_id": task_id})
        return d.payload
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e


@board_delta
def release_lease(agent_id: str, paths: list[str]) -> dict:
    """Release leases you hold. Do this when the task is done."""
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        return ledger.release_lease(conn, room, agent_id, list(paths))


@board_delta
def list_files(agent_id: str, dir: str = ".") -> dict:
    """List files in a directory of the shared workspace (relative to its root)."""
    with db.tx() as conn:
        _, sandbox_id = _room_of(conn, agent_id)
    try:
        return {"dir": dir, "entries": sandbox.list_files(sandbox_id, dir)}
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e


@board_delta
def read_file(agent_id: str, path: str) -> dict:
    """Read a file from the shared workspace. No lease needed for reads."""
    with db.tx() as conn:
        _, sandbox_id = _room_of(conn, agent_id)
    try:
        return {"path": path, "content": sandbox.read_file(sandbox_id, path)}
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e


@board_delta
def write_file(agent_id: str, path: str, content: str) -> dict:
    """Write (create or overwrite) a file in the shared workspace. REQUIRES a live lease
    on the path held by you (acquire_lease first). Renews the lease on success."""
    try:
        sandbox.safe_path(path)
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e
    with db.tx() as conn:
        room, sandbox_id = _room_of(conn, agent_id)
        denial = ledger.check_and_renew_lease(conn, room, agent_id, path)
        if denial:
            db.emit(conn, room, agent_id, "lease_denied",
                    {"path": path, "held_by": denial["denials"][0].get("held_by"), "on": "write_file"})
            return denial
    try:
        size = sandbox.write_file(sandbox_id, path, content)
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e
    with db.tx() as conn:
        db.emit(conn, room, agent_id, "file_written", {"path": path, "bytes": size})
        tid = ledger.claimed_task_id(conn, room, agent_id)
        if tid:
            ledger.add_worklog(conn, tid, agent_id, f"wrote {path} ({size} bytes)")
    return {"ok": True, "path": path, "bytes": size}


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
        tid = ledger.claimed_task_id(conn, room, agent_id)
        if tid:
            ledger.add_worklog(conn, tid, agent_id, f"ran `{command}` -> exit {result['exit_code']}")
    return result


@board_delta
def commit_and_push(agent_id: str, message: str) -> dict:
    """Commit the workspace and push it to the room's git remote. Release your leases first.
    Files currently leased by OTHER agents are left out of the commit (their work is in
    progress). Returns the commit sha. If the remote moved, run `git pull --rebase` and retry."""
    if not message.strip():
        raise ValueError("message is required")
    with db.tx() as conn:
        room, sandbox_id = _room_of(conn, agent_id)
        expire_leases(conn, room)
        mine = [r["path"] for r in conn.execute(
            "SELECT path FROM leases WHERE room=? AND agent_id=?", (room, agent_id)).fetchall()]
        if mine:
            raise ValueError("DENIED: release your leases before pushing (you still hold: "
                             + ", ".join(mine) + "). Call release_lease, then commit_and_push again.")
        others = [r["path"] for r in conn.execute(
            "SELECT path FROM leases WHERE room=? AND agent_id!=?", (room, agent_id)).fetchall()]
    try:
        result = sandbox.commit_and_push(sandbox_id, message, exclude=others)
    except sandbox.SandboxError as e:
        raise ValueError(str(e)) from e
    with db.tx() as conn:
        db.emit(conn, room, agent_id, "pushed", {"sha": result["sha"], "branch": result["branch"],
                                                 "message": message, "excluded": others})
        tid = ledger.claimed_task_id(conn, room, agent_id)
        if tid:
            ledger.add_worklog(conn, tid, agent_id, f"pushed {result['sha']} to {result['branch']}: {message}")
    return {**result, "excluded_leased_by_others": others}


@board_delta
def log_work(agent_id: str, task_id: int, note: str) -> dict:
    """Append a note to a task's worklog. Write it for someone who wasn't here."""
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        if ledger.task_row(conn, room, task_id) is None:
            raise ValueError(f"task #{task_id} does not exist in this room")
        ledger.add_worklog(conn, task_id, agent_id, note)
    return {"ok": True, "task_id": task_id}


@board_delta
def post_update(agent_id: str, kind: str, message: str, task_id: int | None = None) -> dict:
    """Post a structured update. kind: note | blocked | done. With task_id, 'done' marks
    your claimed task done and 'blocked' marks it blocked."""
    if kind not in ("note", "blocked", "done"):
        raise ValueError("kind must be one of: note, blocked, done")
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        if task_id is None and kind in ("done", "blocked"):
            task_id = ledger.claimed_task_id(conn, room, agent_id)
        changed = False
        if task_id is not None and kind in ("done", "blocked"):
            changed = ledger.set_task_status(conn, room, agent_id, task_id, kind, message)
            if not changed:
                raise ValueError(f"task #{task_id} is not claimed by you, so you cannot mark it {kind}")
        else:
            db.emit(conn, room, agent_id, "update_posted", {"kind": kind, "message": message, "task_id": task_id})
            if task_id is not None:
                ledger.add_worklog(conn, task_id, agent_id, f"[{kind}] {message}")
    return {"ok": True, "task_id": task_id, "task_updated": changed}


@board_delta
def handoff(agent_id: str, summary: str, next_steps: str, blockers: str = "") -> dict:
    """Stop cleanly: releases all your leases, marks you inactive, attaches your summary and
    next steps to your claimed task so the next agent resumes with your context."""
    with db.tx() as conn:
        room, _ = _room_of(conn, agent_id)
        return ledger.handoff(conn, room, agent_id, summary, next_steps, blockers)


@board_delta
def wait_for_event(agent_id: str, timeout_s: float = 30) -> dict:
    """Block (max 60s) until something happens in the room. New events arrive in board_delta."""
    fresh = ledger.wait_for_event(agent_id, timeout_s)
    return {"timed_out": not fresh}


TOOLS = {f.__name__: f for f in (
    join_room, get_board, create_task, claim_task, acquire_lease, release_lease,
    list_files, read_file, write_file, run, log_work, post_update, handoff, wait_for_event,
    commit_and_push,
)}

mcp = FastMCP(
    "agenthub",
    instructions="Shared agent workspace. Call join_room first; every response carries "
                 "board_delta with what other agents did since your last call.",
)
for _fn in TOOLS.values():
    mcp.tool(_fn)


# --------------------------------------------------------------------------
# plain HTTP: /tools/{name} shim (same handlers), health, admin reset
# --------------------------------------------------------------------------

async def tool_http(request: Request) -> JSONResponse:
    fn = TOOLS.get(request.path_params["name"])
    if fn is None:
        return JSONResponse({"error": "no such tool"}, status_code=404)
    args = await request.json() if await request.body() else {}
    import anyio
    try:
        result = await anyio.to_thread.run_sync(lambda: fn(**args))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(result)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "tools": sorted(TOOLS)})


STATIC = Path(__file__).with_name("static")


async def api_board(request: Request) -> JSONResponse:
    """Spectator feed. Read-only; also triggers reaping so the display is truthful."""
    room = request.path_params["room"].strip().lower()
    with db.tx() as conn:
        r = conn.execute("SELECT brief, sandbox_id FROM rooms WHERE id=?", (room,)).fetchone()
        if r is None:
            return JSONResponse({"error": "no such room", "room": room}, status_code=404)
        reap(conn, room)
        expire_leases(conn, room)
        b = board(conn, room)
        names = {a["id"]: a["name"] for a in b["agents"]}
        for t in b["tasks"]:
            t["claimed_by_name"] = names.get(t["claimed_by"])
        for l in b["leases"]:
            l["holder_name"] = names.get(l["agent_id"])
        events = recent_events(conn, room, 60)
        for e in events:
            e["agent_name"] = names.get(e["agent_id"])
        last_run = next((e for e in reversed(events) if e["kind"] == "command_run"), None)
    return JSONResponse({"room": room, "brief": r["brief"], "sandbox_id": r["sandbox_id"], "now": db.now(),
                         **b, "events": events, "last_run": last_run})


async def ui(request: Request) -> HTMLResponse:
    return HTMLResponse((STATIC / "spectator.html").read_text())


async def admin_reset(request: Request) -> JSONResponse:
    """Wipe a room's ledger and destroy its sandbox (next join re-seeds a fresh one)."""
    room = request.path_params["room"].strip().lower()
    with db.tx() as conn:
        r = conn.execute("SELECT sandbox_id FROM rooms WHERE id=?", (room,)).fetchone()
    if r and r["sandbox_id"]:
        try:
            sandbox.destroy(r["sandbox_id"])
        except Exception:
            pass  # best effort; a stale sandbox is harmless
    with db.tx() as conn:
        conn.execute("DELETE FROM worklog WHERE task_id IN (SELECT id FROM tasks WHERE room=?)", (room,))
        for table in ("tasks", "leases", "events", "agents", "rooms"):
            conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'rooms' else 'room'}=?", (room,))
    return JSONResponse({"ok": True, "room": room})


mcp_app = mcp.http_app(path="/mcp", stateless_http=True, allowed_hosts=["*"])

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/api/board/{room}", api_board),
        Route("/ui/{room}", ui),
        Route("/", ui),
        Route("/tools/{name}", tool_http, methods=["POST"]),
        Route("/admin/reset/{room}", admin_reset, methods=["POST"]),
        Mount("/", app=mcp_app),  # serves /mcp exactly, no trailing-slash redirect
    ],
    lifespan=mcp_app.lifespan,
)

db.init_schema()
