"""State, accounts, the worker registry, and the client side of the daemon.

Accounts are Codex homes (CODEX_HOME folders), each signed in to a Codex plan. Every account
runs headless through `codex app-server`, hosted by the always-on daemon, so workers never wait
on approval prompts and nothing depends on the Codex Desktop app being open. (Desktop threads
from older versions are still readable through the legacy desktop backend.)
"""
import json
import os

os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")   # our reads (status) never take index.lock in a lane a worker is using
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _home():
    env = os.environ.get("MRM_HOME") or os.environ.get("FOREMAN_HOME")
    if env:
        return Path(env)
    new, legacy = Path.home() / ".mr-manager", Path.home() / ".codex-foreman"
    return legacy if legacy.exists() and not new.exists() else new


HOME = _home()
PORT = int(os.environ.get("MRM_PORT") or os.environ.get("FOREMAN_PORT") or "8777")

DEFAULT_CONFIG = {
    # Codex homes to run workers on. Add more with `mrm account add`.
    "accounts": {"a": {"kind": "headless", "home": str(Path.home() / ".codex")}},
    # Opinionated defaults for every assignment; override per assignment.
    "defaults": {"timebox_minutes": 25, "size": "medium", "model": None, "effort": None},
    # Weekly-limit resets mrm may spend automatically when every account is exhausted.
    "auto_resets_allowed": 0,
    # MCP servers workers may use (others in the account's config are switched off for workers).
    "worker_mcp_allow": [],
    # A file that, while it exists, stops new assignments (e.g. a disk guard's low-space flag). Its text
    # is shown as the reason. Empty = off.
    "stop_file": "",
}
SIZES = {"small": 200, "medium": 800, "large": 3000}   # changed-line budgets before a size flag


def load_json(name, default):
    try:
        return json.loads((HOME / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(name, data):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = HOME / f"{name}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(HOME / name)


def config():
    cfg = load_json("config.json", None)
    if cfg is None:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_json("config.json", cfg)
    for k, v in DEFAULT_CONFIG.items():   # fill keys added by newer versions
        cfg.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT_CONFIG["defaults"].items():
        cfg["defaults"].setdefault(k, v)
    # Older configs had account a as the Desktop app; run it headless on the Desktop's home instead.
    for acc in cfg["accounts"].values():
        if acc.get("kind") == "desktop":
            acc["kind"], acc["home"] = "headless", str(Path.home() / ".codex")
    return cfg


def save_config(cfg):
    save_json("config.json", cfg)


def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259                       # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class merge_lock:
    """One accept at a time per repo, across the CLI and the daemon: each accept integrates the base,
    checks and merges, and the next one must integrate *that* result, not race it into the index."""

    def __init__(self, repo, waiting=None):
        import hashlib
        self.path = HOME / "locks" / (hashlib.sha1(os.path.normcase(os.path.abspath(repo)).encode()).hexdigest()[:16] + ".lock")
        self.waiting = waiting

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        told = False
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text() or 0)
                    if pid and not pid_alive(pid):
                        self.path.unlink()
                        continue
                except (OSError, ValueError):
                    pass
                if self.waiting and not told:
                    self.waiting()
                    told = True
                time.sleep(0.5)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------- registry
class registry_lock:
    """Cross-process lock for read-modify-write of the worker registry."""

    def __enter__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        self.path = HOME / "workers.lock"
        deadline = time.time() + 30
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise SystemExit("worker registry is locked by another mrm command")
                time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


def workers():
    return load_json("workers.json", {})


def save_worker(name, _create=False, **fields):
    """Update a worker's row. Only assign creates one (_create=True): a late update from the daemon or a
    CLI command racing a scrap must not resurrect a removed worker as a partial row with no account, which
    crashed every daemon tick (seen on macOS, 2026-10-10)."""
    with registry_lock():
        w = workers()
        if name not in w and not _create:
            return None
        w[name] = dict(w.get(name, {}), **fields)
        save_json("workers.json", w)
        return w[name]


def remove_worker(name):
    with registry_lock():
        w = workers()
        w.pop(name, None)
        save_json("workers.json", w)


def worker(name):
    w = workers().get(name)
    if not w:
        raise SystemExit(f"no worker called {name!r} (see: mrm status)")
    return w


def history():
    return load_json("history.json", [])


def add_history(entry):
    h = history()
    h.append(entry)
    save_json("history.json", h[-1000:])


# ---------------------------------------------------------------------------- daemon client
def daemon_call(account, method, timeout=600, **args):
    data = json.dumps({"account": account, "method": method, "args": args}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/call", data=data,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())["result"]
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise SystemExit(f"mrm daemon ({account}): {msg}")
        except urllib.error.URLError:
            if attempt == 0 and ensure_daemon():
                continue
            raise SystemExit("the mrm daemon isn't running and couldn't be started (try: mrm setup)")


def daemon_alive():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_daemon():
    """Start the daemon if it's down: via its scheduled task when installed, else detached."""
    if daemon_alive():
        return True
    import subprocess
    import sys
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Run", "/TN", "Mr Manager daemon"], capture_output=True)
        if r.returncode != 0:
            r = subprocess.run(["schtasks", "/Run", "/TN", "Codex Foreman daemon"], capture_output=True)
        if r.returncode != 0:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve().parent.parent / "mrm.py"),
                              "daemon"], creationflags=flags, close_fds=True)
    else:
        plist = Path.home() / "Library" / "LaunchAgents" / "dev.mr-manager.daemon.plist"
        started = False
        if sys.platform == "darwin" and plist.exists():
            # The service owns the daemon. Spawning a detached copy here races it for the port: the copy wins
            # while launchd restarts, and launchd's own daemon then crash-loops on "address already in use".
            r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/dev.mr-manager.daemon"],
                               capture_output=True)
            started = r.returncode == 0
        if not started:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent.parent / "mrm.py"), "daemon"],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.25)
        if daemon_alive():
            return True
    return False


class Account:
    """CLI-side handle for one account; every call goes through the daemon."""
    kind = "headless"

    def __init__(self, name):
        self.name = name

    def __getattr__(self, method):
        return lambda **kw: daemon_call(self.name, method, **kw)


LOCAL = None   # set inside the daemon: account -> its Headless host, so the daemon never calls itself over HTTP


def backend(account):
    acc = config()["accounts"].get(account)
    if not acc:
        raise SystemExit(f"unknown account {account!r}; accounts: {', '.join(config()['accounts'])}")
    return LOCAL(account) if LOCAL else Account(account)


def legacy_desktop():
    """Threads created by earlier versions on the Desktop app (read/steer only)."""
    from .desktop import Desktop
    state = load_json("state.json", {})
    d = Desktop(state.get("home_thread"))
    if not d.caller:
        d.caller = any_existing_thread()
    return d


def any_existing_thread():
    db = Path.home() / ".codex" / "state_5.sqlite"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = c.execute("select id from threads where archived=0 order by updated_at desc limit 1").fetchone()
    if not r:
        raise SystemExit("no Codex thread found")
    return r[0]
