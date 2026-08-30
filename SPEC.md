# Implementation Spec — Shared Agent Workspace

Hand this to Claude Code **one phase at a time**. Each phase has acceptance criteria.
Do not start a phase until the previous one passes its criteria.

---

## What we're building

An MCP server that lets multiple coding agents — different vendors, different machines,
different sessions — work in **one shared workspace on one branch**, coordinating through
file leases and a task ledger instead of through git branches.

The workspace is a Daytona sandbox. Agents never have local copies. All file operations
go through this server so leases can be enforced.

**Non-goals:** auth, user accounts, permissions, merge resolution, multiple repos per
room, anything not required by the demo.

---

## Stack

- **Python 3.11+** with **FastMCP** (streamable HTTP transport, not stdio)
- **SQLite** for the ledger, single file on disk
- **Daytona Python SDK** (`pip install daytona`) for the sandbox
- **FastAPI** for the spectator UI endpoints (mount alongside MCP)
- Deploy to a host with persistent disk (Railway / Render / Fly). **Not Vercel** —
  serverless loses the SQLite file, and you will redeploy constantly.

TypeScript is an acceptable substitute throughout if the team prefers it.

---

## Data model

```sql
CREATE TABLE rooms (
  id            TEXT PRIMARY KEY,        -- short room code, e.g. "amber-fox"
  sandbox_id    TEXT,                    -- Daytona sandbox id
  brief         TEXT,                    -- project description shown to joiners
  created_at    REAL
);

CREATE TABLE agents (
  id              TEXT PRIMARY KEY,      -- uuid
  room            TEXT NOT NULL,
  name            TEXT NOT NULL,         -- "Alex's Claude Code"
  kind            TEXT NOT NULL,         -- "claude-code" | "codex" | "sim"
  joined_at       REAL,
  last_seen       REAL,                  -- heartbeat, updated on EVERY tool call
  active          INTEGER DEFAULT 1,
  cursor_event_id INTEGER DEFAULT 0      -- last event this agent has been told about
);

CREATE TABLE tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room        TEXT NOT NULL,
  title       TEXT NOT NULL,
  description TEXT,
  status      TEXT NOT NULL,             -- open | claimed | done | blocked
  claimed_by  TEXT,                      -- agent id
  created_at  REAL,
  updated_at  REAL
);

CREATE TABLE worklog (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id  INTEGER NOT NULL,
  agent_id TEXT,
  ts       REAL,
  note     TEXT NOT NULL
);

CREATE TABLE leases (
  path        TEXT NOT NULL,
  room        TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  task_id     INTEGER,
  acquired_at REAL,
  expires_at  REAL,
  PRIMARY KEY (room, path)
);

CREATE TABLE events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  room      TEXT NOT NULL,
  ts        REAL,
  agent_id  TEXT,
  kind      TEXT NOT NULL,
  payload   TEXT                          -- JSON
);
```

Event kinds: `joined | left | task_created | task_claimed | task_done | task_reverted |
lease_granted | lease_denied | lease_released | lease_expired | file_written |
command_run | update_posted | handoff | research_done | research_cache_hit`

---

## Critical correctness requirements

**These will be got wrong by default. Implement them exactly.**

### 1. Claims and leases must be atomic

Do NOT read-then-write. Use conditional updates and check the affected row count:

```python
cur = db.execute(
    "UPDATE tasks SET status='claimed', claimed_by=?, updated_at=? "
    "WHERE id=? AND room=? AND status='open'",
    (agent_id, now, task_id, room))
if cur.rowcount == 0:
    # someone else won the race, or task isn't open — return a denial
```

Leases use `INSERT ... ON CONFLICT DO UPDATE ... WHERE expires_at < now` so an expired
lease is reclaimable but a live one is not. Two simultaneous acquires must produce
exactly one winner.

### 2. Lease expiry is lazy, not a background job

Check `expires_at < now()` at read time and treat expired leases as absent. Emit a
`lease_expired` event when you observe one. No scheduler, no threads.

TTL: **300 seconds**, renewed on any successful write to a leased path.

### 3. Every tool response carries `board_delta`

This is how agents learn about each other. Non-negotiable — without it they work in
silence and the whole thing fails.

Every tool handler, before returning, must:
1. Update `agents.last_seen = now()`
2. Fetch events for this room with `id > agents.cursor_event_id`
3. Set `agents.cursor_event_id` to the newest event id
4. Attach those events to the response as `board_delta`

Wrap this in a decorator so it cannot be forgotten on a new tool.

### 4. Heartbeat reaping

On any board read, find agents in the room with `last_seen` older than **180 seconds**
and `active = 1`. For each: mark inactive, release their leases, revert their `claimed`
tasks to `open`, emit `task_reverted`. **Worklog rows are never deleted** — that
accumulated context is what the next agent inherits.

### 5. All times are UTC epoch floats

`time.time()`. No naive datetimes, no local timezones.

---

## Tool contract

All tools take `agent_id` except `join_room`. All responses include `board_delta`.

### `join_room(room, agent_name, agent_kind)`

Creates the room and its Daytona sandbox if the room doesn't exist. Returns:

```json
{
  "agent_id": "...",
  "room": "amber-fox",
  "brief": "FastAPI todo service. Tests: pytest -q",
  "resume_briefing": "<see below>",
  "board": { "tasks": [...], "leases": [...], "agents": [...] },
  "protocol": "<one-paragraph reminder of the rules>"
}
```

**`resume_briefing` is prose, generated for someone with zero context.** Not a data
dump. It must cover: what the project is, what's done, what's in flight and who has it,
what's blocked and why, and — if the previous agent left a handoff — their stated next
steps. Include the worklog of any task currently `open` that has prior history, because
that's the inherited context.

