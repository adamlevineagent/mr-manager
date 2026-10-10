"""The always-on daemon: hosts one headless app-server per account and runs the watchdog.

The watchdog is where the opinions enforce themselves, whether or not anyone is watching:
  - time boxes: a wrap-up order at the time box, a stop order at 1.5x
  - ceremony: a nudge when a lane adds harness/audit/ledger-style files the brief didn't ask for
  - attention: when a worker finishes, its report and the time it started waiting are recorded,
    so `mrm status` can say what needs the manager and for how long
  - auto-accept (lanes assigned with --auto): a DONE report with no ceremony files, inside its size
    budget, is integrated, checked and merged exactly as `mrm accept` would; a failing check goes
    back to the worker (twice) before it lands in NEEDS YOU
  - restarts: lanes a restart cut off mid-turn are told to pick up where they left off
Every event is appended to events.jsonl, which `mrm wait` follows.
"""
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import brief as briefs
from . import accept as acc
from . import core, lanes, lifecycle, board

TICK = 20
DISK_INTERVAL = 180
AUTO_REJECT_LIMIT = 2


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def event(name, kind, **data):
    core.HOME.mkdir(parents=True, exist_ok=True)
    with open(core.HOME / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "worker": name, "event": kind, **data}) + "\n")


def report_kind(text):
    head = (text or "").strip().lstrip("-*• ").upper()
    for k in ("DONE", "PARTIAL", "BLOCKED"):
        if head.startswith(k) or head.startswith(f"**{k}"):
            return k
    return None


