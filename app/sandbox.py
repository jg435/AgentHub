from __future__ import annotations

import os
import shlex
from pathlib import PurePosixPath
from typing import Any


WORKSPACE_ROOT = "/workspace"
# Daytona's standard image runs as an unprivileged user. Its user-relative
# `workspace` directory is the physical backing for our `/workspace` contract.
DAYTONA_WORKSPACE = "workspace"


class SandboxError(RuntimeError):
    """A clear error for an unavailable or failed shared sandbox operation."""


def workspace_path(path: str, *, directory: bool = False) -> str:
    """Return a sandbox path while rejecting absolute and traversal input."""
    if not isinstance(path, str):
        raise ValueError("Path must be a string.")
    candidate = path.strip()
    if directory and candidate in {"", "."}:
        return WORKSPACE_ROOT
    parts = PurePosixPath(candidate).parts
    if not candidate or PurePosixPath(candidate).is_absolute() or ".." in parts:
        raise ValueError("Invalid path: paths must be relative to /workspace and cannot contain '..'.")
    return f"{WORKSPACE_ROOT}/{candidate.lstrip('./')}"


class DaytonaSandboxGateway:
    """The only component that talks to Daytona; MCP clients never do directly."""

    def _client(self) -> Any:
        token = os.getenv("DAYTONA_API_KEY")
        if not token:
            raise SandboxError("DAYTONA_API_KEY is not configured; the shared sandbox is unavailable.")
        try:
            from daytona import Daytona, DaytonaConfig
            return Daytona(DaytonaConfig(api_key=token, target=os.getenv("DAYTONA_TARGET")))
        except Exception as exc:
            raise SandboxError(f"Unable to initialise Daytona: {exc}") from exc

    def create_workspace(self, room: str) -> str:
        try:
            sandbox = self._client().create()
            seed_repo = os.getenv("SEED_REPO_URL")
            if seed_repo:
                command = f"git clone {shlex.quote(seed_repo)} {DAYTONA_WORKSPACE}"
            else:
                command = f"mkdir -p {DAYTONA_WORKSPACE}"
            result = sandbox.process.exec(command, timeout=120)
            if getattr(result, "exit_code", 0) != 0:
                raise SandboxError(f"Failed to seed /workspace: {getattr(result, 'result', '')}")
            return str(sandbox.id)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Unable to create shared sandbox for room {room}: {exc}") from exc

    def _sandbox(self, sandbox_id: str) -> Any:
        try:
            return self._client().get(sandbox_id, request_timeout=15)
        except Exception as exc:
            raise SandboxError(f"Shared sandbox {sandbox_id} is unreachable: {exc}") from exc

    @staticmethod
    def _remote_path(virtual_path: str) -> str:
        if virtual_path == WORKSPACE_ROOT:
            return DAYTONA_WORKSPACE
        return DAYTONA_WORKSPACE + virtual_path.removeprefix(WORKSPACE_ROOT)

    @staticmethod
    def _virtual_path(remote_path: str) -> str:
        if remote_path == DAYTONA_WORKSPACE:
            return WORKSPACE_ROOT
        return WORKSPACE_ROOT + remote_path.removeprefix(DAYTONA_WORKSPACE)

    def list_files(self, sandbox_id: str, path: str) -> list[dict[str, Any]]:
        try:
            entries = self._sandbox(sandbox_id).fs.list_files(self._remote_path(path), depth=2, request_timeout=15)
            return [
                {
                    "path": self._virtual_path(item.path),
                    "name": item.name,
                    "is_dir": item.is_dir,
                    "size": item.size,
                }
                for item in entries
            ]
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Unable to list shared files: {exc}") from exc

    def read_file(self, sandbox_id: str, path: str) -> str:
        try:
            return self._sandbox(sandbox_id).fs.download_file(self._remote_path(path)).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxError("The requested file is not UTF-8 text.") from exc
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Unable to read shared file: {exc}") from exc

    def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        try:
            self._sandbox(sandbox_id).fs.upload_file(content.encode("utf-8"), self._remote_path(path), timeout=30)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Unable to write shared file: {exc}") from exc

    def run(self, sandbox_id: str, command: str) -> dict[str, Any]:
        try:
            response = self._sandbox(sandbox_id).process.exec(command, cwd=DAYTONA_WORKSPACE, timeout=120)
            return {
                "stdout": str(getattr(response, "result", "")),
                "stderr": str(getattr(response, "stderr", "")),
                "exit_code": int(getattr(response, "exit_code", 0)),
            }
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"Unable to run command in shared sandbox: {exc}") from exc
