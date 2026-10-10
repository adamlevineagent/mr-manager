"""Headless account: one `codex app-server` under a CODEX_HOME, owned by the daemon.

Workers run lean: MCP servers and notify hooks from the account's config are switched off for
them (unless allow-listed), approvals are off with full access, and each lane gets its own
environment (ports, shared folders) through Codex's shell environment policy.
"""
import os
import re
import threading
import time
from pathlib import Path

from .appserver import AppServer, RpcError


def _text(t):
    return {"type": "text", "text": t, "text_elements": []}


def _mcp_servers(home):
    try:
        cfg = (Path(home) / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted(set(re.findall(r"^\[mcp_servers\.([A-Za-z0-9_-]+)\]", cfg, re.M)))


class Headless:
    kind = "headless"

    def __init__(self, codex_home, log=print, mcp_allow=()):
        self.home = str(codex_home)
        self.log = log
        self.mcp_allow = set(mcp_allow)
        self.server = None
        self.lock = threading.Lock()
        self.active = {}      # thread_id -> running turn id
        self.last = {}        # thread_id -> latest finished turn: status, at, error, message
        self.activity = {}    # thread_id -> time of the last item event (progress signal)
        self.turn_started = {}

    def ensure(self):
        with self.lock:
            if self.server and self.server.alive:
                return self.server
            env = dict(os.environ, CODEX_HOME=self.home)
            args = ["-c", "notify=[]"]
            for name in _mcp_servers(self.home):
                if name not in self.mcp_allow:
                    args += ["-c", f"mcp_servers.{name}.enabled=false"]
            s = AppServer(args=args, client_name="mr-manager", log=self.log, env=env)
            s.listeners.append(self._notify)
            s.server_request_handler = self._server_request
            s.start()
            self.server = s
            self.active.clear()
            return s

    def _server_request(self, method, params):
        if "approval" in method.lower():
            return {"decision": "decline"}   # workers never escalate
        raise RuntimeError(f"mr-manager can't answer {method}")

    def _notify(self, method, p):
        tid = p.get("threadId")
        if not tid:
            return
        now = time.time()
        if method == "turn/started":
            self.active[tid] = p["turn"]["id"]
            self.turn_started[tid] = now
        elif method == "turn/completed":
            self.active.pop(tid, None)
            turn = p.get("turn") or {}
            self.last[tid] = dict(self.last.get(tid, {}), status=turn.get("status"), at=now,
                                  error=turn.get("error"))
        elif method.startswith("item/"):
            self.activity[tid] = now
            item = p.get("item") or {}
            if method == "item/completed" and item.get("type") == "agentMessage":
                self.last.setdefault(tid, {})["message"] = item.get("text", "")

    # -- the account interface (called by the daemon) ----------------------------------------
    def create(self, prompt, title, cwd, model=None, effort=None, env=None):
        s = self.ensure()
        config = {}
        if effort:
            config["model_reasoning_effort"] = effort
        if env:
            config["shell_environment_policy"] = {"inherit": "all", "set": env}
        params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "danger-full-access",
                  "threadSource": "user", "config": config or None}
        if model:
            params["model"] = model
        tid = s.request("thread/start", params)["thread"]["id"]
        try:
            s.request("thread/name/set", {"threadId": tid, "name": title})
        except RpcError:
            pass
        s.request("turn/start", {"threadId": tid, "input": [_text(prompt)]})
        self.activity[tid] = time.time()
        return tid

    def send(self, thread_id, text, env=None):
        """Steer a running turn (lands at its next step) or start a new one. Never queues.
        env: the lane environment, re-applied if the thread has to be resumed (e.g. after a restart)."""
        s = self.ensure()
        turn = self.active.get(thread_id)
        if turn:
            try:
                s.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn, "input": [_text(text)]})
                return {"steered": True}
            except RpcError:
                if thread_id in self.active:
                    raise
        try:
            s.request("turn/start", {"threadId": thread_id, "input": [_text(text)]})
        except RpcError as e:
            if "not found" not in str(e).lower() and "not loaded" not in str(e).lower():
                raise
            params = {"threadId": thread_id, "excludeTurns": True,
                      "approvalPolicy": "never", "sandbox": "danger-full-access"}
            if env:
                params["config"] = {"shell_environment_policy": {"inherit": "all", "set": env}}
            s.request("thread/resume", params)
            s.request("turn/start", {"threadId": thread_id, "input": [_text(text)]})
        self.activity[thread_id] = time.time()
        return {"started": True}

    def interrupt(self, thread_id):
        turn = self.active.get(thread_id)
        if not turn:
            return {"interrupted": False, "reason": "no running turn"}
        self.ensure().request("turn/interrupt", {"threadId": thread_id, "turnId": turn})
        return {"interrupted": True}

    def read(self, thread_id, turns=3, outputs=False):
        s = self.ensure()
        r = s.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        th = r.get("thread") or {}
        out = []
        for t in (th.get("turns") or [])[-turns:]:
            items = t.get("items") or []
            if not items or t.get("itemsView") == "notLoaded":
                try:
                    items = s.request("thread/items/list", {"threadId": thread_id, "turnId": t["id"]}).get("data", [])
                except RpcError:
                    items = []
            out.append({"id": t["id"], "status": t.get("status"), "items": [summarize(i, outputs) for i in items]})
        return {"thread": {"id": thread_id, "title": th.get("name") or (th.get("preview") or "")[:80],
                           "status": {"type": "active" if thread_id in self.active else "idle"},
                           "cwd": th.get("cwd")}, "turns": out}

    def last_message(self, thread_id):
        """The worker's latest message, from the thread itself (survives daemon restarts)."""
        for t in reversed(self.read(thread_id, turns=2)["turns"]):
            for it in reversed(t["items"]):
                if it["type"] == "agentMessage" and it.get("text"):
                    return t["status"], it["text"]
        return None, ""

    def status(self, thread_id):
        if self.server and not self.server.alive and thread_id in self.active:
            self.active.pop(thread_id, None)
            self.last[thread_id] = {"status": "failed", "at": time.time(),
                                    "error": "codex app-server exited; resend to resume"}
        return {"running": thread_id in self.active, "turn_started": self.turn_started.get(thread_id),
                "last_activity": self.activity.get(thread_id), **self.last.get(thread_id, {})}

    def statuses(self, thread_ids):
        return {t: self.status(t) for t in thread_ids}

    def usage(self):
        return self.ensure().request("account/rateLimits/read", {})

    def consume_reset(self):
        return self.ensure().request("account/rateLimitResetCredit/consume", {})

    def archive(self, thread_id):
        try:
            return self.ensure().request("thread/archive", {"threadId": thread_id})
        except RpcError as e:
            return {"archived": False, "error": str(e)}


def summarize(item, outputs=False):
    t = item.get("type")
    d = {"type": t}
    if t == "userMessage":
        d["text"] = "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text")
    elif t == "agentMessage":
        d["text"] = item.get("text", "")
        d["phase"] = item.get("phase")
    elif t == "commandExecution":
        d["command"] = item.get("command")
        d["exitCode"] = item.get("exitCode")
        d["status"] = item.get("status")
        if outputs:
            d["output"] = (item.get("aggregatedOutput") or "")[-4000:]
    elif t == "fileChange":
        d["files"] = [c.get("path") for c in item.get("changes", [])]
    elif t == "webSearch":
        d["query"] = item.get("query")
    elif t in ("mcpToolCall", "dynamicToolCall"):
        d["tool"] = f"{item.get('server', '')}.{item.get('tool', '')}".strip(".")
    return d
