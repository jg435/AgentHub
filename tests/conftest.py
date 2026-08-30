"""Starts one server per test session on a free port with a fresh SQLite file and
the in-memory fake sandbox (SANDBOX_FAKE=1). Each test gets its own room."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sim.agent import SimAgent  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    port = _free_port()
    env = {**os.environ, "DB_PATH": str(db_path), "SANDBOX_FAKE": "1", "PYTHONUNBUFFERED": "1"}
    env.pop("DAYTONA_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(f"{base}/health", timeout=1).ok:
                break
        except requests.ConnectionError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start:\n" + proc.stdout.read().decode())
    yield {"base": base, "db": str(db_path)}
    proc.kill()


@pytest.fixture
def room(server):
    name = f"t-{uuid.uuid4().hex[:8]}"
    yield name
    requests.post(f"{server['base']}/admin/reset/{name}", timeout=10)


@pytest.fixture
def agents(server, room):
    def make(name, kind="sim"):
        a = SimAgent(server["base"], room, name, kind)
        a.join()
        return a
    return make


@pytest.fixture
def sql(server):
    """Direct SQLite access for force-aging leases / heartbeats."""
    def run(query, *params):
        conn = sqlite3.connect(server["db"], timeout=10)
        try:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.fetchall()
        finally:
            conn.close()
    return run