### `get_board()`
Returns `{ tasks, leases, agents, recent_events }`. Triggers heartbeat reaping.

### `create_task(title, description, suggested_files[])`
Returns `{ task_id }`. Emits `task_created`.

### `claim_task(task_id)`
Atomic. Returns `{ granted: true, task, worklog }` or
`{ granted: false, reason: "..." }`. On success the worklog is returned so the agent
inherits prior context immediately.

### `acquire_lease(paths[], task_id)`
All-or-nothing: if any path is unavailable, grant none. Returns
`{ granted: true, expires_at }` or `{ granted: false, denials: [...] }`.

**The denial message is product surface. Format exactly like this:**

```
DENIED: src/auth.py is leased by "Alex's Claude Code" since 14:32:07 (4m ago),
working on task #4 "add password reset endpoint".

Options:
  - task #7 "write tests for /health" touches tests/ only and is unclaimed
  - call wait_for_event to block until the lease frees
  - the lease expires automatically at 14:41:12 if that agent goes silent

Do not retry in a loop.
```

Name the holder, name their task, offer a concrete alternative, state the expiry.
This is what makes the agent route around instead of spinning.

### `release_lease(paths[])`
Only the holder may release. Emits `lease_released`.

### `list_files(dir)` / `read_file(path)`
Proxy to the Daytona sandbox filesystem. No lease required for reads.

### `write_file(path, content)`
**Requires a valid, unexpired lease held by this agent on this path.** Without one,
return the denial format above with a note to call `acquire_lease` first. On success:
renew the lease, emit `file_written`, append to the worklog of the agent's claimed task.

### `run(command)`
Executes in the room's sandbox. Returns `{ stdout, stderr, exit_code }`. Emits
`command_run` with the command and exit code. Truncate output to 4000 chars in the
event payload but return it in full to the caller.

### `log_work(task_id, note)`
Appends to the task worklog.

### `post_update(kind, message)`
`kind` ∈ `note | blocked | done`. Structured, not free chat.

### `handoff(summary, next_steps, blockers)`
Releases all this agent's leases, sets `active = 0`, emits `handoff` with the payload.
Claimed tasks stay claimed but the handoff note is attached to their worklog, so a
resuming agent sees it. Returns confirmation.

### `wait_for_event(timeout_s)`
Long-poll, max 60s. Returns as soon as any event newer than the agent's cursor appears,
or empty on timeout. Poll internally every 500ms — do not hold a DB transaction open.

### `research(objective)` — Phase 4, stretch
Calls Parallel Search. Returns excerpts, writes them to
`/workspace/.research/<slug>.md` in the sandbox, emits `research_done`. If a
near-duplicate objective exists in cache, serve it and emit `research_cache_hit`
instead.

---

## Sandbox integration

One Daytona sandbox per room, created on first `join_room`. Repo seeded at
`/workspace`. Store `sandbox_id` on the room and reuse it.

Path safety: reject any path that escapes `/workspace` after normalisation. Reject
absolute paths and `..` segments.

If the sandbox is unreachable, fail the tool call with a clear message rather than
hanging. Agents handle explicit errors far better than timeouts.

---

## Agent instruction files

Generate identical content into `CLAUDE.md` and `AGENTS.md` at the repo root:

```markdown
# Shared workspace protocol

You are one of several agents working in a SHARED workspace on a SHARED branch.
Other agents — possibly other people's, possibly other models — are working here
at the same time. The code is not on your machine; it is in a sandbox you reach
through the workspace MCP tools. Do not use your own file or shell tools.

1. Call join_room first. Read the resume_briefing carefully — it is your context.
2. Read the board before choosing work. Never work on a task you have not claimed.
3. Acquire a lease before ANY write. Release when the task is done.
4. If a lease is denied, read the suggestion and pick different work, or call
   wait_for_event. Never retry in a loop.
5. Verify with `run` — run the tests. Never assume they pass.
6. Call log_work as you learn things. The next agent inherits your notes, so write
   them for someone who wasn't here.
7. Call post_update when you finish or are blocked.
8. If you are running low on context, call handoff with a summary and next steps
   before you stop.
```

Keep it to one screen. Longer files get skimmed.

---

## Phases

### Phase 0 — skeleton (target: 12:00)
FastMCP server over streamable HTTP with `join_room` and `get_board` only. SQLite
schema created. Deployed and publicly reachable.

**Acceptance:** both Claude Code and Codex connect and list the two tools
(`/mcp` in each). Two different machines. This is the go/no-go for the whole project.

### Phase 1 — sandbox (target: 13:15)
Daytona sandbox per room, seeded repo, `list_files`, `read_file`, `write_file`
(no lease check yet), `run`.

**Acceptance:** an agent reads a file, edits it, runs the test suite, and gets real
output. A second agent on another machine sees the edit.

### Phase 2 — ledger (target: 14:30)
Tasks, atomic claims, leases with TTL, `board_delta` decorator, worklog, resume
briefing, heartbeat reaping, `handoff`, `wait_for_event`. `write_file` now enforces
leases.

**Acceptance:** all simulation scenarios in TESTING.md pass. Two real agents visibly
react to each other, and a killed session is resumed cleanly by a different agent.

### Phase 3 — spectator UI (target: 15:00)
Single page, polls `GET /api/board/{room}` every 500ms. Shows: task board by status,
lease table with holder and countdown, live event feed, last test result with exit code.

**Acceptance:** readable on a projector from four metres. No interaction required —
it is a display, not an app.

### Phase 4 — research (stretch, cut at 15:00)
Parallel Search integration per the tool contract above.

---

## Freeze at 15:00

No features after 15:00. The remaining hour is the dogfood run, the backup video, and
the written submission.
