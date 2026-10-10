"""A bounded, real Codex turn per account, independent of the daemon."""
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import time
from collections import deque

from . import appserver, core
from .headless import _mcp_servers, _text

TIMEOUT = 75


def cli_version(exe, env=None):
    r = subprocess.run([exe, "--version"], env=env, capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"codex --version exited {r.returncode}")
    return r.stdout.strip() or r.stderr.strip() or "version unknown"


def hint(error, home, exe):
    text = str(error).lower()
    if isinstance(error, FileNotFoundError):
        return "install Codex or set MRM_CODEX to another codex"
    if any(s in text for s in ("certificate", "unknownissuer", "tls", "ssl")):
        return "check TLS certificates or set MRM_CODEX to another codex (try the desktop-bundled CLI)"
    if any(s in text for s in ("auth", "sign in", "sign-in", "login", "401", "403")):
        if os.name == "nt":
            home, exe = str(home).replace("'", "''"), str(exe).replace("'", "''")
            return f"sign in: $env:CODEX_HOME='{home}'; & '{exe}' login"
        return f"sign in: CODEX_HOME={shlex.quote(str(home))} {shlex.quote(str(exe))} login"
    return "check your network and proxy settings, or set MRM_CODEX to another codex"


def _error_text(error):
    if not isinstance(error, dict):
        return str(error)
    message = error.get("message") or str(error)
    details = error.get("additionalDetails")
    return f"{message}: {details}" if details and details != message else message


def probe_account(name, account, cfg, exe=None):
    exe = exe or appserver.find_codex()
    exe = shutil.which(exe) or exe
    if os.path.dirname(exe):
        exe = os.path.abspath(exe)   # a relative MRM_CODEX must also work from the throwaway cwd
    home = account["home"]
    env = dict(os.environ, CODEX_HOME=str(home))
    started = time.monotonic()
    deadline = started + TIMEOUT
    events = queue.Queue()
    logs = deque(maxlen=20)
    server, tid, turn_id, cwd = None, None, None, None
    last_error = None

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError(f"probe did not complete within {TIMEOUT}s")
        return seconds

    print(f"account {name}: CODEX_HOME={home}\n  Codex CLI: {exe}", flush=True)
    try:
        print(f"  version: {cli_version(exe, env)}", flush=True)
        cwd = tempfile.mkdtemp(prefix="mrm-probe-")
        args = ["-c", "notify=[]"]
        for mcp in _mcp_servers(home):
            if mcp not in cfg.get("worker_mcp_allow", []):
                args += ["-c", f"mcp_servers.{mcp}.enabled=false"]
        server = appserver.AppServer(exe=exe, args=args, client_name="mr-manager", env=env,
                                     cwd=cwd, log=lambda *parts: logs.append(" ".join(map(str, parts))))
        server.listeners.append(lambda method, params: events.put((method, params))
                                if method in ("error", "turn/completed", "item/completed") else None)
        server.start(timeout=remaining())
        defaults = cfg.get("defaults", {})
        params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "danger-full-access",
                  "threadSource": "user"}
        if defaults.get("model"):
            params["model"] = defaults["model"]
        if defaults.get("effort"):
            params["config"] = {"model_reasoning_effort": defaults["effort"]}
        tid = server.request("thread/start", params, timeout=remaining())["thread"]["id"]
        turn = server.request("turn/start", {"threadId": tid, "input": [_text("Reply with exactly OK.")]},
                              timeout=remaining())["turn"]
        turn_id = turn["id"]
        if turn.get("status") in ("completed", "failed", "interrupted"):
            events.put(("turn/completed", {"threadId": tid, "turn": turn}))
        reply = ""
        while True:
            try:
                method, p = events.get(timeout=min(0.2, remaining()))
            except queue.Empty:
                if not server.alive or server.proc.poll() is not None:
                    raise RuntimeError(f"codex app-server exited (code {server.proc.poll()})")
                continue
            if p.get("threadId") not in (None, tid) or p.get("turnId") not in (None, turn_id):
                continue
            if method == "error":
                last_error = _error_text(p.get("error") or p)
                if not p.get("willRetry", False):
                    raise RuntimeError(last_error)
            elif method == "item/completed":
                item = p.get("item") or {}
                if item.get("type") == "agentMessage":
                    reply = item.get("text", "")
            elif method == "turn/completed":
                turn = p["turn"]
                if turn.get("id") != turn_id:
                    continue
                if turn.get("status") != "completed":
                    raise RuntimeError(_error_text(turn.get("error") or last_error or
                                                  f"turn {turn.get('status', 'failed')}"))
                for item in turn.get("items") or []:
                    if item.get("type") == "agentMessage":
                        reply = item.get("text", "")
                if reply.strip() != "OK":
                    raise RuntimeError(f"turn completed but expected OK, got {reply!r}")
                print(f"  OK ({time.monotonic() - started:.1f}s)", flush=True)
                return True
    except (OSError, RuntimeError, appserver.RpcError, subprocess.TimeoutExpired) as error:
        detail = str(error)
        if last_error and last_error not in detail:
            detail += f": {last_error}"
        elif not last_error and logs and (isinstance(error, TimeoutError) or "exited" in detail):
            detail += "\n    " + "\n    ".join(logs)
        advice = hint(error if isinstance(error, FileNotFoundError) else detail, home, exe)
        print(f"  FAILED ({time.monotonic() - started:.1f}s): {detail}\n  hint: {advice}",
              flush=True)
        return False
    finally:
        if server and server.proc:
            if tid and server.alive:
                try:
                    server.request("thread/archive", {"threadId": tid}, timeout=3)
                except (OSError, appserver.RpcError, TimeoutError):
                    pass
            server.stop()
            try:
                server.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.proc.kill()
                server.proc.wait(timeout=2)
        if cwd:
            shutil.rmtree(cwd, ignore_errors=True)


def run(account=None, cfg=None):
    cfg = cfg if cfg is not None else core.config()
    accounts = cfg["accounts"]
    if account and account not in accounts:
        raise SystemExit(f"unknown account {account!r}; accounts: {', '.join(accounts)}")
    if not accounts:
        raise SystemExit("no accounts configured; add one: mrm account add NAME")
    exe = appserver.find_codex()
    ok = True
    for name in ([account] if account else accounts):
        ok = probe_account(name, accounts[name], cfg, exe) and ok
    return ok
