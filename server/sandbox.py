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
NOISE = {"__pycache__", ".pytest_cache", ".git", "node_modules", ".venv"}
SEED_SKIP = {"tasks.json"}  # server-side seed data, not part of the project
_client: Daytona | None = None
_cache: dict[str, object] = {}  # sandbox_id -> Sandbox handle
FAKE = os.environ.get("SANDBOX_FAKE") == "1"  # in-memory sandbox for the simulation suite
_fake_fs: dict[str, dict[str, str]] = {}


class SandboxError(Exception):
    pass


class _FakeSandbox:
    """Just enough of the Daytona surface for the ledger tests (no Daytona calls)."""

    def __init__(self, sid: str):
        self.id = sid
        self.files = _fake_fs.setdefault(sid, {
            f.relative_to(SEED_DIR).as_posix(): f.read_text()
            for f in SEED_DIR.rglob("*")
            if f.is_file() and not (set(f.parts) & NOISE) and f.name not in SEED_SKIP})


def destroy(sandbox_id: str) -> None:
    _cache.pop(sandbox_id, None)
    if FAKE:
        _fake_fs.pop(sandbox_id, None)
        return
    _daytona().get(sandbox_id, request_timeout=30).delete()


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
    if FAKE:
        sid = f"fake-{room}"
        _cache[sid] = _FakeSandbox(sid)
        return sid
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
            for f in SEED_DIR.rglob("*")
            if f.is_file() and not (set(f.parts) & NOISE) and f.name not in SEED_SKIP
        ]
        sb.fs.upload_files(files, timeout=120)
        r = sb.process.exec(
            f"cd {WORKSPACE} && pip install -q -r requirements.txt && "
            f"(command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git >/dev/null)) && "
            f"git config --global user.email agent@agenthub.local && git config --global user.name 'AgentHub room' && "
            f"git config --global init.defaultBranch main && git config --global --add safe.directory {WORKSPACE}",
            timeout=300)
        if r.exit_code != 0:
            raise SandboxError(f"seed setup failed (exit {r.exit_code}): {r.result[-2000:]}")
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
    if FAKE:
        return sb or _cache.setdefault(sandbox_id, _FakeSandbox(sandbox_id))
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
    """Two levels deep (ported from the Codex branch), so one call shows the repo shape."""
    full = safe_path(rel_dir or ".")
    sb = get(sandbox_id)
    if FAKE:
        prefix = "" if full == WORKSPACE else posixpath.relpath(full, WORKSPACE) + "/"
        seen: dict[str, bool] = {}
        for p in sb.files:
            if not p.startswith(prefix):
                continue
            parts = p[len(prefix):].split("/")
            for depth in (1, 2):
                if len(parts) >= depth:
                    sub = prefix + "/".join(parts[:depth])
                    seen[sub] = seen.get(sub, False) or len(parts) > depth
        return sorted(({"path": k, "is_dir": v, "size": None} for k, v in seen.items()),
                      key=lambda x: (x["path"].count("/"), not x["is_dir"], x["path"]))
    try:
        infos = sb.fs.list_files(full, depth=2, request_timeout=30)
    except Exception as e:
        raise SandboxError(f"list_files {rel_dir!r} failed: {e}") from e
    out = []
    for i in infos:
        abs_path = getattr(i, "path", None) or posixpath.join(full, getattr(i, "name", str(i)))
        if not abs_path.startswith("/"):
            abs_path = posixpath.join(full, abs_path)
        out.append({
            "path": posixpath.relpath(abs_path, WORKSPACE),
            "is_dir": bool(getattr(i, "is_dir", False)),
            "size": getattr(i, "size", None),
        })
    out = [o for o in out if not any(seg in NOISE for seg in o["path"].split("/"))]
    return sorted(out, key=lambda x: (x["path"].count("/"), not x["is_dir"], x["path"]))


def read_file(sandbox_id: str | None, rel_path: str) -> str:
    full = safe_path(rel_path)
    sb = get(sandbox_id)
    if FAKE:
        rel = posixpath.relpath(full, WORKSPACE)
        if rel not in sb.files:
            raise SandboxError(f"read_file {rel_path!r}: no such file")
        return sb.files[rel]
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
    if FAKE:
        sb.files[posixpath.relpath(full, WORKSPACE)] = content
        return len(content)
    try:
        parent = posixpath.dirname(full)
        if parent != WORKSPACE:
            sb.process.exec(f"mkdir -p {shlex.quote(parent)}", timeout=30)
        sb.fs.upload_file(content.encode("utf-8"), full, timeout=60)
    except Exception as e:
        raise SandboxError(f"write_file {rel_path!r} failed: {e}") from e
    return len(content)


def commit_and_push(sandbox_id: str | None, message: str, exclude: list[str]) -> dict:
    """git add (minus `exclude`), commit, push HEAD:<GIT_BRANCH> to GIT_REMOTE_URL.
    Returns {sha, branch, pushed, summary}. Raises SandboxError with a directive message."""
    remote = os.environ.get("GIT_REMOTE_URL")
    branch = os.environ.get("GIT_BRANCH", "main")
    if not remote:
        raise SandboxError("GIT_REMOTE_URL is not configured on the server, so there is nowhere to push")
    if FAKE:
        sb = get(sandbox_id)
        return {"sha": "fake0000", "branch": branch, "pushed": True,
                "summary": f"{len(sb.files)} files (fake sandbox)"}
    q = shlex.quote
    excl = " ".join(f"':!{p}'" for p in exclude)
    script = (
        f"cd {WORKSPACE} && "
        f"( [ -d .git ] || git init -q ) && "
        f"( git remote get-url origin >/dev/null 2>&1 && git remote set-url origin {q(remote)} || git remote add origin {q(remote)} ) && "
        f"git add -A -- . {excl} && "
        f"( git diff --cached --quiet && echo NOTHING_TO_COMMIT || git commit -q -m {q(message)} ) && "
        f"git rev-parse --short HEAD && git push -q origin HEAD:{q(branch)} 2>&1 && echo PUSH_OK"
    )
    r = run(sandbox_id, script)
    out = (r["stdout"] + "\n" + r["stderr"]).replace(remote, "<remote>")
    if "NOTHING_TO_COMMIT" in out and "PUSH_OK" not in out and r["exit_code"] != 0:
        raise SandboxError("nothing to commit and push failed: " + out[-800:])
    if r["exit_code"] != 0 or "PUSH_OK" not in out:
        hint = (" The remote has moved: run(\"git pull --rebase origin " + branch + "\") then retry."
                if "rejected" in out or "fetch first" in out else "")
        raise SandboxError("push failed: " + out[-800:].strip() + hint)
    sha = next((line.strip() for line in out.splitlines() if len(line.strip()) in (7, 8) and line.strip().isalnum()), "?")
    return {"sha": sha, "branch": branch, "pushed": True,
            "summary": "nothing new to commit; pushed existing HEAD" if "NOTHING_TO_COMMIT" in out else "committed and pushed"}


def run(sandbox_id: str | None, command: str) -> dict:
    """Run a shell command in /workspace. Returns {stdout, stderr, exit_code}."""
    sb = get(sandbox_id)
    if FAKE:
        return {"stdout": f"[fake sandbox] {command}\n", "stderr": "", "exit_code": 0}
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
