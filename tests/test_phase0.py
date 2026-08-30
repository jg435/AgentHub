from __future__ import annotations

from pathlib import Path

from app.ledger import Ledger
from app.service import WorkspaceService


class FakeDaytona:
    def __init__(self) -> None:
        self.rooms: list[str] = []

    def create_workspace(self, room: str) -> str:
        self.rooms.append(room)
        return f"sandbox-{room}"


class FakeSandbox(FakeDaytona):
    def __init__(self) -> None:
        super().__init__()
        self.files = {"/workspace/README.md": "seed project"}

    def list_files(self, sandbox_id: str, path: str):
        return [
            {"path": file_path, "name": file_path.rsplit("/", 1)[-1], "is_dir": False, "size": len(content)}
            for file_path, content in self.files.items()
            if file_path.startswith(path.rstrip("/") + "/")
        ]

    def read_file(self, sandbox_id: str, path: str) -> str:
        return self.files[path]

    def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        self.files[path] = content

    def run(self, sandbox_id: str, command: str):
        return {"stdout": self.files.get("/workspace/README.md", ""), "stderr": "", "exit_code": 0}


def service_for(tmp_path: Path) -> tuple[WorkspaceService, Ledger, FakeDaytona]:
    ledger = Ledger(tmp_path / "workspace.db")
    ledger.initialize()
    daytona = FakeDaytona()
    return WorkspaceService(ledger, daytona), ledger, daytona


def test_join_creates_one_room_sandbox_and_board_delta(tmp_path: Path) -> None:
    service, _, daytona = service_for(tmp_path)
    first = service.join_room("amber-fox", "Alex's Claude Code", "claude-code")
    second = service.join_room("amber-fox", "Sam's Codex", "codex")

    assert daytona.rooms == ["amber-fox"]
    assert first["board_delta"][0]["kind"] == "joined"
    assert any(event["payload"]["name"] == "Sam's Codex" for event in second["board_delta"])
    assert len(second["board"]["agents"]) == 2


def test_get_board_delivers_events_since_agent_cursor(tmp_path: Path) -> None:
    service, _, _ = service_for(tmp_path)
    first = service.join_room("amber-fox", "Alex", "claude-code")
    second = service.join_room("amber-fox", "Sam", "codex")

    board = service.get_board(first["agent_id"])

    assert "board_delta" in board
    assert any(event["agent_id"] == second["agent_id"] and event["kind"] == "joined" for event in board["board_delta"])
    assert {"tasks", "leases", "agents", "recent_events"} <= board.keys()


def test_conditional_claim_allows_exactly_one_winner(tmp_path: Path) -> None:
    service, ledger, _ = service_for(tmp_path)
    first = service.join_room("amber-fox", "Alex", "claude-code")
    second = service.join_room("amber-fox", "Sam", "codex")
    with ledger.transaction() as db:
        task_id = db.execute(
            "INSERT INTO tasks(room, title, status, created_at, updated_at) VALUES (?, ?, 'open', ?, ?)",
            ("amber-fox", "Atomic claim", ledger.now(), ledger.now()),
        ).lastrowid

    assert ledger.claim_task_conditionally("amber-fox", task_id, first["agent_id"])
    assert not ledger.claim_task_conditionally("amber-fox", task_id, second["agent_id"])


def test_get_board_reaps_stale_agents_and_reverts_claims(tmp_path: Path) -> None:
    service, ledger, _ = service_for(tmp_path)
    first = service.join_room("amber-fox", "Alex", "claude-code")
    watcher = service.join_room("amber-fox", "Sam", "codex")
    with ledger.transaction() as db:
        task_id = db.execute(
            "INSERT INTO tasks(room, title, status, claimed_by, created_at, updated_at) VALUES (?, ?, 'claimed', ?, ?, ?)",
            ("amber-fox", "Interrupted work", first["agent_id"], ledger.now(), ledger.now()),
        ).lastrowid
        db.execute("UPDATE agents SET last_seen=? WHERE id=?", (ledger.now() - 181, first["agent_id"]))

    board = service.get_board(watcher["agent_id"])

    assert next(task for task in board["tasks"] if task["id"] == task_id)["status"] == "open"
    assert any(event["kind"] == "task_reverted" for event in board["recent_events"])


def test_shared_sandbox_file_operations_are_visible_to_another_agent(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workspace.db")
    ledger.initialize()
    service = WorkspaceService(ledger, FakeSandbox())
    first = service.join_room("amber-fox", "Alex", "claude-code")
    second = service.join_room("amber-fox", "Sam", "codex")

    assert service.read_file(first["agent_id"], "README.md")["content"] == "seed project"
    written = service.write_file(first["agent_id"], "notes.txt", "shared change")
    observed = service.read_file(second["agent_id"], "notes.txt")
    result = service.run(second["agent_id"], "pytest -q")

    assert written["ok"] is True
    assert observed["content"] == "shared change"
    assert result["exit_code"] == 0
    board = service.get_board(second["agent_id"])
    assert {event["kind"] for event in board["recent_events"]} >= {"file_written", "command_run"}


def test_sandbox_paths_cannot_escape_workspace(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workspace.db")
    ledger.initialize()
    service = WorkspaceService(ledger, FakeSandbox())
    agent = service.join_room("amber-fox", "Alex", "claude-code")["agent_id"]

    for path in ("../../etc/passwd", "/etc/passwd", "src/../../x"):
        try:
            service.write_file(agent, path, "nope")
        except ValueError as exc:
            assert "Invalid path" in str(exc)
        else:
            raise AssertionError(f"path escape was accepted: {path}")
