"""TESTING.md scenarios S1–S12 against the running server via the /tools shim."""
import os
import threading
import time

import pytest

from sim.agent import ToolError

ALL_TOOLS = ["join_room", "get_board", "create_task", "claim_task", "acquire_lease", "release_lease",
             "list_files", "read_file", "write_file", "run", "log_work", "post_update", "handoff",
             "wait_for_event", "commit_and_push"]


def _task(agent, title="task", files=None):
    return agent.call("create_task", title=title, description="", suggested_files=files or [])["task_id"]


def _claimed(agent, title="task"):
    tid = _task(agent, title)
    assert agent.call("claim_task", task_id=tid)["granted"]
    return tid


# ---- S3 / S4: races (run first, 50x) ------------------------------------

@pytest.mark.parametrize("i", range(50))
def test_s3_claim_race(agents, i):
    a, b = agents("A"), agents("B")
    tid = _task(a, "race")
    results = {}

    def go(ag, key):
        results[key] = ag.call("claim_task", task_id=tid)

    ts = [threading.Thread(target=go, args=(a, "a")), threading.Thread(target=go, args=(b, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    granted = [k for k, v in results.items() if v["granted"]]
    assert len(granted) == 1, results
    winner = {"a": a, "b": b}[granted[0]]
    task = next(t for t in a.call("get_board")["tasks"] if t["id"] == tid)
    assert task["status"] == "claimed" and task["claimed_by"] == winner.agent_id


@pytest.mark.parametrize("i", range(50))
def test_s4_lease_race(agents, i):
    a, b = agents("A"), agents("B")
    ta, tb = _task(a, "ta"), _task(b, "tb")
    a.call("claim_task", task_id=ta); b.call("claim_task", task_id=tb)
    results = {}

    def go(ag, key):
        results[key] = ag.call("acquire_lease", paths=["src/auth.py"], task_id={"a": ta, "b": tb}[key])

    ts = [threading.Thread(target=go, args=(a, "a")), threading.Thread(target=go, args=(b, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    granted = [k for k, v in results.items() if v["granted"]]
    assert len(granted) == 1, results
    leases = a.call("get_board")["leases"]
    assert len(leases) == 1 and leases[0]["agent_id"] == {"a": a, "b": b}[granted[0]].agent_id


# ---- S10: board_delta on every tool --------------------------------------

@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_s10_board_delta_on_every_tool(agents, room, server, tool):
    a, b = agents("A"), agents("B")
    tid = _task(a, "something", ["x.py"])
    b.call("claim_task", task_id=tid)
    b.call("acquire_lease", paths=["x.py"], task_id=tid)
    args = {
        "join_room": dict(room=room, agent_name="C", agent_kind="sim"),
        "get_board": {}, "create_task": dict(title="t", description="", suggested_files=[]),
        "claim_task": dict(task_id=tid), "acquire_lease": dict(paths=["y.py"], task_id=tid),
        "release_lease": dict(paths=["x.py"]), "list_files": dict(dir="."),
        "read_file": dict(path="README.md"), "write_file": dict(path="x.py", content="hi"),
        "run": dict(command="true"), "log_work": dict(task_id=tid, note="n"),
        "post_update": dict(kind="note", message="m"),
        "handoff": dict(summary="s", next_steps="n", blockers=""),
        "wait_for_event": dict(timeout_s=0),
        "commit_and_push": dict(message="s10"),
    }[tool]
    if tool == "commit_and_push":
        b.call("release_lease", paths=["x.py"])
    a.call("post_update", kind="note", message="ping")  # something for B to learn about
    resp = b.call(tool, **args)
    assert "board_delta" in resp, tool
    assert isinstance(resp["board_delta"], list)
    if tool != "join_room":
        assert any(e["kind"] == "update_posted" for e in resp["board_delta"]), tool


# ---- S2 / S5 / S6: leasing -----------------------------------------------

def test_s2_lease_collision(agents):
    a, b = agents("Alex's Claude Code"), agents("Bea's Codex")
    t1 = _task(a, "add password reset endpoint", ["src/auth.py"])
    t2 = _task(a, "write tests for /health", ["tests/test_health.py"])
    a.call("claim_task", task_id=t1)
    assert a.call("acquire_lease", paths=["src/auth.py"], task_id=t1)["granted"]
    t3 = _task(b, "refactor auth", ["src/auth.py"])
    b.call("claim_task", task_id=t3)
    d = b.call("acquire_lease", paths=["src/auth.py"], task_id=t3)
    assert d["granted"] is False
    msg = d["denials"][0]["message"]
    assert msg.startswith("DENIED: src/auth.py is leased by \"Alex's Claude Code\"")
    assert f"task #{t1} \"add password reset endpoint\"" in msg
    assert f"task #{t2} \"write tests for /health\"" in msg and "unclaimed" in msg
    assert "wait_for_event" in msg and "expires automatically" in msg and "Do not retry in a loop" in msg
    assert any(e["kind"] == "lease_denied" for e in a.call("get_board")["recent_events"])


def test_s5_write_without_lease(agents):
    a = agents("A")
    tid = _task(a, "edit readme", ["README.md"])
    a.call("claim_task", task_id=tid)
    before = a.call("read_file", path="README.md")["content"]
    r = a.call("write_file", path="README.md", content="clobbered")
    assert r["granted"] is False and "acquire_lease" in r["denials"][0]["message"]
    assert a.call("read_file", path="README.md")["content"] == before


def test_s6_lease_expiry(agents, sql, room):
    a, b = agents("A"), agents("B")
    ta, tb = _claimed(a), _claimed(b)
    assert a.call("acquire_lease", paths=["src/auth.py"], task_id=ta)["granted"]
    sql("UPDATE leases SET expires_at=? WHERE room=? AND path='src/auth.py'", time.time() - 1, room)
    assert b.call("acquire_lease", paths=["src/auth.py"], task_id=tb)["granted"]
    kinds = [e["kind"] for e in b.call("get_board")["recent_events"]]
    assert "lease_expired" in kinds


def test_lease_all_or_nothing(agents):
    a, b = agents("A"), agents("B")
    ta, tb = _claimed(a), _claimed(b)
    assert a.call("acquire_lease", paths=["a.py"], task_id=ta)["granted"]
    d = b.call("acquire_lease", paths=["b.py", "a.py"], task_id=tb)
    assert d["granted"] is False
    assert b.call("get_board")["leases"][0]["path"] == "a.py"  # b.py was NOT granted


def test_lease_requires_claimed_task(agents):
    """Ported from the Codex branch: no lease without a task you hold."""
    a = agents("A")
    d = a.call("acquire_lease", paths=["x.py"])
    assert d["granted"] is False and "claim_task" in d["denials"][0]["message"]
    tid = _task(a, "unclaimed")
    d = a.call("acquire_lease", paths=["x.py"], task_id=tid)
    assert d["granted"] is False
    a.call("claim_task", task_id=tid)
    assert a.call("acquire_lease", paths=["x.py"], task_id=tid)["granted"]
    assert a.call("get_board")["leases"] != []


def test_commit_and_push_rules(agents):
    a, b = agents("A"), agents("B")
    ta, tb = _claimed(a), _claimed(b)
    a.call("acquire_lease", paths=["app/main.py"], task_id=ta)
    b.call("acquire_lease", paths=["tests/test_todos.py"], task_id=tb)
    with pytest.raises(ToolError, match="release your leases"):
        a.call("commit_and_push", message="too early")
    a.call("release_lease", paths=["app/main.py"])
    r = a.call("commit_and_push", message="ship it")
    assert r["pushed"] and r["excluded_leased_by_others"] == ["tests/test_todos.py"]
    assert any(e["kind"] == "pushed" for e in b.call("get_board")["board_delta"])


def test_list_files_is_two_levels_deep(agents):
    a = agents("A")
    paths = {e["path"] for e in a.call("list_files", dir=".")["entries"]}
    assert {"app", "tests", "app/main.py", "tests/test_todos.py"} <= paths


def test_write_renews_lease_and_logs(agents, room, sql):
    a = agents("A")
    tid = _task(a, "t", ["f.py"])
    a.call("claim_task", task_id=tid)
    a.call("acquire_lease", paths=["f.py"], task_id=tid)
    sql("UPDATE leases SET expires_at=? WHERE room=?", time.time() + 5, room)
    assert a.call("write_file", path="f.py", content="x")["ok"]
    (exp,) = sql("SELECT expires_at FROM leases WHERE room=?", room)[0]
    assert exp > time.time() + 200
    assert any("wrote f.py" in n for n in _notes(sql, tid))


# ---- S7 / S8 / S9: continuity --------------------------------------------

def _notes(sql, tid):
    return [n for (n,) in sql("SELECT note FROM worklog WHERE task_id=? ORDER BY id", tid)]


def test_s7_heartbeat_reaping_and_resume(agents, sql, room):
    a = agents("A")
    tid = _task(a, "implement password reset", ["src/auth.py"])
    a.call("claim_task", task_id=tid)
    a.call("acquire_lease", paths=["src/auth.py", "src/mail.py"], task_id=tid)
    for n in ("found the token helper in src/auth.py", "mail sending needs a stub", "tests in tests/test_auth.py"):
        a.call("log_work", task_id=tid, note=n)
    a.go_silent()
    sql("UPDATE agents SET last_seen=? WHERE id=?", time.time() - 400, a.agent_id)
    b = agents("B")
    board = b.call("get_board")
    assert next(x for x in board["agents"] if x["id"] == a.agent_id)["active"] == 0
    assert board["leases"] == []
    assert next(t for t in board["tasks"] if t["id"] == tid)["status"] == "open"
    assert len(_notes(sql, tid)) == 3
    briefing = b.call("join_room", room=room, agent_name="B2", agent_kind="sim")["resume_briefing"]
    assert "implement password reset" in briefing
    assert "mail sending needs a stub" in briefing
    assert any(e["kind"] == "task_reverted" for e in board["recent_events"])


def test_s8_graceful_handoff(agents, room):
    a = agents("A")
    tid = _task(a, "wire up /reset", ["src/auth.py"])
    a.call("claim_task", task_id=tid)
    a.call("acquire_lease", paths=["src/auth.py"], task_id=tid)
    a.call("log_work", task_id=tid, note="half done")
    a.call("handoff", summary="endpoint scaffolded", next_steps="add the email step and a test", blockers="")
    b = agents("B")
    board = b.call("get_board")
    assert board["leases"] == []
    assert next(x for x in board["agents"] if x["id"] == a.agent_id)["active"] == 0
    assert next(t for t in board["tasks"] if t["id"] == tid)["status"] == "claimed"
    briefing = b.call("join_room", room=room, agent_name="B2", agent_kind="sim")["resume_briefing"]
    assert "add the email step and a test" in briefing


def test_s9_worklog_inheritance(agents, sql):
    a = agents("A")
    tid = _task(a, "t")
    a.call("claim_task", task_id=tid)
    a.call("log_work", task_id=tid, note="learned something important")
    sql("UPDATE agents SET last_seen=? WHERE id=?", time.time() - 400, a.agent_id)
    b = agents("B")
    b.call("get_board")  # triggers reaping
    r = b.call("claim_task", task_id=tid)
    assert r["granted"] and any(w["note"] == "learned something important" for w in r["worklog"])


# ---- S1 / S11 / S12 -------------------------------------------------------

def test_s1_two_agents_see_each_other(agents):
    a, b = agents("A"), agents("B")
    tid = _task(a, "hello")
    board = b.call("get_board")
    assert any(e["kind"] == "task_created" for e in board["board_delta"])
    assert any(t["id"] == tid for t in board["tasks"])


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "src/../../x"])
def test_s11_path_escape(agents, path):
    a = agents("A")
    with pytest.raises(ToolError):
        a.call("write_file", path=path, content="x")
    with pytest.raises(ToolError):
        a.call("acquire_lease", paths=[path])


def test_wait_for_event_unblocks(agents):
    a, b = agents("A"), agents("B")
    out = {}
    t = threading.Thread(target=lambda: out.update(b.call("wait_for_event", timeout_s=10)))
    t.start()
    time.sleep(0.7)
    a.call("post_update", kind="note", message="wake up")
    t.join(timeout=15)
    assert out["timed_out"] is False and any(e["kind"] == "update_posted" for e in out["board_delta"])


def test_post_update_done_marks_task(agents):
    a = agents("A")
    tid = _task(a, "t")
    a.call("claim_task", task_id=tid)
    a.call("post_update", kind="done", message="shipped")
    assert next(t for t in a.call("get_board")["tasks"] if t["id"] == tid)["status"] == "done"


@pytest.mark.skipif(os.environ.get("RUN_DAYTONA") != "1", reason="set RUN_DAYTONA=1 to hit real Daytona")
def test_s12_sandbox_is_shared_ground_truth():
    """Real Daytona: run against a live server given by AGENTHUB_URL."""
    from sim.agent import SimAgent
    base = os.environ.get("AGENTHUB_URL", "http://localhost:8000")
    room = f"s12-{int(time.time())}"
    a, b = SimAgent(base, room, "A"), SimAgent(base, room, "B")
    a.join(); b.join()
    tid = _task(a, "break a test", ["tests/test_broken.py"])
    a.call("claim_task", task_id=tid)
    a.call("acquire_lease", paths=["tests/test_broken.py"], task_id=tid)
    a.call("write_file", path="tests/test_broken.py", content="def test_broken():\n    assert False\n")
    r = b.call("run", command="pytest -q")
    assert r["exit_code"] != 0 and "test_broken" in r["stdout"]
    import requests
    requests.post(f"{base}/admin/reset/{room}", timeout=60)
