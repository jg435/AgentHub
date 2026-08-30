# AgentHub — shared agent workspace (MCP)

One shared workspace, one branch, many agents. Coordination through file leases and a
task ledger, not git branches. See `SPEC.md`.

**Phase 0** — `join_room` + `get_board`, SQLite schema, deployable app.
**Phase 1** — Daytona sandbox per room seeded from `seed/`; `list_files`, `read_file`,
`write_file`, `run`.
**Phase 2** — tasks, atomic claims, leases (300s TTL, lazy expiry), lease-enforced
`write_file`, worklog, resume briefing, heartbeat reaping (180s), `handoff`,
`wait_for_event`, `log_work`, `post_update`.
**Phase 3** — spectator UI at `/ui/{room}` polling `GET /api/board/{room}` every 500 ms.
New rooms are seeded with the tasks in `seed/tasks.json` (`SEED_TASKS=0` disables).
**Combined** — ported from the parallel Codex build (see `docs/PARALLEL-BUILD-STORY.md`):
`acquire_lease` requires a task you have claimed; `list_files` returns a two-level tree.

## Tests

```sh
uv run pytest -q                       # S1–S11 + extras, fake sandbox, ~6s
RUN_DAYTONA=1 AGENTHUB_URL=http://localhost:8000 uv run pytest -q -k s12   # real Daytona
```

`tests/conftest.py` starts a server on a free port with a fresh SQLite file and
`SANDBOX_FAKE=1`. `sim/agent.py` is the scripted agent; it calls `POST /tools/{name}`,
which dispatches to the *same* handler functions the MCP tools use.

## Demo

Launch each agent from `demo/` — it holds the `CLAUDE.md` / `AGENTS.md` protocol
file (also seeded into the sandbox). Room code: `demo`.

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
