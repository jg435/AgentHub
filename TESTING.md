# Testing Plan

## The core problem

You cannot debug this with real agent sessions. Launching Claude Code, waiting for it to
reason, hoping it calls the right tool, then reading the transcript is a 3–10 minute
loop with nondeterministic results. You have six hours. That loop will eat all of them.

**Build a simulated agent first.** It is a plain HTTP client that calls the same tools
in a scripted order. Coordination bugs then surface in milliseconds, deterministically.

Real agents are for confirming the protocol is followable — nothing else. Run them
twice: once at the end of Phase 2, once at rehearsal.

---

## The simulator

`sim/agent.py` — no LLM, no MCP client library, just HTTP calls to the same endpoints.

```python
class SimAgent:
    def __init__(self, base_url, room, name, kind="sim"):
        self.base, self.room, self.name, self.kind = base_url, room, name, kind
        self.agent_id = None
        self.seen_events = []

    def call(self, tool, **args):
        """POST to the tool endpoint. Collect board_delta from every response."""
        if self.agent_id:
            args["agent_id"] = self.agent_id
        r = requests.post(f"{self.base}/tools/{tool}", json=args, timeout=30)
        r.raise_for_status()
        data = r.json()
        self.seen_events.extend(data.get("board_delta", []))
        return data

    def join(self):
        d = self.call("join_room", room=self.room,
                      agent_name=self.name, agent_kind=self.kind)
        self.agent_id = d["agent_id"]
        return d

    def go_silent(self):
        """Stop calling. Used to test heartbeat reaping."""
        pass
```

Expose the MCP tools over plain HTTP POST as well as MCP, or call the MCP HTTP endpoint
directly — either is fine, but the simulator must exercise **the same handler code** as
the real agents. If it tests a parallel code path it tests nothing.

---

## Scenarios (write these as pytest tests)

### S1 — two agents see each other
A and B join. A creates a task. B calls `get_board`.
**Assert:** B's `board_delta` contains `task_created`. B's board shows the task.

### S2 — lease collision
A and B join. A claims task 1, acquires a lease on `src/auth.py`. B claims task 2,
attempts a lease on `src/auth.py`.
**Assert:** B is denied. The denial names A by display name, names A's task, and
suggests an alternative. A `lease_denied` event exists.

### S3 — claim race
A and B call `claim_task(1)` concurrently (threads, no sleep between).
**Assert:** exactly one gets `granted: true`. Task status is `claimed`, `claimed_by` is
the winner. Run this 50 times in a loop — race bugs are intermittent by nature.

### S4 — lease race
Same, for `acquire_lease` on the same path. 50 iterations.
**Assert:** exactly one holder, every time.

### S5 — write without lease
A claims a task and calls `write_file` without acquiring a lease.
**Assert:** refused, with a message telling it to call `acquire_lease`. File unchanged
in the sandbox.

### S6 — lease expiry
A acquires a lease. Force-age it (set `expires_at` in the past directly in SQLite).
B attempts the same path.
**Assert:** B is granted. A `lease_expired` event exists.

### S7 — heartbeat reaping and resume
A joins, claims task 1, acquires leases, calls `log_work` three times, then goes silent.
Age its `last_seen` past 180s. B joins.
**Assert:** A is inactive. A's leases are gone. Task 1 is back to `open`. **A's three
worklog notes still exist.** B's `resume_briefing` mentions the task and A's notes.

This is the session-handoff demo beat. If S7 passes, that beat works.

### S8 — graceful handoff
A claims a task, logs work, calls `handoff(summary, next_steps, blockers)`. B joins.
**Assert:** A's leases released, A inactive, task still claimed, B's `resume_briefing`
contains A's stated next steps.

### S9 — worklog inheritance
A works task 1 and logs notes. A is reaped. B claims task 1.
**Assert:** `claim_task` response includes A's worklog.

### S10 — board_delta on every tool
For each tool in the contract: A does something, B calls that tool.
**Assert:** every response has a `board_delta` key. Parametrise over the tool list so a
newly added tool fails this test until wired up.

### S11 — path escape
Attempt `write_file("../../etc/passwd")`, `write_file("/etc/passwd")`,
`write_file("src/../../x")`.
**Assert:** all rejected.

### S12 — sandbox is shared ground truth
A writes a failing test via the tools. B calls `run("pytest -q")`.
**Assert:** B sees the failure A caused. Neither machine ran anything locally.

This is the thesis, as an assertion.

---

## Test order

S3, S4, S10 first — they're the ones most likely to be silently broken, and they're
cheapest to check. Then S2, S5, S6 (leasing). Then S7, S8, S9 (continuity). Then
S1, S11, S12.

Run the whole suite against a fresh SQLite file each time. Add a
`POST /admin/reset/{room}` endpoint — you will want it constantly, and again at
rehearsal between demo runs.

---

## Sandbox tests

Mock the Daytona client for everything above except S12 — you don't want 50 sandbox
spin-ups in your unit loop, and sandbox latency will make the race tests flaky for the
wrong reasons.

Keep one integration test that actually hits Daytona: create a sandbox, write a file,
read it back, run a command, assert the exit code. Run it after any change to the
sandbox layer, not on every iteration.

---

## Real-agent checks

Only after the simulation suite is green. Two runs, maximum.

**Run 1 — protocol adherence.** One Claude Code session, one Codex session, one task
each, files that overlap. Watch for:
- Do they call `join_room` first, unprompted?
- Do they acquire a lease before writing, or do they try to write and get refused?
- On denial, do they route to other work, or retry in a loop?
- Do they call `log_work` without being told again?

If an agent misbehaves, **fix the instruction file or the tool response text, not the
agent.** The denial message and the protocol file are the two levers. If agents ignore
each other entirely, `board_delta` isn't reaching them — check that first.

**Run 2 — full rehearsal.** The actual demo, timed, twice.

---

## Pre-demo smoke script

Run this immediately before you present. Five checks, under 60 seconds:

1. `POST /admin/reset/{room}` — clean room
2. Sandbox reachable: `run("pytest -q")` returns a real exit code
3. Spectator UI loads and shows an empty board
4. Both laptops' agents connect and appear in the agent list
5. Seed tasks present

If any fail, play the backup video. Do not debug on stage.

---

## Failure modes to watch for

**Agents ignore each other.** `board_delta` missing or cursor not advancing. Highest
priority bug — nothing works without it.

**Agent retries a denied lease in a loop.** The denial text isn't directive enough.
Add "Do not retry in a loop" explicitly, and name a specific alternative task.

**Both agents claim the same task.** Read-then-write instead of a conditional update.
S3 catches this.

**Lease held forever.** TTL not enforced at read time, or the write path renews without
bound. S6 catches it.

**Resume briefing is a data dump.** It's prose for a stranger, not a JSON serialisation.
Judge this by reading it yourself and asking whether you could pick up the work from it.
