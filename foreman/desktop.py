"""Codex Desktop backend: drive threads through Desktop's own codex_app tools.

Codex Desktop serves its thread tools (create_thread, send_message_to_thread, read_thread,
wait_threads, get_usage_limits, ...) to its agents over a local named pipe. Frames are a
4-byte little-endian length followed by UTF-8 JSON-RPC 2.0. The pipe name changes on every
Desktop launch, so we find it by asking each candidate for its tool list.

Threads created here are ordinary Desktop threads: visible, steerable and openable in the UI.
"""
import json
import os
import struct
import threading
import uuid

PIPE_DIR = "\\\\.\\pipe\\"
PREFIX = "codex-browser-use-"


def _read_exact(f, n):
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise ConnectionError("pipe closed")
        buf += chunk
    return buf


def rpc(path, method, params=None, timeout=60):
    msg = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method}
    if params is not None:
        msg["params"] = params
    box = {}

    def go():
        try:
            with open(path, "r+b", buffering=0) as f:
                data = json.dumps(msg).encode("utf-8")
                f.write(struct.pack("<I", len(data)) + data)
                while True:
                    n = struct.unpack("<I", _read_exact(f, 4))[0]
                    reply = json.loads(_read_exact(f, n).decode("utf-8"))
                    if reply.get("id") == msg["id"]:
                        box["reply"] = reply
                        return
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if "error" in box:
        raise box["error"]
    if "reply" not in box:
        raise TimeoutError(f"{method} timed out")
    if "error" in box["reply"]:
        raise RuntimeError(box["reply"]["error"].get("message", str(box["reply"]["error"])))
    return box["reply"].get("result")


def find_pipe():
    if not os.path.isdir(PIPE_DIR):
        return None
    for name in os.listdir(PIPE_DIR):
        if not name.startswith(PREFIX):
            continue
        try:
            tools = (rpc(PIPE_DIR + name, "tools/list", {"threadStartKind": "all"}, timeout=3) or {}).get("tools", [])
        except Exception:
            continue
        if any(t.get("name") == "send_message_to_thread" for t in tools):
            return PIPE_DIR + name
    return None


class Desktop:
    """One Codex Desktop install (account A)."""
    kind = "desktop"

    def __init__(self, caller_thread=None):
        self._pipe = None
        self.caller = caller_thread   # a real thread id; Desktop requires a calling thread

    def pipe(self, refresh=False):
        if refresh or not self._pipe:
            self._pipe = find_pipe()
        if not self._pipe:
            raise ConnectionError("Codex Desktop isn't running (no app-tools pipe found)")
        return self._pipe

    def tool(self, name, args, caller=None, timeout=120):
        for attempt in range(2):
            try:
                r = rpc(self.pipe(refresh=attempt > 0), "tools/call", {
                    "namespace": "codex_app", "tool": name, "arguments": args, "callerSource": "codex",
                    "callId": "foreman-" + uuid.uuid4().hex, "hostId": "local",
                    "threadId": caller or self.caller, "turnId": "foreman-" + uuid.uuid4().hex[:12]},
                    timeout=timeout) or {}
                break
            except TimeoutError:
                raise   # the call may have landed; replaying could duplicate a worker or message
            except (FileNotFoundError, ConnectionRefusedError, BrokenPipeError):
                if attempt:   # pipe gone (Desktop restarted): safe to rediscover and retry once
                    raise
        text = (r.get("contentItems") or [{}])[0].get("text", "")
        if not r.get("success", False):
            raise RuntimeError(text[:500] or "Desktop refused the call")
        try:
            return json.loads(text)
        except ValueError:
            return text

    # -- the backend interface ---------------------------------------------------------------
    def projects(self):
        return self.tool("list_projects", {}).get("projects", [])

    def create(self, prompt, title, project_id=None, worktree=False, branch=None, model=None, effort=None):
        if project_id:
            env = {"type": "local"}
            if worktree:
                env = {"type": "worktree"}
                if branch:
                    env["startingState"] = {"type": "branch", "branchName": branch, "onMissing": "create-branch"}
            target = {"type": "project", "projectId": project_id, "environment": env}
        else:
            target = {"type": "projectless", "directoryName": title[:60]}
        args = {"title": title, "prompt": prompt, "target": target}
        if model:
            args["model"] = model
        if effort:
            args["thinking"] = effort
        return self.tool("create_thread", args)["threadId"]

    def send(self, thread_id, text):
        # Steers into a running turn (lands at the next tool boundary) or starts one. Never queues.
        return self.tool("send_message_to_thread", {"threadId": thread_id, "prompt": text}, caller=thread_id)

    def read(self, thread_id, turns=3, outputs=False):
        return self.tool("read_thread", {"threadId": thread_id, "turnLimit": max(1, min(turns, 10)),
                                         "includeOutputs": outputs, "maxOutputCharsPerItem": 4000})

    def wait(self, thread_ids, timeout_ms=60000, cursors=None):
        """wait_threads takes at most 8 targets; larger sets are polled in batches."""
        batches = [thread_ids[i:i + 8] for i in range(0, len(thread_ids), 8)] or [[]]
        per = max(1000, timeout_ms // len(batches)) if len(batches) > 1 else timeout_ms
        polls = []
        for batch in batches:
            targets = [dict({"threadId": t}, **({"afterCursor": cursors[t]} if cursors and t in cursors else {}))
                       for t in batch]
            r = self.tool("wait_threads", {"targets": targets, "timeoutMs": per if len(batches) == 1 else 0},
                          timeout=per / 1000 + 60)
            polls += r.get("polls") or []
        return {"polls": polls}

    def usage(self):
        return self.tool("get_usage_limits", {})

    def archive(self, thread_id, archived=True):
        return self.tool("set_thread_archived", {"threadId": thread_id, "archived": archived})

    def open(self, thread_id):
        os.startfile(f"codex://threads/{thread_id}")
