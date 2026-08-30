# AgentHub — shared agent workspace (MCP)

One shared workspace, one branch, many agents. Coordination through file leases and a
task ledger, not git branches. See `SPEC.md`.

**Phase 0** — `join_room` + `get_board`, SQLite schema, deployable app.
**Phase 1** — Daytona sandbox per room seeded from `seed/`; `list_files`, `read_file`,
`write_file` (no lease check yet), `run`.

## Run locally

```sh
uv sync
cp .env.example .env   # add DAYTONA_API_KEY
uv run uvicorn server.app:app --port 8000
curl localhost:8000/health
```

MCP endpoint: `http://localhost:8000/mcp` (streamable HTTP, stateless).
`POST /admin/reset/{room}` wipes a room's ledger and destroys its sandbox.

## Connect an agent

Claude Code:
```sh
claude mcp add --transport http agenthub https://<host>/mcp
```
Codex:
```sh
codex mcp add agenthub --url https://<host>/mcp
```
Then `/mcp` in either client should list `join_room` and `get_board`.

## Deploy

`Dockerfile` runs uvicorn on `$PORT`. SQLite lives at `$DB_PATH` (default
`/data/agenthub.db`) — mount a persistent volume at `/data`. Optional
`DEFAULT_BRIEF` env var seeds the brief for newly created rooms.

## Layout

```
server/schema.sql  # ledger schema, verbatim from SPEC.md
server/db.py       # sqlite: WAL, BEGIN IMMEDIATE tx(), emit()
server/board.py    # @board_delta wrapper, heartbeat reaping, lazy lease expiry, briefing
server/app.py      # FastMCP tools + Starlette app (/health, /admin/reset, /mcp)
```
