"""The shared, read-only status board. HTTP reads never run git or contact accounts."""
import re
import subprocess
import threading
import time
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import core, lanes

_counts = {}
_lock = threading.Lock()
_watch_cache = {}
_watch_refresh = {}
_watch_busy = set()
_watch_disk = {}  # workdir -> (bytes, measured at); never persisted to watched registries.
quotas = {}  # Filled by the daemon only, from already-running account hosts.


def size_watched_lanes():
    """Called only by the sizing thread; read-only walks no more than every ten minutes."""
    for source in core.config().get("watch", []):
        try:
            ws = json.loads(Path(source.get("registry") or "").expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for w in ws.values():
            if not w.get("workdir"):
                continue
            workdir = str(Path(w["workdir"]).expanduser())
            with _lock:
                cached = _watch_disk.get(workdir)
            if cached and time.time() - cached[1] < 600:
                continue
            size = lanes.disk_usage([workdir])
            with _lock:
                _watch_disk[workdir] = (size, time.time())


def watched_rows(now):
    rows = []
    for source in core.config().get("watch", []):
        label = source.get("label") or "watched"
        registry = source.get("registry") or ""
        daemon = (source.get("daemon") or "").rstrip("/")
        # Watched daemons are local, like our own HTTP service.
        url = urlparse(daemon)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1"):
            continue
        try:
            ws = json.loads(Path(registry).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = (registry, daemon)
        with _lock:
            due = key not in _watch_busy and now - _watch_refresh.get(key, 0) >= 5
            if due:
                _watch_refresh[key] = now
                _watch_busy.add(key)
        if due:
            threading.Thread(target=_refresh_watch, args=(key, ws), daemon=True).start()
        for name, w in ws.items():
            with _lock:
                live = _watch_cache.get((key, name, w.get("thread")))
                disk = _watch_disk.get(str(Path(w["workdir"]).expanduser())) if w.get("workdir") else None
            status = "?" if live is None else "WORKING" if live.get("running") else "IDLE"
            since = w.get("started") if status == "WORKING" else (live or {}).get("at")
            rows.append(dict(key=registry + ":" + name, name=name, status=status, source=label, watched=True,
                             repo=Path(w.get("repo") or w.get("project") or w.get("workdir") or "").name,
                             elapsed_minutes=round(max(0, (now - since) / 60), 1) if since else None,
                             disk_bytes=disk[0] if disk else None,
                             note=w.get("outcome") or "", account=w.get("account"), flags=[], auto=False))
    rows.sort(key=lambda r: (0 if r["status"] == "WORKING" else
                            1 if r["status"] == "IDLE" else 2,
                            r["elapsed_minutes"] is None,
                            -(r["elapsed_minutes"] or 0) if r["status"] == "WORKING" else
                            (r["elapsed_minutes"] or 0), r["name"]))
    return rows


def _refresh_watch(key, ws):
    for name, w in ws.items():
        live = None
        try:
            body = json.dumps(dict(account=w.get("account"), method="status",
                                   args=dict(thread_id=w.get("thread")))).encode()
            req = urllib.request.Request(key[1] + "/call", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1) as response:
                result = json.loads(response.read())["result"]
                if isinstance(result, dict) and isinstance(result.get("running"), bool):
                    live = result
        except (OSError, ValueError, KeyError):
            pass
        with _lock:
            _watch_cache[(key, name, w.get("thread"))] = live
    with _lock:
        _watch_busy.discard(key)
        _watch_refresh[key] = time.time()


def refresh_counts():
    """Refresh cheap git summaries off the HTTP request path, with bounded waits."""
    fresh = {}
    for name, w in core.workers().items():
        if not w.get("isolated") or not w.get("workdir") or not w.get("base"):
            continue
        try:
            def git(*args):
                r = subprocess.run(["git", "-C", w["workdir"], *args],
                                   capture_output=True, text=True, timeout=2)
                if r.returncode:
                    raise ValueError(r.stderr)
                return r.stdout
            commits = int(git("rev-list", "--count", f"{w['base']}..HEAD").strip())
            lines = sum(int(n) for args in (("diff", "--shortstat", f"{w['base']}...HEAD"),
                                           ("diff", "--shortstat"))
                        for n in re.findall(r"(\d+) (?:insertion|deletion)", git(*args)))
            fresh[name] = (w["workdir"], commits, lines)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    with _lock:
        _counts.clear()
        _counts.update(fresh)


def board_data():
    now = time.time()
    ws = core.workers()
    rows = []
    with _lock:
        counts = dict(_counts)
    for name, w in ws.items():
        state = w.get("state") or "unknown"
        status = {"idle": "NEEDS YOU", "working": "WORKING", "accepting": "AUTO-MERGE",
                  "parked": "PARKED"}.get(state, state.upper())
        plan_pending = w.get("plan_first") and not w.get("plan_approved")
        if state == "idle" and plan_pending and w.get("report_kind") == "PLAN":
            status = "PLAN REVIEW"
        elapsed = 0 if plan_pending else max(0, (now - (w.get("clock_from") or w.get("started") or now)) / 60)
        waiting = max(0, (now - (w.get("waiting_since") or now)) / 60)
        box = w.get("timebox")
        flags = []
        commits, lines = w.get("commits"), w.get("lines")
        cached = counts.get(name)
        if cached and cached[0] == w.get("workdir"):
            commits, lines = cached[1:]
        budget = core.SIZES.get(w.get("size") or "medium", 800)
        if lines is not None and lines > budget * 1.5:
            flags.append("big")
        if w.get("ceremony_nudged"):
            flags.append("ceremony")
        if state == "working" and not plan_pending and box and elapsed > box:
            flags.append("over time box")
        head = (w.get("report") or "").strip().splitlines()
        note = w.get("auto_note") or (head[0] if head else w.get("outcome") or w.get("parked_why") or "")
        rows.append(dict(name=name, state=state, status=status, report_kind=w.get("report_kind"),
                         elapsed_minutes=round(elapsed, 1), timebox=box,
                         waiting_minutes=round(waiting, 1), waiting_since=w.get("waiting_since"),
                         started=w.get("started"), note=" ".join(note.split())[:110],
                         commits=commits, lines=lines, flags=flags, account=w.get("account"),
                         disk_bytes=w.get("disk_bytes"),
                         auto=bool(w.get("auto")), adopted=bool(w.get("adopted"))))
    rows.sort(key=lambda r: (0 if r["status"] in ("NEEDS YOU", "PLAN REVIEW") else
                            1 if r["status"] in ("WORKING", "AUTO-MERGE") else
                            3 if r["status"] == "PARKED" else 2,
                            (r["waiting_since"] or 0) if r["status"] in ("NEEDS YOU", "PLAN REVIEW") else
                            (r["started"] or 0), r["name"]))
    recent = sorted((h for h in core.history() if (h.get("closed") or 0) >= now - 86400),
                    key=lambda h: h.get("closed") or 0, reverse=True)[:6]
    merged = [dict(name=h.get("name"), result=h.get("result"), closed=h.get("closed"),
                   auto_merged=bool(h.get("auto_merged"))) for h in recent]
    sizes = [w["disk_bytes"] for w in ws.values() if w.get("isolated") and w.get("disk_bytes") is not None]
    watched = watched_rows(now)
    sizes.extend(r["disk_bytes"] for r in watched if r["disk_bytes"] is not None)
    return dict(now=now, lanes=rows, watched=watched, merged=merged, quotas=dict(quotas),
                disk_bytes=sum(sizes) if sizes else None)
