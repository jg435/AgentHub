# Multi-Agent Coordination Platform — Build Spec

**Daytona HackSprint London, Sun 30 Aug 2026**
Kickoff 10:30 · Freeze 15:00 · Submission 16:00 (deadline 17:00)

---

## One-sentence pitch

Two people, on two laptops, running two different AI coding agents, ship one feature into one codebase without stepping on each other.

## The thesis (say this to judges)

Agents can't collaborate without shared ground truth. In software, ground truth is what happens when you run the code. Two agents on two laptops have different runtimes, different dependencies, different environments — so "tests pass" is a claim about one machine, not a fact about the project. One sandbox makes it a fact both agents observe.

---

## Architecture

```
Laptop A: Claude Code ─┐
                       ├─→ MCP server (hosted, streamable HTTP)
Laptop B: Codex ───────┘         │
                                 ├─→ Ledger (SQLite or in-memory)
                                 │     tasks, leases, events, agents
                                 │
                                 └─→ Daytona sandbox
                                       Volume: /workspace (the repo)
                                       run: tests, builds, commands
                                       preview URL: the running app

Spectator UI (browser, projector) ──→ polls ledger + sandbox state
```

**Critical:** all file reads and writes go through the MCP server so leases can be
enforced. Do NOT let agents connect to Daytona's MCP server directly — your server
proxies to the Daytona SDK.

**Transport:** streamable HTTP, not stdio. Both laptops must reach it over conference
wifi. Deploy early (Railway / Render / Fly / Vercel functions — whatever you can ship
in 20 minutes).

---

## Tool surface

Keep it small. Agents get confused past ~12 tools. Every tool response includes a
`board_delta` field (see "The polling problem" below).

| Tool | Args | Returns |
|---|---|---|
| `join_room` | `room`, `agent_name`, `agent_kind` | `agent_id`, project brief, board, **resume briefing** |
| `get_board` | — | tasks, leases, recent events |
| `create_task` | `title`, `description`, `suggested_files[]` | `task_id` |
| `claim_task` | `task_id` | granted / denied + reason |
| `acquire_lease` | `paths[]`, `task_id` | granted / denied + holder info |
| `release_lease` | `paths[]` | ok |
| `list_files` | `dir` | tree |
| `read_file` | `path` | contents |
| `write_file` | `path`, `content` | ok / **lease error** |
| `run` | `command` | stdout, stderr, exit_code (executes in sandbox) |
| `post_update` | `kind` (note/blocked/done), `message` | ok |
| `log_work` | `task_id`, `note` | ok (appends to task worklog) |
| `handoff` | `summary`, `next_steps`, `blockers` | releases leases, marks agent inactive |
| `wait_for_event` | `timeout_s` | events, or empty on timeout |
| `research` *(stretch)* | `objective` | cited excerpts, + written to shared cache |

### The lease denial message

This is your best demo moment. Make the error prose good, because the agent reads it
and has to decide what to do:

```
DENIED: src/auth.py is leased by claude-code (Alex's laptop) since 14:32:07,
working on task #4 "add password reset endpoint".
Suggestion: task #7 "write tests for /health" touches tests/ only and is unclaimed.
Call wait_for_event if you'd rather block until the lease frees.
```

Actionable, names the holder, offers an alternative. That's what makes the agent
gracefully route around instead of retrying or giving up.

---

## State machine

**Task:** `open` → `claimed(agent)` → `done` | `blocked`

Four states. Resist adding more. Claims are atomic server-side — first call wins,
second gets a denial.

**Lease:** held by one `agent_id`, on a list of paths, with a **5-minute TTL**.
Renewed implicitly on any write to a leased path. Expired leases are reclaimable.
TTL is non-negotiable — without it an agent that wanders off blocks the other forever.

**Event:** `{ts, agent_id, kind, payload}` where kind ∈
`joined | task_created | task_claimed | lease_granted | lease_denied | file_written |
command_run | update_posted | task_done`

The event log is the spectator feed and the demo narrative. Log everything.

---

## The three hard problems

### 1. Write contention

Naive shared filesystem = silent clobbering. Leasing solves it. `write_file` checks
for a valid lease and refuses without one. Roughly 40 lines of logic and it's the
single most important thing you build — it's what makes the demo prove the thesis.

### 2. Agents won't poll

