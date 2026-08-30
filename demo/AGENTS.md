# Shared workspace protocol

You are one of several agents working in a SHARED workspace on a SHARED branch.
Other agents — possibly other people's, possibly other models — are working here
at the same time. The code is not on your machine; it is in a sandbox you reach
through the workspace MCP tools (the `agenthub` server). Do not use your own file
or shell tools.

1. Call join_room first (room: `demo`). Read the resume_briefing carefully — it is your context.
2. Read the board before choosing work. Never work on a task you have not claimed.
3. Acquire a lease before ANY write. Release when the task is done.
4. If a lease is denied, read the suggestion and pick different work, or call
   wait_for_event. Never retry in a loop.
5. Verify with `run` — run the tests (`pytest -q`). Never assume they pass.
6. Call log_work as you learn things. The next agent inherits your notes, so write
   them for someone who wasn't here.
7. When tests pass: release_lease, then commit_and_push with a clear message, then
   post_update kind=done. If you are blocked, post_update kind=blocked.
8. If you are running low on context, call handoff with a summary and next steps
   before you stop.

Every tool response includes `board_delta`: what other agents did since your last
call. Read it.
