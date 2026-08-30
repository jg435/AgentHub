# Real-agent test — one PC, two terminals

## 0. Prereqs
- Server on `:8000` and ngrok are running — leave them.
- Both `claude` and `codex` have the `agenthub` MCP server registered (user scope).
  Check: `claude mcp list` / `codex mcp list`.

## 1. Projector view
Open in a browser:

    http://localhost:8000/ui/demo

Expect: 3 open tasks, no agents, "waiting for the first agent…".
(If not fresh: `scripts/smoke.sh` resets the room.)

## 2. Terminal 1 — Codex (the protagonist)

    cd ~/AgentHub/.claude/worktrees/claude-phase0/demo
    codex

Prompt:

    Join room "demo" on the agenthub MCP server and pick up a task from the board. Follow AGENTS.md.

Approve tool calls as they come up (or start with `codex --full-auto` to skip prompts).

## 3. Terminal 2 — Claude Code

    cd ~/AgentHub/.claude/worktrees/claude-phase0/demo
    claude

Prompt:

    Join room "demo" on the agenthub MCP server and pick up a task from the board. Follow CLAUDE.md.

## 4. Watch the UI
Expected: both names in the header → each claims a task → the second to touch
`app/main.py` gets a red **lease denied** → it moves to "Write tests for GET /health"
instead of retrying → `pytest -q` results show in "Last test run".

## 5. Handoff beat (needed for the demo)
Ctrl-C one agent mid-task. Either wait 3 min for the reaper, or have it call
`handoff` first. Then open a fresh session in the same terminal:

    Join room demo and resume whatever is in flight.

Its `resume_briefing` should mention the task and the other agent's notes.

## 6. Report back
Per TESTING.md, note for each agent:
- called `join_room` first, unprompted?
- acquired a lease before writing, or tried to write and got refused?
- on denial: routed to other work, or retried in a loop?
- called `log_work` without being told?

If an agent misbehaves, fix the denial text or the protocol file (`demo/CLAUDE.md`,
`demo/AGENTS.md`), not the agent. If agents ignore each other, check `board_delta` first.

## Before the real demo

    scripts/smoke.sh                # resets room `demo`, checks sandbox, UI, seed tasks