class Daemon:
    def __init__(self):
        from .headless import Headless
        self.Headless = Headless
        self.hosts = {}
        self.lock = threading.Lock()
        self.accepting = set()
        core.LOCAL = self.host

    def host(self, account):
        with self.lock:
            if account not in self.hosts:
                cfg = core.config()
                acc = cfg["accounts"][account]
                self.hosts[account] = self.Headless(acc["home"], mcp_allow=cfg.get("worker_mcp_allow", []),
                                                    worker_root=core.HOME / "homes",
                                                    inherit=cfg.get("worker_inherit", []),
                                                    log=lambda *a: log(account, *a))
            return self.hosts[account]

    # -------------------------------------------------------------------------- watchdog
    def watch(self):
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(TICK)

    def tick(self):
        now = time.time()
        with self.lock:
            hosts = list(self.hosts.values())
        for h in hosts:
            h.sync_auth()
        if now - getattr(self, "deletes_tried", 0) > 120:
            self.deletes_tried = now
            try:
                lifecycle.retry_deletes()
            except Exception as e:
                log("retrying lane folder deletes failed", e)
        for name, w in core.workers().items():
            if w.get("legacy") or w["account"] not in core.config()["accounts"] or name in self.accepting \
                    or w.get("state") == "parked":
                continue
            h = self.host(w["account"])
            if not h.server:
                h.ensure()
            st = h.status(w["thread"])
            if w.get("adopted") or ("state" not in w and "clock_from" not in w):
                upd = self.adopted(name, w, st)
            elif st.get("running"):
                upd = self.while_running(name, w, h, now)
            elif w.get("state") in ("working", None):
                upd = self.on_stopped(name, w, h, st, now)
            elif w.get("state") == "accepting":      # a restart interrupted an auto-accept
                upd = {"state": "idle", "auto_note": "auto-accept was interrupted by a restart; accept it by hand"}
                event(name, "finished", report_kind=w.get("report_kind"), note=upd["auto_note"])
            else:
                upd = {}
            if upd:
                core.save_worker(name, **upd)

    def watch_disk(self):
        """One sizing thread: slow file walks never hold up the watchdog."""
        while True:
            try:
                self.size_lanes()
                board.size_watched_lanes()
            except (Exception, SystemExit):
                traceback.print_exc()
            time.sleep(TICK)

    def size_lanes(self):
        for name, w in core.workers().items():
            # Parked and legacy lanes still occupy disk, even when their threads aren't watched.
            if not w.get("isolated") or not w.get("workdir") or \
                    time.time() - (w.get("disk_checked") or 0) < DISK_INTERVAL:
                continue
            size = lanes.disk_usage([w["workdir"]])
            with core.registry_lock():
                ws = core.workers()
                if name in ws and ws[name].get("workdir") == w["workdir"]:
                    ws[name].update(disk_bytes=size, disk_checked=time.time())
                    core.save_json("workers.json", ws)

    def adopted(self, name, w, st):
        """Lanes started by an older version: tracked on the board, never woken, nudged or auto-merged.
        Their briefs predate the watchdog's rules, and their owners may be keeping them idle on purpose."""
        running = bool(st.get("running"))
        upd = {}
        if not w.get("adopted"):
            upd["adopted"] = True
            if not running:   # its report is in the thread; it's been waiting since its last commit
                try:
                    msg = self.host(w["account"]).last_message(w["thread"])[1]
                except Exception:
                    msg = ""
                since = lanes.last_commit_time(w["workdir"]) if w.get("isolated") else None
                upd.update(state="idle", report=msg[:4000], report_kind=report_kind(msg),
                           waiting_since=max(since or 0, w.get("started") or 0) or time.time())
                return upd
        if w.get("state") == "parked":
            return upd
        if running and w.get("state") != "working":
            upd["state"] = "working"
        elif not running and w.get("state") != "idle":
            upd.update(state="idle", waiting_since=w.get("waiting_since") or time.time())
            if w.get("state") == "working":       # watched it finish under this daemon
                msg = st.get("message") or ""
                upd.update(report=msg[:4000], report_kind=report_kind(msg), waiting_since=time.time())
                event(name, "finished", report_kind=upd["report_kind"], status=st.get("status"))
        return upd

    def while_running(self, name, w, h, now):
        upd = {"state": "working"} if w.get("state") != "working" else {}
        started = w.get("clock_from") or w.get("started") or now
        box = w.get("timebox")
        if box and not w.get("wrapup_sent") and now - started > box * 60:
            h.send(w["thread"], briefs.wrapup_order(box), env=w.get("env"))
            upd["wrapup_sent"] = now
            event(name, "timebox", minutes=box)
        elif box and w.get("wrapup_sent") and not w.get("stop_sent") and now - started > box * 90:
            h.send(w["thread"], briefs.stop_order(), env=w.get("env"))
            upd["stop_sent"] = now
            event(name, "stop_order")
        if w.get("isolated") and now - w.get("ceremony_checked", 0) > 120:
            upd["ceremony_checked"] = now
            try:
                flags = [f for f in self.ceremony(w) if f not in (w.get("ceremony_nudged") or [])]
                if flags:
                    h.send(w["thread"], briefs.ceremony_nudge(flags), env=w.get("env"))
                    upd["ceremony_nudged"] = (w.get("ceremony_nudged") or []) + flags
                    event(name, "ceremony", files=flags)
            except Exception as e:
                log("ceremony check failed", name, e)
        return upd

    def on_stopped(self, name, w, h, st, now):
        """A lane marked working has no running turn: it finished, or a restart cut it off."""
        message, status = st.get("message"), st.get("status")
        if not status:
            # nothing seen since this daemon started: ask the thread itself how its last turn ended
            try:
                status, message = h.last_message(w["thread"])
            except Exception as e:
                log("couldn't read", name, e)
                return {}
            if not status and not message and now - (w.get("clock_from") or w.get("started") or now) < 180:
                # a brand-new lane whose first turn the app-server hasn't reported yet: not finished.
                # (Seen on macOS: wait returned "finished" seconds after assign, then accept said "still working".)
                return {}
            if status in ("interrupted", "inProgress", "failed") and (w.get("resumed") or 0) < 3:
                h.send(w["thread"], "Manager: you were cut off by a restart on my side. Pick up where you left "
                                    "off, then finish and send your report.", env=w.get("env"))
                event(name, "resumed", was=status)
                return {"resumed": (w.get("resumed") or 0) + 1}
        elif not message:
            try:
                message = h.last_message(w["thread"])[1]
            except Exception:
                message = ""
        kind = report_kind(message)
        upd = dict(state="idle", report=(message or "")[:4000], report_kind=kind, waiting_since=now, resumed=0)
        if w.get("auto_off"):
            upd["auto_note"] = w["auto_off"]
        if w.get("auto") and w.get("isolated"):
            why = self.auto_blocker(w, kind)
            if not why:
                core.save_worker(name, **dict(upd, state="accepting"))
                self.accepting.add(name)
                threading.Thread(target=self.auto_accept, args=(name,), daemon=True, name=f"accept-{name}").start()
                return {}
            upd["auto_note"] = why
        event(name, "finished", report_kind=kind, status=status, note=upd.get("auto_note"))
        return upd

    # -------------------------------------------------------------------------- auto-accept
    def ceremony(self, w):
        return lanes.ceremony_flags(lanes.changed_files(w["repo"], w["base"], w["workdir"]), w.get("brief_text", ""))

    def auto_blocker(self, w, kind):
        """Why this lane needs the manager's eyes instead of an automatic merge (None = it doesn't)."""
        if kind != "DONE":
            return f"reported {kind or 'no status'}"
        try:
            flags = self.ceremony(w)
            lines = lanes.changed_lines(w["base"], w["workdir"])
        except Exception as e:
            return f"couldn't inspect the lane: {e}"
        if flags:
            return "ceremony files: " + ", ".join(flags[:5])
        budget = core.SIZES.get(w.get("size") or "medium", 800)
        if lines > budget * 1.5:
            return f"{lines} lines changed, over the {w.get('size') or 'medium'} budget"
        if lines == 0:
            return "DONE but nothing changed"
        return None

    def auto_accept(self, name):
        try:
            w = core.worker(name)
            try:
                r = acc.accept(name, w, commit=True, log=lambda m: log(name, m))
            except acc.NotAccepted as e:
                self.auto_failed(name, w, e)
                return
            acc.close(name, w, f"auto-accepted: merged {r['merged_commits']} commit(s) into {r['base']}",
                      extra={"auto_merged": True, "report": (w.get("report") or "")[:1500]})
            event(name, "auto_merged", commits=r["merged_commits"], base=r["base"])
            log(name, "auto-merged", r)
        except BaseException as e:   # never let a lane wedge in "accepting"
            log(name, "auto-accept crashed", repr(e))
            core.save_worker(name, state="idle", auto_note=f"auto-accept crashed: {e}")
            event(name, "finished", report_kind="DONE", note=f"auto-accept crashed: {e}")
        finally:
            self.accepting.discard(name)

    def auto_failed(self, name, w, e):
        if e.kind == "check" and w.get("auto_rejects", 0) < AUTO_REJECT_LIMIT:
            tail = "\n".join(e.output.splitlines()[-40:])
            base = w["base"]
            if e.integrated:
                # The base moved under the lane. Whatever the worker does next reconciles someone else's
                # newer work with this lane, so it must not undo that work, and a person reviews the result.
                text = (f"Manager review: not accepted yet. `{base}` gained {e.integrated} newer commit(s), which I "
                        f"merged into your branch, and now `{w['check']}` fails:\n```\n{tail}\n```\n"
                        f"Make your change work on top of `{base}`'s newer commits. Don't revert or undo them. "
                        f"If they truly contradict the check or your outcome, stop and report BLOCKED with what "
                        f"conflicts. Otherwise make the check pass, commit, and send your report.")
                upd = {"auto": False, "auto_off": f"the check broke after {base} moved on; review how it was reconciled"}
            else:
                text = (f"Manager review: not accepted yet. `{w['check']}` fails:\n```\n{tail}\n```\n"
                        "Fix that, make sure the check passes, commit, and send your report.")
                upd = {}
            self.host(w["account"]).send(w["thread"], text, env=w.get("env"))
            core.save_worker(name, state="working", clock_from=time.time(), wrapup_sent=None, stop_sent=None,
                             auto_rejects=w.get("auto_rejects", 0) + 1, auto_note=None, **upd)
            event(name, "auto_rejected", reason=str(e), base_moved=e.integrated)
        elif e.kind == "conflict":
            event(name, "auto_conflict", reason=str(e))      # the worker was already sent to resolve it
        else:
            core.save_worker(name, state="idle", auto_note=f"auto-accept stopped: {e}")
            event(name, "finished", report_kind="DONE", note=f"auto-accept stopped: {e}")

    # -------------------------------------------------------------------------- http
    def watch_board(self):
        counts_at = quota_at = 0
        while True:
            try:
                now = time.time()
                if now - counts_at >= 30:
                    board.refresh_counts()
                    counts_at = now
                if now - quota_at >= 60:
                    with self.lock:
                        hosts = list(self.hosts.items())
                    for name, host in hosts:
                        if not host.server or not host.server.alive:
                            continue
                        try:
                            usage = host.server.request("account/rateLimits/read", {}, timeout=1)
                            limits = usage.get("rateLimits") or usage
                            primary = limits.get("primary") or {}
                            board.quotas[name] = {"used": primary.get("usedPercent"),
                                                 "resets": primary.get("resetsAt"), "at": now}
                        except Exception:
                            board.quotas.pop(name, None)
                    quota_at = now
                board.board_data()  # Refresh watched-daemon status caches in the background.
            except Exception:
                traceback.print_exc()
            time.sleep(5)

    def serve(self):
        d = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"ok": True, "accounts": list(d.hosts), "version": 2})
                elif self.path == "/board.json":
                    self._send(200, board.board_data())
                elif self.path == "/board":
                    data = Path(__file__).with_name("board.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._send(404, {"error": "no route"})

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    h = d.host(body["account"])
                    result = getattr(h, body["method"])(**body.get("args", {}))
                    self._send(200, {"result": result})
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})

        srv = ThreadingHTTPServer(("127.0.0.1", core.PORT), H)
        srv.daemon_threads = True
        threading.Thread(target=self.watch, daemon=True, name="watchdog").start()
        threading.Thread(target=self.watch_disk, daemon=True, name="disk-sizing").start()
        threading.Thread(target=self.watch_board, daemon=True, name="board-cache").start()
        log(f"mr-manager daemon on 127.0.0.1:{core.PORT}; home {core.HOME}")
        srv.serve_forever()


def main():
    Daemon().serve()
