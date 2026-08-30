"""Daytona sandbox per room. Agents never touch Daytona directly — everything
proxies through here so leases can be enforced upstream.

Every call fails fast with a SandboxError carrying a clear message; nothing hangs.
"""
import os
import posixpath
import shlex
from pathlib import Path

from daytona import CreateSandboxFromImageParams, Daytona, DaytonaConfig, FileUpload

WORKSPACE = "/workspace"
IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")
SEED_DIR = Path(__file__).resolve().parent.parent / "seed"
EXEC_TIMEOUT_S = 120
_client: Daytona | None = None
_cache: dict[str, object] = {}  # sandbox_id -> Sandbox handle


class SandboxError(Exception):
    pass


def _daytona() -> Daytona:
    global _client
    if _client is None:
        key = os.environ.get("DAYTONA_API_KEY")
        if not key:
            raise SandboxError("DAYTONA_API_KEY is not set on the server")
        _client = Daytona(DaytonaConfig(api_key=key, api_url=os.environ.get("DAYTONA_API_URL")))
    return _client


# --------------------------------------------------------------------------
# path safety
# --------------------------------------------------------------------------

def safe_path(path: str) -> str:
    """Return an absolute path inside /workspace or raise. Rejects absolute paths,
    '..' segments, and anything that normalises outside the workspace."""
    if not isinstance(path, str) or not path.strip():
        raise SandboxError("path must be a non-empty relative path inside the workspace")
    p = path.strip()
    if p.startswith("/") or p.startswith("~"):
        raise SandboxError(f"rejected {path!r}: absolute paths are not allowed; use paths relative to the workspace root")
    if any(seg == ".." for seg in p.split("/")):
        raise SandboxError(f"rejected {path!r}: '..' segments are not allowed")
    full = posixpath.normpath(posixpath.join(WORKSPACE, p))
    if full != WORKSPACE and not full.startswith(WORKSPACE + "/"):
        raise SandboxError(f"rejected {path!r}: escapes the workspace")
    return full


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def create_for_room(room: str) -> str:
    """Create a sandbox, seed /workspace, install deps. Returns sandbox id."""
    try:
        sb = _daytona().create(
            CreateSandboxFromImageParams(
                image=IMAGE, language="python", auto_stop_interval=0,
                labels={"room": room, "app": "agenthub"},
            ),
            timeout=180,
        )
    except Exception as e:
        raise SandboxError(f"could not create sandbox: {e}") from e
    try:
        files = [
            FileUpload(source=f.read_bytes(), destination=f"{WORKSPACE}/{f.relative_to(SEED_DIR).as_posix()}")
            for f in SEED_DIR.rglob("*") if f.is_file() and "__pycache__" not in f.parts
        ]
        sb.fs.upload_files(files, timeout=120)
        r = sb.process.exec(
            f"cd {WORKSPACE} && pip install -q -r requirements.txt", timeout=240)
        if r.exit_code != 0:
            raise SandboxError(f"seed dependency install failed (exit {r.exit_code}): {r.result[-2000:]}")
    except SandboxError:
        raise
    except Exception as e:
        raise SandboxError(f"could not seed sandbox: {e}") from e
    _cache[sb.id] = sb
    return sb.id


def get(sandbox_id: str | None):
    if not sandbox_id:
        raise SandboxError("this room has no sandbox; join_room should have created one")
    sb = _cache.get(sandbox_id)
    try:
        if sb is None:
            sb = _daytona().get(sandbox_id, request_timeout=30)
            _cache[sandbox_id] = sb
        state = str(getattr(sb, "state", "")).lower()
        if "started" not in state:
            sb.start(timeout=90)
    except Exception as e:
        raise SandboxError(f"sandbox {sandbox_id} unreachable: {e}") from e
    return sb


# --------------------------------------------------------------------------
# file + process ops
# --------------------------------------------------------------------------

def list_files(sandbox_id: str | None, rel_dir: str) -> list[dict]:
    full = safe_path(rel_dir or ".")
    sb = get(sandbox_id)
    try:
        infos = sb.fs.list_files(full, request_timeout=30)
    except Exception as e:
        raise SandboxError(f"list_files {rel_dir!r} failed: {e}") from e
    out = []
    for i in infos:
        name = getattr(i, "name", None) or str(i)
        out.append({
            "path": posixpath.relpath(posixpath.join(full, name), WORKSPACE),
            "is_dir": bool(getattr(i, "is_dir", False)),
            "size": getattr(i, "size", None),
        })
    return sorted(out, key=lambda x: (not x["is_dir"], x["path"]))


def read_file(sandbox_id: str | None, rel_path: str) -> str:
    full = safe_path(rel_path)
    sb = get(sandbox_id)
    try:
        data = sb.fs.download_file(full)
    except Exception as e:
        raise SandboxError(f"read_file {rel_path!r} failed: {e}") from e
    if data is None:
        raise SandboxError(f"read_file {rel_path!r}: no such file")
    return data.decode("utf-8", errors="replace")


def write_file(sandbox_id: str | None, rel_path: str, content: str) -> int:
    full = safe_path(rel_path)
    sb = get(sandbox_id)
    try:
        parent = posixpath.dirname(full)
        if parent != WORKSPACE:
            sb.process.exec(f"mkdir -p {shlex.quote(parent)}", timeout=30)
        sb.fs.upload_file(content.encode("utf-8"), full, timeout=60)
    except Exception as e:
        raise SandboxError(f"write_file {rel_path!r} failed: {e}") from e
    return len(content)


def run(sandbox_id: str | None, command: str) -> dict:
    """Run a shell command in /workspace. Returns {stdout, stderr, exit_code}."""
    sb = get(sandbox_id)
    # Daytona's exec returns combined output; split stderr out via a temp file.
    wrapped = (
        f"cd {WORKSPACE} && ( {command} ) 2>/tmp/.agenthub_stderr; rc=$?; "
        f"printf '\\n__AGENTHUB_STDERR__\\n'; cat /tmp/.agenthub_stderr; exit $rc"
    )
    try:
        r = sb.process.exec(f"bash -lc {shlex.quote(wrapped)}", timeout=EXEC_TIMEOUT_S)
    except Exception as e:
        raise SandboxError(f"run failed (sandbox unreachable or timed out after {EXEC_TIMEOUT_S}s): {e}") from e
    out = r.result or ""
    stdout, sep, stderr = out.partition("\n__AGENTHUB_STDERR__\n")
    return {"stdout": stdout, "stderr": stderr if sep else "", "exit_code": r.exit_code}