MCP tools are pull-based. Neither agent spontaneously notices the other's work, and
if you rely on `wait_for_event` alone they'll ignore it and the demo dies.

**Solution: piggyback.** Every tool response carries a `board_delta` field listing
what changed since that agent's last call. The agent learns about the other agent as
a side effect of doing literally anything. Much more reliable than hoping for polite
polling. `wait_for_event` stays as the explicit blocking option.

Test this by 13:00. If agents aren't reacting to each other, nothing else matters.

### 3. Chatter loops

Two LLMs with a free-form channel will congratulate each other indefinitely. There is
no free-form chat. `post_update` takes a typed `kind`, and the board is the shared
memory. Structure kills the loop and keeps the spectator view readable.

---

## Stretch: shared research (Parallel)

**Only after leasing works. Build 14:30–15:00. Cut at 15:00 if unfinished.**

Parallel is a search API built for agents — natural-language objective in, ranked URLs
and token-dense compressed excerpts out. 200ms–3s, ~$0.001–0.005 per 10 results,
600 req/min. Marketed at coding agents specifically: live docs, changelogs, dev forums.

**Do not point agents at Parallel's own MCP server.** Same reasoning as file ops — if
Claude Code searches and learns something, it dies in Claude Code's context and Codex
re-searches it independently, possibly getting different excerpts. Two agents,
two pictures of the world.

Route it through your server instead:

1. `research(objective)` calls Parallel Search
2. Returns excerpts to the caller
3. Writes an event to the ledger: `research_done`, with the objective and sources
4. Caches to `/workspace/.research/<slug>.md` in the Volume
5. Near-duplicate objective from the other agent → serve the cache, log `cache_hit`

**Why this strengthens the pitch:** shared ground truth extends from "what happens when
we run the code" to "what's true about the world outside." Both agents reason from
identical cited sources. Paid for once.

**Keep it out of the 2-minute demo** unless the dogfood run falls through — there's no
room. Explain it in the written submission, where the caching argument has space to land.

---

## Session continuity (core — build with the ledger)

**The principle: context accrues to the task, not to the agent.**

An agent on another laptop and an agent in a later session are the same problem —
someone who wasn't there for the earlier reasoning. One mechanism solves both.

This mostly falls out of the architecture for free: the code is in the sandbox and the
work state is in the ledger, so nothing important lives in a context window. Four
additions make it real:

**1. Resume briefing.** `join_room` returns an orientation, not just raw board state:
what's in flight, what's blocked and why, what the previous agent last touched and what
it was about to do. Write it for someone with zero context — that's the reader.

**2. Explicit handoff.** `handoff(summary, next_steps, blockers)` releases all leases,
marks the agent inactive, writes a handoff note. Instruct agents in CLAUDE.md /
AGENTS.md to call it when they sense they're running low on context.

**3. Ungraceful death.** Sessions usually get cut off, not closed politely. Heartbeat
on every tool call; if an agent is silent for 3 minutes, expire its leases and revert
its claimed tasks to `open` — preserving all accumulated worklog context.

**4. Task worklog.** Append-only notes on each *task*. Every write, test run and
decision appends a line. A new agent claiming task #4 inherits everything previous
agents learned about task #4. This is what makes handoff feel like continuity rather
than a lossy summary.

**Pitch it as:** continuity across sessions and across agents — context limits, reset
windows, changing laptop, handing to a colleague, switching Claude Code to Codex
mid-feature. Every person in that room has lost an agent session this month.

---

## Seed project

Pre-scaffold tonight (legitimate boilerplate — no core functionality):

- Small FastAPI or Express service, 3–4 endpoints, existing test suite that passes
- Seeded into the Volume at sandbox creation
- Two tasks pre-written that touch *adjacent but overlapping* files, so the lease
  collision happens naturally in the demo rather than being staged

Small enough that agents finish tasks in ~90 seconds. Nobody watches a five-minute
demo of an agent thinking.

---

## Agent instructions

Identical protocol in `CLAUDE.md` and `AGENTS.md`, loaded into each agent's project
dir. Protocol adherence comes from the prompt, not the tools. Contents:

1. Call `join_room` first, always.
2. Read the board before choosing work. Never work on a task you haven't claimed.
3. Acquire a lease before any write. Release when the task is done.
4. Run tests via `run` — never assume, always verify.
5. If denied a lease, pick different work or `wait_for_event`. Do not retry in a loop.
6. Post an update when you finish or get blocked.

