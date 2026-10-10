"""Minimal JSON-RPC client for `codex app-server` over stdio."""
import itertools
import json
import os
import subprocess
import threading
from pathlib import Path


def find_codex():
    """Newest Desktop-bundled codex.exe, else whatever is on PATH."""
    override = os.environ.get("MRM_CODEX")
    if override:
        return override
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    cands = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    if cands:
        return str(cands[0])
    return "codex"


class RpcError(Exception):
    def __init__(self, err):
        super().__init__(err.get("message", str(err)))
        self.err = err


class AppServer:
    def __init__(self, exe=None, args=(), client_name="codex-foreman", log=print, env=None):
        self.exe = exe or find_codex()
        self.env = env
        self.args = list(args)
        self.client_name = client_name
        self.log = log
        self.proc = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self.listeners = []          # fn(method, params)
        self.server_request_handler = None   # fn(method, params) -> result
        self.alive = False

    def start(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen([self.exe, "app-server", *self.args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
                                     env=self.env)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True, name="appserver-out").start()
        threading.Thread(target=self._stderr, daemon=True, name="appserver-err").start()
        r = self.request("initialize", {"clientInfo": {"name": self.client_name, "title": "Codex Foreman", "version": "1.0"},
                                        "capabilities": {"experimentalApi": True}})
        self.notify("initialized", {})
        return r

    def stop(self):
        self.alive = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _write(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._wlock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def notify(self, method, params):
        self._write({"method": method, "params": params})

    def request(self, method, params, timeout=120):
        rid = next(self._ids)
        ev = threading.Event()
        slot = {"ev": ev}
        with self._lock:
            self._pending[rid] = slot
        self._write({"method": method, "id": rid, "params": params})
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out")
        if "error" in slot:
            raise RpcError(slot["error"])
        return slot["result"]

    def _reader(self):
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    if "error" in msg:
                        slot["error"] = msg["error"]
                    else:
                        slot["result"] = msg["result"]
                    slot["ev"].set()
            elif "id" in msg and "method" in msg:
                threading.Thread(target=self._serve_request, args=(msg,), daemon=True).start()
            elif "method" in msg:
                for fn in list(self.listeners):
                    try:
                        fn(msg["method"], msg.get("params") or {})
                    except Exception as e:
                        self.log("listener error", e)
        self.alive = False
        with self._lock:
            for slot in self._pending.values():
                slot["error"] = {"message": "app-server exited"}
                slot["ev"].set()
            self._pending.clear()
        self.log("codex app-server exited", self.proc.poll())

    def _serve_request(self, msg):
        try:
            if not self.server_request_handler:
                raise RuntimeError("unhandled")
            result = self.server_request_handler(msg["method"], msg.get("params") or {})
            self._write({"id": msg["id"], "result": result})
        except Exception as e:
            self._write({"id": msg["id"], "error": {"code": -32000, "message": str(e)}})

    def _stderr(self):
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and "failed to load skill" not in line:
                self.log("[codex]", line[:300])
