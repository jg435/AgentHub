from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastmcp import FastMCP

from .config import APP_NAME, DATABASE_PATH
from .ledger import Ledger
from .service import WorkspaceService


def create_app(service: WorkspaceService | None = None) -> FastAPI:
    ledger = service.ledger if service else Ledger(DATABASE_PATH)
    workspace = service or WorkspaceService(ledger)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ledger.initialize()
        yield

    app = FastAPI(title=APP_NAME, lifespan=lifespan)
    mcp = FastMCP(APP_NAME)

    @mcp.tool()
    def join_room(room: str, agent_name: str, agent_kind: str) -> dict[str, Any]:
        """Join a shared room, creating its Daytona workspace on the first join."""
        return workspace.join_room(room, agent_name, agent_kind)

    @mcp.tool()
    def get_board(agent_id: str) -> dict[str, Any]:
        """Read the shared task board, leases, agents, and recent events."""
        return workspace.get_board(agent_id)

    @mcp.tool()
    def list_files(agent_id: str, dir: str = ".") -> dict[str, Any]:
        """List files in the shared Daytona workspace."""
        return workspace.list_files(agent_id, dir)

    @mcp.tool()
    def read_file(agent_id: str, path: str) -> dict[str, Any]:
        """Read a UTF-8 file from the shared Daytona workspace."""
        return workspace.read_file(agent_id, path)

    @mcp.tool()
    def write_file(agent_id: str, path: str, content: str) -> dict[str, Any]:
        """Write a UTF-8 file to the shared workspace. Leases begin in Phase 2."""
        return workspace.write_file(agent_id, path, content)

    @mcp.tool()
    def run(agent_id: str, command: str) -> dict[str, Any]:
        """Run a command in the room's shared Daytona workspace."""
        return workspace.run(agent_id, command)

    @mcp.tool()
    def create_task(agent_id: str, title: str, description: str = "",
                    suggested_files: list[str] | None = None) -> dict[str, Any]:
        """Create an open task on the shared board."""
        return workspace.create_task(agent_id, title, description, suggested_files)

    @mcp.tool()
    def claim_task(agent_id: str, task_id: int) -> dict[str, Any]:
        """Atomically claim an open task."""
        return workspace.claim_task(agent_id, task_id)

    @mcp.tool()
    def log_work(agent_id: str, task_id: int, note: str) -> dict[str, Any]:
        """Append inherited context to a task's worklog."""
        return workspace.log_work(agent_id, task_id, note)

    @mcp.tool()
    def acquire_lease(agent_id: str, paths: list[str], task_id: int) -> dict[str, Any]:
        """Atomically acquire all requested file leases for a claimed task."""
        return workspace.acquire_lease(agent_id, paths, task_id)

    @mcp.tool()
    def release_lease(agent_id: str, paths: list[str]) -> dict[str, Any]:
        """Release leases held by this agent."""
        return workspace.release_lease(agent_id, paths)

    @mcp.tool()
    def post_update(agent_id: str, kind: str, message: str) -> dict[str, Any]:
        """Post a structured note, blocked update, or done update."""
        return workspace.post_update(agent_id, kind, message)

    @mcp.tool()
    def handoff(agent_id: str, summary: str, next_steps: str, blockers: str = "") -> dict[str, Any]:
        """Release leases and leave context for a successor."""
        return workspace.handoff(agent_id, summary, next_steps, blockers)

    @mcp.tool()
    def wait_for_event(agent_id: str, timeout_s: int = 60) -> dict[str, Any]:
        """Wait for new room events for up to 60 seconds."""
        return workspace.wait_for_event(agent_id, timeout_s)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/board/{room}")
    def spectator_data(room: str) -> dict[str, Any]:
        """Read-only board data for the spectator display."""
        return workspace.spectator_board(room)

    @app.get("/spectator/{room}", response_class=HTMLResponse)
    def spectator(room: str) -> str:
        return f'''<!doctype html><title>AgentHub · {room}</title><style>body{{font:18px system-ui;background:#10131a;color:#eef;margin:0;padding:28px}}h1{{margin:0 0 18px}}main{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}section{{background:#1a2030;padding:16px;border-radius:12px}}pre{{white-space:pre-wrap;max-height:55vh;overflow:auto}}.open{{color:#8dd}}.claimed{{color:#fd8}}.done{{color:#8f8}}</style><h1>AgentHub · {room}</h1><main><section><h2>Tasks</h2><div id="tasks"></div></section><section><h2>Leases</h2><div id="leases"></div><h2>Last command</h2><pre id="command">None</pre></section><section style="grid-column:1/-1"><h2>Live event feed</h2><pre id="events"></pre></section></main><script>const esc=s=>String(s??'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));async function refresh(){{let b=await fetch('/api/board/{room}').then(r=>r.json());tasks.innerHTML=b.tasks.map(t=>`<p class="${{t.status}}">#${{t.id}} · ${{esc(t.title)}} — <b>${{t.status}}</b></p>`).join('')||'No tasks';leases.innerHTML=b.leases.map(l=>`<p>${{esc(l.path)}} · expires ${{new Date(l.expires_at*1000).toLocaleTimeString()}}</p>`).join('')||'No active leases';command.textContent=b.last_command?JSON.stringify(b.last_command.payload,null,2):'None';events.textContent=b.recent_events.map(e=>`${{new Date(e.ts*1000).toLocaleTimeString()}}  ${{e.kind}}  ${{JSON.stringify(e.payload)}}`).join('\n')}}refresh();setInterval(refresh,500)</script>'''

    @app.get("/")
    def index() -> dict[str, str]:
        return {"name": APP_NAME, "mcp": "/mcp", "health": "/healthz"}

    # FastMCP's ASGI app uses streamable HTTP; mount it where remote clients expect it.
    app.mount("/mcp", mcp.http_app(transport="streamable-http"))
    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
