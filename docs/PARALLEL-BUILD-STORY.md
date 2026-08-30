# The accidental experiment: two agents, one spec, no shared ground truth

*Daytona HackSprint London, 30 Aug 2026.*

## What happened

While building AgentHub, the maintainer gave the **same spec** (`SPEC.md`, `TESTING.md`)
to two coding agents — Claude Code and Codex — and asked each to implement it, phase by
phase, in **separate git worktrees**. Neither agent knew the other existed. Each was told
"this ships today", each was reviewed only through its own transcript, and each reported
its phases as complete.

Roughly three hours later the maintainer asked one of them to compare the two branches.

| | `worktree-claude-phase0` (Claude Code) | `codex` (Codex) |
|---|---|---|
| Commits | 5 | 3 |
| Lines | ~2,600 | ~1,000 |
| Layout | `server/{app,board,ledger,sandbox,db}.py`, `seed/`, `sim/`, `tests/`, `demo/`, `scripts/` | `app/{main,service,ledger,sandbox}.py`, `tests/` |
| Own tests | 128 passed (simulator over HTTP, S1–S12, 50× race loops) | 6 passed (direct service calls) |
| Self-reported status | Phases 0–3 done, deployed | Phases 0–3 done |

Both agents did competent work. Both followed the spec's hard rules (conditional
`UPDATE … WHERE status='open'` for claims, a `board_delta` decorator, lazy lease expiry,
UTC epoch floats). They made **different, reasonable, incompatible choices** everywhere the
spec left room:

- schema: one added `tasks.suggested_files`; the other kept the spec's table verbatim
- protocol: one requires a claimed task before you can lease a file; the other doesn't
- lease atomicity: one used `INSERT … ON CONFLICT … WHERE expires_at < now` as the spec
  demanded; the other did `SELECT` then upsert inside a `BEGIN IMMEDIATE` (safe in
  practice, but the read-then-write pattern the spec explicitly forbids)
- test strategy: one built the simulator from `TESTING.md`; the other unit-tested the
  service class
- `resume_briefing`: one wrote prose; the other returned `#1 title (status), #2 …`
- MCP mounting: one wired FastMCP's lifespan into Starlette; the other mounted it inside
  FastAPI without it

That last one is the punchline. **Codex's branch, as committed, could not serve a single
MCP request** — `POST /mcp` returned 404 (double-mounted path), `POST /mcp/mcp` returned
500 (`StreamableHTTPSessionManager task group was not initialized`). Its six tests were
green because they never crossed the transport. It had "completed" Phase 0 whose only
acceptance criterion was *"both Claude Code and Codex connect and list the two tools"*.

Nobody was lying. Each agent's tests passed *on its own machine, against its own code, in
its own definition of done*. There was no shared place where "does it work" was a fact
rather than a claim.

## Why this is exactly the problem AgentHub exists for

The thesis of this project: **agents cannot collaborate without shared ground truth**, and
in software, ground truth is what happens when you run the code — in one place, observed by
everyone. Map the mechanisms onto what went wrong:

| Failure in the experiment | AgentHub mechanism that prevents it |
|---|---|
| Two agents built the same thing in full | **Task ledger + atomic claims.** The second agent's `join_room` returns a `resume_briefing`: *"Phase 0 is in flight, held by Jayesh's Claude Code."* Its `claim_task` is denied. It picks other work. |
| Incompatible schema / protocol decisions | **One workspace, one branch.** There is one `schema.sql`. The first agent to lease and write it wins; the second reads the file that exists rather than inventing a parallel one. |
| Both would have edited `app.py`, `ledger.py` | **File leases with a directive denial.** *"DENIED: server/app.py is leased by "Claude Code" since 11:42, working on task #1 … task #3 touches tests/ only and is unclaimed. Do not retry in a loop."* |
| "Tests pass" meant different things | **`run` executes in the shared sandbox.** `pytest -q` is one command, one result, in `command_run` events both agents receive via `board_delta`. A green suite that never touches the transport is visible to the *other* agent, who can call the endpoint. |
| Nobody knew what the other had decided | **Worklog on the task, not the agent.** `log_work("mounted FastMCP at /mcp with lifespan wired; /mcp/mcp is wrong")` is inherited by whoever touches that task next — including a session that starts after the first one dies. |
| Reconciling afterwards needs a human to merge | **There is no merge, by design.** Divergence never accumulates because writes are serialised by leases on a single branch. The reconciliation step doesn't exist. |

In git-branch workflows, discovering the divergence cost a full review pass and a
decision about which 1,000–2,600 lines to throw away. In a room, the cost is one denied
claim and one denied lease, each of which arrives with instructions for what to do instead.

## How we resolved it (and what the product would have us do)

The spec lists merge resolution as a non-goal, so we didn't merge. We did what the product
does: **pick a base, turn the delta into tasks.**

1. Base: the branch that was reachable, deployed, and exercised against real Daytona
   (`worktree-claude-phase0`).
2. Tasks for what the other branch did better:
   - require a claimed task before `acquire_lease` (Codex's stricter rule)
   - `list_files` returns a depth-2 tree
   - inject the sandbox gateway instead of a `SANDBOX_FAKE` env flag
3. Those tasks are executed **through AgentHub** — join, claim, lease, write, `run pytest`
   — so the ledger for that session is the record of the reconciliation.

## Lessons we're keeping

- **Definition of done must live outside the agent.** A test suite the agent wrote,
  passing in an environment the agent controls, is a claim. Acceptance was "another
  client on another machine lists the tools" — that check would have caught the 404 in
  thirty seconds.
- **Ambiguity is where divergence comes from, and it can't be spec'd away.** The spec was
  detailed and both agents still made a dozen incompatible calls. Coordination has to be
  a runtime mechanism, not a document.
- **Tests that skip the transport can be green while the product is unreachable.** Every
  scenario in `tests/` now goes over HTTP into the same handlers the MCP tools use.
- **The agent that judged was also a participant.** Claude Code reviewed both branches
  and reached the same verdict Codex reached when asked independently — but both had
  skin in the game. In a room, the sandbox is the judge.

## Evidence

- Branches: `worktree-claude-phase0` and `codex` in this repository.
- Codex's MCP endpoint failure, reproduced from a clean export of its branch:
  `POST /mcp` → 307 → `/mcp/` → 404; `POST /mcp/mcp` → 500
  `RuntimeError: FastMCP's StreamableHTTPSessionManager task group was not initialized`.
- Claude Code's branch: `uv run pytest -q` → 128 passed; S12 against a live Daytona sandbox
  → passed; Codex on a second machine listing all tools via `/mcp` over the public URL.
- Both agents' independent comparisons reached the same conclusion; Codex's is quoted in
  the project transcript.