Keep it to one screen. Long instruction files get skimmed.

---

## Build order

| Time | Milestone | Go/no-go |
|---|---|---|
| 10:30–12:00 | MCP server deployed, `join_room` + `get_board` only. **Both** Claude Code and Codex connected and listing tools (`/mcp`). | If Codex won't connect by 12:00, fall back to two Claude Code instances and pitch "any MCP client". |
| 12:00–13:15 | Daytona sandbox + Volume, seed repo, `read_file` / `write_file` / `run` working. | Test results returning to both agents. |
| 13:15–14:30 | Ledger, tasks, claims, leases with TTL, `board_delta` piggyback, worklog + resume briefing + heartbeat. | Two agents visibly reacting to each other; a killed session resumes cleanly. |
| 14:30–15:00 | Spectator UI: task board, lease table, event feed, last test result. | Readable from 4 metres away. |
| **15:00** | **FEATURE FREEZE** | No exceptions. |
| 15:00–16:00 | Dogfood run, record backup video, write submission. | |
| 16:00 | Submit (deadline 17:00 — the hour is buffer, not build time). | |

### Priority order if you're behind

1. Two agents connected + shared sandbox execution ← without this there's no project
2. Lease conflict working ← without this there's no demo moment
3. Spectator UI ← without this the room can't see it
4. Task board / claiming ← can be faked with pre-seeded tasks
5. Dogfood run ← best-to-have
6. Shared research via Parallel ← first to cut

---

## The dogfood play

Judging weights "use of Codex — meaningful use of Codex in how the project was built"
at 25%. So:

- Build with Codex all day. Keep a log of what it wrote and what you prompted.
- In the 15:00–16:00 window, use your own platform to ship one small feature *of your
  platform* — Codex and Claude Code coordinating through your MCP server, on your repo.
- Screenshot the resulting event ledger. That artifact is your submission's strongest
  single piece of evidence.

Even a partial version is a legitimate claim. "We used our tool to build our tool"
is a sentence no other team will have.

**Make Codex the protagonist in the demo.** Your instinct will be to lead with Claude
Code. Don't.

---

## Two-minute demo script

Hard cut-off. Rehearse twice with a timer.

| Time | Beat |
|---|---|
| 0:00–0:15 | "Two agents, two laptops, one codebase. Today they'd overwrite each other." Spectator view already on screen — no setup, no terminal-opening. |
| 0:15–0:45 | Both agents claim tasks. Board updates live. Codex named first. |
| 0:45–1:15 | **The collision.** Codex tries to touch a leased file, gets denied, reads the suggestion, routes to different work. This is the whole pitch in one moment — narrate it. |
| 1:15–1:30 | **The handoff.** Close a terminal mid-task. A Codex session joins and picks up the same task with full context. No explanation needed — everyone watching has lost a session this month. |
| 1:30–1:40 | Tests run *in the sandbox*. One result, both agents see it. "Neither laptop ran this. That's the point." |
| 1:40–1:52 | Dogfood reveal: this feature was built by the agents, through this platform. Show the ledger. |
| 1:52–2:00 | Land it. Stop talking. |

**Everything on one screen.** If a judge has to look at two laptops, you've lost them.

No architecture slide. No team intro. No live typing.

---

## Submission (do at 15:30, not 16:55)

Round 1 is judged on the written submission, not the demo — at least two judges per
entry, top six advance. Most teams will write this at 16:55. That's your edge.

- [ ] Problem statement, two sentences, no jargon
- [ ] Why Daytona is required (ground truth + isolation — see thesis above)
- [ ] How Codex was used to build it, with specifics
- [ ] 90-second video, recorded early, works without wifi
- [ ] Live link + room code a judge can join in one click
- [ ] Repo link

---

## Tonight's pre-flight (~30 min, all legal)

- [ ] Daytona account + API key, `pip install daytona`, hello-world sandbox runs
- [ ] Confirm Codex MCP config path: `~/.codex/config.toml` (or project-scoped
      `.codex/config.toml` in a trusted dir); `codex mcp add` works
- [ ] Deploy target chosen and a "hello world" HTTP service already live on it
- [ ] Seed repo scaffolded with passing tests
- [ ] Check SWR engineering works — Staines→Waterloo→Shoreditch, ~1h15–1h30,
      **doors close 12:00**
