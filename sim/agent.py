"""Scripted agent: plain HTTP calls to POST /tools/{name} — the same handler
code the MCP tools run. No LLM, no MCP client library."""
import requests


class ToolError(Exception):
    pass


class SimAgent:
    def __init__(self, base_url, room, name, kind="sim"):
        self.base, self.room, self.name, self.kind = base_url.rstrip("/"), room, name, kind
        self.agent_id = None
        self.seen_events = []

    def call(self, tool, **args):
        """POST to the tool endpoint. Collect board_delta from every response."""
        if self.agent_id and tool != "join_room" and "agent_id" not in args:
            args["agent_id"] = self.agent_id
        r = requests.post(f"{self.base}/tools/{tool}", json=args, timeout=90)
        if r.status_code == 400:
            raise ToolError(r.json().get("error", r.text))
        r.raise_for_status()
        data = r.json()
        self.seen_events.extend(data.get("board_delta", []))
        return data

    def join(self):
        d = self.call("join_room", room=self.room, agent_name=self.name, agent_kind=self.kind)
        self.agent_id = d["agent_id"]
        return d

    def seen(self, kind):
        return [e for e in self.seen_events if e["kind"] == kind]

    def go_silent(self):
        """Stop calling. Used to test heartbeat reaping."""
        pass
