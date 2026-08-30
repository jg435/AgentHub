from __future__ import annotations

import os
from pathlib import Path


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/workspace.db"))
DAYTONA_TARGET = os.getenv("DAYTONA_TARGET", "us")
APP_NAME = "Shared Agent Workspace"
PROTOCOL = (
    "You are in a shared workspace. Call join_room first, read the board before "
    "choosing work, claim a task before working, and acquire a lease before every "
    "write. If a lease is denied, choose other work or wait_for_event; never retry "
    "in a loop. Verify through run and record useful context with log_work."
)

