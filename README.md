# Shared Agent Workspace — Phase 0

FastMCP server for a single shared agent workspace. Phase 1 exposes
`join_room`, `get_board`, `list_files`, `read_file`, `write_file`, and `run`
over streamable HTTP at `/mcp`. File reads, writes, and commands are proxied
through this server to the room's Daytona sandbox; MCP clients never connect to
Daytona directly.

## Run

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
export DAYTONA_API_KEY=...
export SEED_REPO_URL=https://github.com/your-org/seed-project.git # optional
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Set `DATABASE_PATH` to a persistent mounted location in production, for example
`/data/workspace.db`. `DAYTONA_API_KEY` is required when the first agent joins a
room; that join provisions and seeds the room's shared Daytona sandbox at
`/workspace`, then stores its id. Set `SEED_REPO_URL` to clone a seed repository;
without it, the service creates an empty `/workspace` directory.

Health checks use `GET /healthz`; MCP clients connect to `/mcp` using streamable HTTP.

## Deploy

The included `Dockerfile` runs the service on port 8000. Mount persistent storage at
`/data`, set `DATABASE_PATH=/data/workspace.db`, and provide `DAYTONA_API_KEY` through
the host's secret manager. Do not deploy it to stateless/serverless hosting.
