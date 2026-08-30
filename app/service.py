from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import Any, Callable

from .config import PROTOCOL
from .daytona import DaytonaProvisioner
from .ledger import Ledger
from .sandbox import DaytonaSandboxGateway, workspace_path


def with_board_delta(handler: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Make event delivery mandatory for every workspace tool response.

    Handlers return their payload plus enough identity to locate the caller. This wrapper
    alone performs the heartbeat/cursor protocol, keeping it impossible to omit when a
    new tool is added.
    """
    @wraps(handler)
    def wrapped(self: "WorkspaceService", *args: Any, **kwargs: Any) -> dict[str, Any]:
        response = handler(self, *args, **kwargs)
        bound = inspect.signature(handler).bind(self, *args, **kwargs)
        agent_id = response.get("agent_id") or bound.arguments.get("agent_id")
        if not agent_id:
            raise ValueError("Tool responses must identify their caller for board_delta.")
        response["board_delta"] = self.consume_board_delta(str(agent_id))
        return response
    return wrapped


class WorkspaceService:
    def __init__(self, ledger: Ledger, provisioner: DaytonaProvisioner | None = None):
        self.ledger = ledger
        self.provisioner = provisioner or DaytonaProvisioner()

    def sandbox_for_agent(self, agent_id: str) -> tuple[str, str]:
        with self.ledger.connect() as db:
            row = db.execute(
                "SELECT agents.room, rooms.sandbox_id FROM agents JOIN rooms ON rooms.id=agents.room "
                "WHERE agents.id=?",
                (agent_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown agent_id. Call join_room first.")
        if not row["sandbox_id"]:
            raise RuntimeError("This room has no shared sandbox.")
        return str(row["room"]), str(row["sandbox_id"])

    def agent_context(self, agent_id: str) -> tuple[str, str]:
        with self.ledger.connect() as db:
            row = db.execute("SELECT room, name FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown agent_id. Call join_room first.")
        return str(row["room"]), str(row["name"])

    def consume_board_delta(self, agent_id: str) -> list[dict[str, Any]]:
        """Advance an agent cursor and return all events it had not been told about."""
        now = self.ledger.now()
        with self.ledger.transaction() as db:
            agent = db.execute("SELECT room, cursor_event_id FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                raise ValueError("Unknown agent_id. Call join_room first.")
            db.execute("UPDATE agents SET last_seen=?, active=1 WHERE id=?", (now, agent_id))
            events = db.execute(
                "SELECT * FROM events WHERE room=? AND id>? ORDER BY id",
                (agent["room"], agent["cursor_event_id"]),
            ).fetchall()
            if events:
                db.execute("UPDATE agents SET cursor_event_id=? WHERE id=?", (events[-1]["id"], agent_id))
            return [self._event(row) for row in events]

    @staticmethod
    def _event(row: Any) -> dict[str, Any]:
        event = dict(row)
        event["payload"] = json.loads(event["payload"] or "{}")
        return event

    def reap_inactive_agents(self, room: str) -> None:
        """Lazy heartbeat reaping: only invoked when the board is read."""
        cutoff = self.ledger.now() - 180
        with self.ledger.transaction() as db:
            stale = db.execute(
                "SELECT id FROM agents WHERE room=? AND active=1 AND last_seen<?",
                (room, cutoff),
            ).fetchall()
            for agent in stale:
                agent_id = agent["id"]
                db.execute("UPDATE agents SET active=0 WHERE id=? AND active=1", (agent_id,))
                db.execute("DELETE FROM leases WHERE room=? AND agent_id=?", (room, agent_id))
                reverted = db.execute(
                    "UPDATE tasks SET status='open', claimed_by=NULL, updated_at=? "
                    "WHERE room=? AND status='claimed' AND claimed_by=?",
                    (self.ledger.now(), room, agent_id),
                )
                if reverted.rowcount:
                    self.ledger.emit(db, room, "task_reverted", agent_id, {"reason": "heartbeat_timeout"})

    @with_board_delta
    def join_room(self, room: str, agent_name: str, agent_kind: str) -> dict[str, Any]:
        if not room or not agent_name or not agent_kind:
            raise ValueError("room, agent_name, and agent_kind are required.")

        # The immediate transaction serializes first joins, preventing duplicate sandboxes.
        with self.ledger.transaction() as db:
            existing = db.execute("SELECT * FROM rooms WHERE id=?", (room,)).fetchone()
            if existing is None:
                sandbox_id = self.provisioner.create_workspace(room)
                brief = "Shared agent workspace. Project files live in the Daytona sandbox."
                db.execute(
                    "INSERT INTO rooms(id, sandbox_id, brief, created_at) VALUES (?, ?, ?, ?)",
                    (room, sandbox_id, brief, self.ledger.now()),
                )
                room_row = db.execute("SELECT * FROM rooms WHERE id=?", (room,)).fetchone()
            else:
                room_row = existing
            agent_id = __import__("uuid").uuid4().hex
            now = self.ledger.now()
            db.execute(
                "INSERT INTO agents(id, room, name, kind, joined_at, last_seen, active, cursor_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 0)",
                (agent_id, room, agent_name, agent_kind, now, now),
            )
            self.ledger.emit(db, room, "joined", agent_id, {"name": agent_name, "kind": agent_kind})

        return {
            "agent_id": agent_id,
            "room": room,
            "brief": room_row["brief"],
            "resume_briefing": "This is a new shared workspace. No tasks or prior work have been recorded yet.",
            "board": self.ledger.board(room),
            "protocol": PROTOCOL,
        }

    @with_board_delta
    def get_board(self, agent_id: str) -> dict[str, Any]:
        with self.ledger.connect() as db:
            agent = db.execute("SELECT room FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None:
            raise ValueError("Unknown agent_id. Call join_room first.")
        room = str(agent["room"])
        self.reap_inactive_agents(room)
        with self.ledger.connect() as db:
            recent = db.execute("SELECT * FROM events WHERE room=? ORDER BY id DESC LIMIT 50", (room,)).fetchall()
        board = self.ledger.board(room)
        board["recent_events"] = [self._event(row) for row in reversed(recent)]
        return board

    @with_board_delta
    def create_task(self, agent_id: str, title: str, description: str = "",
                    suggested_files: list[str] | None = None) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("title is required.")
        room, _ = self.agent_context(agent_id)
        now = self.ledger.now()
        with self.ledger.transaction() as db:
            task_id = db.execute(
                "INSERT INTO tasks(room, title, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?)",
                (room, title, description, now, now),
            ).lastrowid
            self.ledger.emit(db, room, "task_created", agent_id, {
                "task_id": task_id, "title": title, "suggested_files": suggested_files or [],
            })
        return {"task_id": task_id}

    @with_board_delta
    def claim_task(self, agent_id: str, task_id: int) -> dict[str, Any]:
        room, _ = self.agent_context(agent_id)
        now = self.ledger.now()
        with self.ledger.transaction() as db:
            claimed = db.execute(
                "UPDATE tasks SET status='claimed', claimed_by=?, updated_at=? "
                "WHERE id=? AND room=? AND status='open'",
                (agent_id, now, task_id, room),
            )
            if claimed.rowcount == 0:
                row = db.execute("SELECT status FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()
                reason = "Task does not exist in this room." if row is None else f"Task is already {row['status']}."
                return {"granted": False, "reason": reason}
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            worklog = self.ledger.rows(db.execute("SELECT * FROM worklog WHERE task_id=? ORDER BY id", (task_id,)).fetchall())
            self.ledger.emit(db, room, "task_claimed", agent_id, {"task_id": task_id, "title": task["title"]})
        return {"granted": True, "task": task, "worklog": worklog}

    @with_board_delta
    def log_work(self, agent_id: str, task_id: int, note: str) -> dict[str, Any]:
        if not note.strip():
            raise ValueError("note is required.")
        room, _ = self.agent_context(agent_id)
        with self.ledger.transaction() as db:
            task = db.execute("SELECT id FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()
            if task is None:
                raise ValueError("Task does not exist in this room.")
            worklog_id = db.execute(
                "INSERT INTO worklog(task_id, agent_id, ts, note) VALUES (?, ?, ?, ?)",
                (task_id, agent_id, self.ledger.now(), note),
            ).lastrowid
        return {"ok": True, "worklog_id": worklog_id}

    @with_board_delta
    def list_files(self, agent_id: str, dir: str = ".") -> dict[str, Any]:
        _, sandbox_id = self.sandbox_for_agent(agent_id)
        path = workspace_path(dir, directory=True)
        return {"files": self.provisioner.list_files(sandbox_id, path)}

    @with_board_delta
    def read_file(self, agent_id: str, path: str) -> dict[str, Any]:
        _, sandbox_id = self.sandbox_for_agent(agent_id)
        sandbox_path = workspace_path(path)
        return {"path": path, "content": self.provisioner.read_file(sandbox_id, sandbox_path)}

    @with_board_delta
    def write_file(self, agent_id: str, path: str, content: str) -> dict[str, Any]:
        room, sandbox_id = self.sandbox_for_agent(agent_id)
        sandbox_path = workspace_path(path)
        self.provisioner.write_file(sandbox_id, sandbox_path, content)
        with self.ledger.transaction() as db:
            self.ledger.emit(db, room, "file_written", agent_id, {"path": path})
        return {"ok": True, "path": path}

    @with_board_delta
    def run(self, agent_id: str, command: str) -> dict[str, Any]:
        if not command.strip():
            raise ValueError("command is required.")
        room, sandbox_id = self.sandbox_for_agent(agent_id)
        result = self.provisioner.run(sandbox_id, command)
        with self.ledger.transaction() as db:
            self.ledger.emit(
                db,
                room,
                "command_run",
                agent_id,
                {
                    "command": command,
                    "exit_code": result["exit_code"],
                    "stdout": result["stdout"][:4000],
                    "stderr": result["stderr"][:4000],
                },
            )
        return result
