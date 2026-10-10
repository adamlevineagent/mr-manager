"""mrm (mr-manager): manage Codex workers so they deliver.

The loop:
  mrm assign NAME "outcome" --repo PATH --check "CMD"    brief a worker in its own lane
  mrm status                                             the board: what needs you, what's working
  mrm wait                                               block until a worker needs you
  mrm review NAME                                        report + check + diff + flags, to decide
  mrm accept NAME                                        check passes -> merge, clean up, archive
  mrm reject NAME "what's wrong"                         send it back with feedback
  mrm nudge NAME "correction"                            steer mid-task (lands at its next step)
  mrm scrap NAME [--force]                               throw the lane away

Also: slot NAME -- CMD | attach NAME --repo PATH | board [--url] | probe [ACCOUNT] | tail NAME | read NAME | diff NAME | history [NAME] | usage | gc [--clean] | account add/list | setup
The watchdog enforces time boxes and flags ceremony on its own. Every lane ends in accept or scrap.
assign --auto lets the watchdog accept a clean DONE itself (check passes, in budget, no ceremony);
those show up under MERGED WHILE YOU WERE AWAY on the next `mrm status`.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import brief as briefs
from . import accept as acc
from . import core, lanes, lifecycle, board, slots

ROOT = Path(__file__).resolve().parent.parent


def ago(t):
    if not t:
        return "-"
    s = time.time() - t
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s / 60)}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{int(s / 86400)}d"


# ------------------------------------------------------------------------------ accounts
def usage_of(name):
    u = core.backend(name).usage()
    rl = u.get("rateLimits") or u
    prim = rl.get("primary") or {}
    return {"used": prim.get("usedPercent", 0), "resets": prim.get("resetsAt"), "plan": rl.get("planType"),
            "window_days": round((prim.get("windowDurationMins") or 0) / 1440, 1)}


def choose_account(explicit=None):
    accounts = core.config()["accounts"]
    if explicit:
        if explicit not in accounts:
            raise SystemExit(f"unknown account {explicit!r}; accounts: {', '.join(accounts)}")
        return explicit
    best, info = None, {}
    for name in accounts:
        try:
            info[name] = usage_of(name)
        except SystemExit as e:
            info[name] = {"used": 101, "error": str(e)}
        if best is None or info[name]["used"] < info[best]["used"]:
            best = name
    if info[best]["used"] < 97:
        return best
    cfg = core.config()
    if cfg.get("auto_resets_allowed", 0) > 0:
        core.backend(best).consume_reset()
        cfg["auto_resets_allowed"] -= 1
        core.save_config(cfg)
        print(f"(every account was at its weekly limit; spent one reset on account {best})")
        return best
    when = ", ".join(f"{n} resets {time.strftime('%a %H:%M', time.localtime(i.get('resets') or 0))}"
                     for n, i in info.items() if i.get("resets"))
    raise SystemExit(f"every account is at its weekly limit ({when}). Add an account (mrm account add), "
                     "or allow resets: mrm config auto_resets_allowed 1")


# ------------------------------------------------------------------------------ assign
def cmd_assign(a):
    outcome = read_assignment_arg(a.outcome, "brief")
    detail = read_assignment_arg(a.detail, "--detail") if a.detail else None
    cfg = core.config()
    stop = cfg.get("stop_file")
    if stop and os.path.exists(os.path.expanduser(stop)):
        try:
            why = Path(os.path.expanduser(stop)).read_text(encoding="utf-8").strip()
        except OSError:
            why = ""
        raise SystemExit(f"new assignments are stopped while {stop} exists" + (f": {why}" if why else ""))
    ws = core.workers()
    if a.name in ws:
        raise SystemExit(f"there's already a worker called {a.name!r}; pick another name or scrap it first")
    sizes = lane_sizes(ws)
    total = sum(size for _, size in sizes)
    limit = float(cfg.get("max_lanes_gb") or 0)
    if limit > 0 and total > limit * lanes.GB:
        biggest = ", ".join(f"{name} {size / lanes.GB:.1f} GB" for name, size in sizes[:3])
        raise SystemExit(f"lanes on disk: {total / lanes.GB:.1f} GB, over max_lanes_gb {limit:g}; "
                         f"accept or scrap lanes first. Biggest: {biggest}")
    leftovers = backlog()
    if leftovers and not a.allow_leftovers:
        raise SystemExit("clean up first. These lanes were merged or abandoned but their worktrees remain:\n" +
                         "\n".join(f"  {f['kind']:<16} {f['branch']} in {f['repo']}" for f in leftovers) +
                         "\nRun: mrm gc --clean   (removes the merged ones; scrap the rest)")
    d = cfg["defaults"]
    repo_path = os.path.abspath(a.repo or os.getcwd())
    in_git = subprocess.run(["git", "-C", repo_path, "rev-parse", "--show-toplevel"], capture_output=True).returncode == 0
    readonly = a.readonly or not in_git
    prof = lanes.profile(lifecycle.repo_top(repo_path)) if in_git else {}
    plan_first = a.plan if a.plan is not None else bool(prof.get("plan_first"))
    # --no-check "why" means no check, even when the repo profile declares a default one.
    check = None if (a.no_check and not a.check) else lanes.native_check(a.check or prof.get("check"))
    if not readonly and not check and not a.no_check:
        raise SystemExit("How will we know it works? Give --check \"command that must pass\" (or set `check` in the "
                         "repo's .mrm.toml). For work that truly has no runnable check, pass --no-check \"why\".")
    if a.auto and not readonly and not check:
        raise SystemExit("--auto merges on a passing check, so it needs one: give --check \"command\".")
    account = choose_account(a.account)
    size = a.size or prof.get("size") or d["size"]
    timebox = a.timebox or prof.get("timebox") or d["timebox_minutes"]
    lane, workdir, repo, base, branch = None, repo_path, None, None, None
    if not readonly and prof.get("max_lanes"):
        top = lifecycle.repo_top(repo_path)
        live = [n for n, w in core.workers().items() if w.get("isolated") and w.get("repo") == top]
        if len(live) >= int(prof["max_lanes"]):
            raise SystemExit(f"{top} already has {len(live)} lane(s), its .mrm.toml max_lanes; accept or scrap one "
                             f"first: {', '.join(live)}")
    if not readonly:
        workdir, repo, base = lifecycle.make_worktree(repo_path, a.name, prof.get("exclude") or ())
        branch = f"worker/{a.name}"
        count = int(prof.get("ports", 10))
        taken = {w.get("port_base") for w in core.workers().values() if w.get("port_base")}
        port_base = lanes.allocate_ports(taken, count)
        lanes.link_shared(repo, workdir, prof.get("share"))
        lane = {"port_base": port_base, "port_count": count}
    text = briefs.build(a.name, outcome, workdir, check=check, out_of_scope=a.not_doing,
                        context=a.read, timebox=timebox, size=size, readonly=readonly, lane=lane,
                        notes=prof.get("notes"), branch=branch, base=base, plan_first=plan_first)
    if detail:
        text += "\n\n## More detail from the manager\n\n" + detail
    env = lanes.lane_env(lane["port_base"], lane["port_count"], prof, a.name) if lane else None
    tid = core.backend(account).create(prompt=text, title=f"mrm · {a.name}", cwd=workdir,
                                       model=a.model or d.get("model"), effort=a.effort or d.get("effort"), env=env)
    core.save_worker(a.name, account=account, thread=tid, project=repo_path, workdir=workdir, repo=repo, base=base,
                     isolated=bool(repo), started=time.time(), clock_from=None if plan_first else time.time(),
                     plan_first=plan_first, plan_approved=False, timebox=timebox, size=size,
                     check=check, brief_text=text, outcome=outcome[:500], state="working", readonly=readonly,
                     port_base=lane and lane["port_base"], port_count=lane and lane["port_count"],
                     shared=prof.get("share") or [], env=env, check_timeout=prof.get("check_timeout"), auto=bool(a.auto and not readonly))
    where = f"lane {workdir} (branch {branch}, ports {lane['port_base']}+)" if lane else f"{workdir} (read-only)"
    print(f"assigned {a.name} to account {account}: {where}; time box {timebox}m; check: {check or 'none'}"
          + ("; auto-accept on" if a.auto and not readonly else ""))


def read_assignment_arg(v, label):
    if re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})|%[A-Za-z_][A-Za-z0-9_]*%", v):
        raise SystemExit(f"{label} contains an unexpanded shell variable: {v!r}; "
                         "expand it in your shell or pass the literal file path")
    p = Path(v)
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read {label} {v!r}: {e}; pass a readable brief file or inline text")
    # Explicit path prefixes also cover paths with spaces; bare prose may mention files.
    pathlike = (v.startswith(("/", "./", "../", "~/", "~\\")) or "\\" in v
                or re.match(r"^[A-Za-z]:", v)
                or (not any(c.isspace() for c in v)
                    and ("/" in v or p.suffix.lower() in (".md", ".markdown", ".txt", ".rst"))))
    if pathlike:
        raise SystemExit(f"{label} file {v!r} does not exist or is not a file; "
                         "create the file or pass inline brief text")
    return v


def read_text_arg(v):
    p = Path(v)
    try:
        if len(v) < 260 and p.is_file():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return v


def backlog():
    findings = lifecycle.audit(known_repos(), core.workers())
    return [f for f in findings if f["kind"] in ("orphan", "merged-leftover")]


def known_repos():
    return lifecycle.known_repos()


def cmd_attach(a):
    w = core.worker(a.name)
    if not w.get("isolated"):
        raise SystemExit(f"{a.name} is read-only; assign an isolated lane first")
    # Use Git's main checkout even if PATH points into the secondary worktree.
    path = lifecycle.repo_top(a.repo)
    listed = lifecycle.git(path, "worktree", "list", "--porcelain")
    top = str(Path(next(l[9:] for l in listed.splitlines() if l.startswith("worktree "))).resolve())
    if lifecycle.same_repo(top, w.get("repo")):
        raise SystemExit(f"{top} is already {a.name}'s primary repo; attach a different repo")
    attached = list(dict.fromkeys(w.get("secondary_repos", []) + [top]))
    candidate = dict(w, secondary_repos=attached)
    if not any(lifecycle.same_repo(s["repo"], top) and s["workdir"]
               for s in lifecycle.secondaries(a.name, candidate)):
        raise SystemExit(f"no secondary worktree for {a.name} in {top}; create one on "
                         f"worker/{a.name} or worker/{a.name}-<suffix>, then attach again")
    core.save_worker(a.name, secondary_repos=attached)
    print(f"{a.name}: attached {top}; accept or scrap includes its secondary worktrees")


# ------------------------------------------------------------------------------ status
def lane_sizes(ws):
    """Cached sizes of all registered lanes, including idle and parked workers."""
    return sorted(((name, w.get("disk_bytes") or 0) for name, w in ws.items() if w.get("isolated")),
                  key=lambda item: item[1], reverse=True)


def print_lane_disk_usage(ws):
    sizes = lane_sizes(ws)
    if not sizes:
        return
    if not any(w.get("disk_bytes") is not None for w in ws.values() if w.get("isolated")):
        print("lanes on disk: measuring…")
        return
    total = sum(size for _, size in sizes)
    biggest = f" ({sizes[0][0]} {sizes[0][1] / lanes.GB:.1f} GB)"
    print(f"lanes on disk: {total / lanes.GB:.1f} GB{biggest}")


def live_counts(w, name):
    if not w.get("isolated"):
        return "", []
    try:
        ahead = lifecycle.ahead(w["repo"], w["base"], f"worker/{name}")
        dirty = len(lifecycle.dirty_files(w["workdir"]))
        lines = lanes.changed_lines(w["base"], w["workdir"])
        budget = core.SIZES.get(w.get("size"), 0)
        flags = []
        if budget and lines > budget * 1.5:
            flags.append(f"big: {lines} lines vs {w.get('size') or 'medium'}")
        if w.get("ceremony_nudged"):
            flags.append(f"ceremony nudged x{len(w['ceremony_nudged'])}")
        return f"{ahead} commits{', ' + str(dirty) + ' uncommitted' if dirty else ''}, {lines} lines", flags
    except SystemExit:
        return "worktree missing", ["missing"]


def merged_while_away():
    """Auto-merges since the manager last looked at the board (shown once)."""
    seen = core.load_json("seen.json", {}).get("auto_merged", 0)
    fresh = [h for h in core.history() if h.get("auto_merged") and (h.get("closed") or 0) > seen]
    if fresh:
        print("MERGED WHILE YOU WERE AWAY (look over these)")
        for h in fresh:
            head = (h.get("report") or "").strip().splitlines()
            print(f"  {h['name']:<22} {ago(h.get('closed')):>5} ago  {h.get('outcome', '')[:90]}")
            if head:
                print(f"  {'':<22} {head[0][:110]}")
        print("  -> mrm history NAME for the full report; git log in the repo for the changes\n")
        core.save_json("seen.json", dict(core.load_json("seen.json", {}), auto_merged=max(h["closed"] for h in fresh)))


def cmd_status(a):
    board.refresh_counts()
    data = board.board_data()
    slots.print_status(data["slots"])
    merged_while_away()
    if not data["lanes"]:
        print('no workers. Assign one: mrm assign NAME "outcome" --repo PATH --check "CMD"')
    group = None
    parked = []
    for row in data["lanes"]:
        status = row["status"]
        if status == "PARKED":
            parked.append(row["name"])
            continue
        heading = "NEEDS YOU" if status == "PLAN REVIEW" else "WORKING" if status in ("WORKING", "AUTO-MERGE") else status
        if heading != group:
            print(heading + (" (oldest first)" if heading == "NEEDS YOU" else ""))
            group = heading
        timing = (f"waiting {int(row['waiting_minutes'])}m" if status in ("NEEDS YOU", "PLAN REVIEW") else
                  f"{int(row['elapsed_minutes'])}m/{row['timebox'] or '-'}m")
        counts = (f"{row['commits']} commits, {row['lines']} lines" if row["commits"] is not None else "")
        flags = "  " + "; ".join(row["flags"]) if row["flags"] else ""
        print(f"  {row['name']:<22} {status:<10} {row['report_kind'] or ''} {timing}  {counts}{flags}"
              + ("  auto" if row["auto"] else "") + ("  adopted" if row["adopted"] else ""))
        if row["note"]:
            print(f"  {'':<22} {row['note']}")
    if parked:
        print(f"\nPARKED ({len(parked)}): " + ", ".join(parked))
    group = None
    for row in data["watched"]:
        if row["source"] != group:
            group = row["source"]
            print(f"\n{group} (read-only)")
        print(f"  {row['name']:<22} {row['status']:<10} {('--' if row['elapsed_minutes'] is None else str(int(row['elapsed_minutes'])) + 'm')}  {row['repo']}")
    leftovers = backlog()
    if leftovers:
        print(f"\n{len(leftovers)} leftover lane(s) on disk; run: mrm gc")
    if data["disk_bytes"] is not None:
        print(f"lanes on disk: {data['disk_bytes'] / lanes.GB:.1f} GB")


def cmd_board(a):
    url = f"http://127.0.0.1:{core.PORT}/board"
    if a.url:
        print(url)
        return
    if not core.ensure_daemon():
        raise SystemExit("the mrm daemon couldn't be started (try: mrm setup)")
    if os.name == "nt":
        candidates = [shutil.which("msedge"), shutil.which("chrome")]
        for browser, folder in (("msedge.exe", "Microsoft/Edge/Application"),
                                ("chrome.exe", "Google/Chrome/Application")):
            for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                if os.environ.get(root):
                    candidates.append(str(Path(os.environ[root]) / folder / browser))
        # Prefer Edge even when only Chrome is on PATH.
        candidates.sort(key=lambda p: 0 if p and "edge" in p.lower() else 1)
        for exe in candidates:
            if exe and Path(exe).is_file():
                try:
                    subprocess.Popen([exe, f"--app={url}"])
                    return
                except OSError:
                    pass
        os.startfile(url)
    elif sys.platform == "darwin":
        chrome = Path("/Applications/Google Chrome.app")
        user_chrome = Path.home() / "Applications/Google Chrome.app"
        if chrome.exists() or user_chrome.exists():
            subprocess.Popen(["open", "-na", str(chrome if chrome.exists() else user_chrome), "--args", f"--app={url}"])
        else:
            subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


def cmd_wait(a):
    """Block until any (or the named) worker finishes a turn, then show the board."""
    names = set(a.names) if a.names else None
    evfile = core.HOME / "events.jsonl"
    pos = evfile.stat().st_size if evfile.exists() else 0
    deadline = time.time() + a.timeout
    core.ensure_daemon()
    while time.time() < deadline:
        if evfile.exists() and evfile.stat().st_size > pos:
            with open(evfile, encoding="utf-8") as f:
                f.seek(pos)
                new = [json.loads(l) for l in f if l.strip()]
                pos = f.tell()
            hits = [e for e in new if e["event"] in ("finished", "auto_merged")
                    and (names is None or e["worker"] in names)]
            for e in new:
                if names is None or e["worker"] in names:
                    print(f"{time.strftime('%H:%M:%S', time.localtime(e['at']))} {e['worker']}: {e['event']}"
                          + (f" ({e.get('report_kind')})" if e.get("report_kind") else "")
                          + (f": {e['note']}" if e.get("note") else ""))
            if hits:
                return
        time.sleep(3)
    print("timed out; still working")
    sys.exit(2)


# ------------------------------------------------------------------------------ inspect
def last_report(w):
    if w.get("report"):
        return w["report"]
    try:
        r = core.backend(w["account"]).read(thread_id=w["thread"], turns=1)
        for t in reversed(r.get("turns") or []):
            for it in reversed(t.get("items") or []):
                if it.get("type") == "agentMessage" and it.get("text"):
                    return it["text"]
    except SystemExit:
        pass
    return ""


def cmd_review(a):
    w = core.worker(a.name)
    print(f"== {a.name}  ({w.get('state', '?')}, account {w['account']}, time box {w.get('timebox')}m)")
    print(f"outcome: {w.get('outcome', '')[:300]}\n")
    print("REPORT\n" + (last_report(w).strip() or "(none yet)") + "\n")
    if not w.get("isolated"):
        return
    counts, flags = live_counts(w, a.name)
    print(f"LANE  {counts}")
    for f in flags:
        print(f"  ⚑ {f}")
    files = lanes.changed_files(w["repo"], w["base"], w["workdir"])
    cer = lanes.ceremony_flags(files, w.get("brief_text", ""))
    if cer:
        print("  ⚑ looks like scaffolding: " + ", ".join(cer))
    print("\nCOMMITS")
    print(lifecycle.git(w["repo"], "log", "--oneline", f"{w['base']}..worker/{a.name}", check=False) or "  (none)")
    print("\nFILES")
    print(lifecycle.git(w["workdir"], "diff", "--stat", fork_point(w), check=False) or "  (no changes)")
    if w.get("check") and not a.no_check:
        print(f"\nCHECK  {w['check']}")
        ok, out = lanes.run_check(w["check"], w["workdir"], timeout=w.get("check_timeout"))
        print(("PASS" if ok else "FAIL") + "\n" + "\n".join(out.splitlines()[-25:]))
    if a.diff:
        print("\nDIFF")
        print(lifecycle.git(w["workdir"], "diff", fork_point(w), check=False))


def cmd_tail(a):
    """Follow a worker live: messages and commands as they happen."""
    w = core.worker(a.name)
    seen = set()
    while True:
        r = core.backend(w["account"]).read(thread_id=w["thread"], turns=2)
        for t in r.get("turns") or []:
            for i, it in enumerate(t.get("items") or []):
                key = (t["id"], i, it.get("type"), it.get("status"))
                if key in seen:
                    continue
                seen.add(key)
                print_item(it)
        if not a.follow:
            return
        st = core.backend(w["account"]).status(thread_id=w["thread"])
        if not st.get("running") and seen:
            return
        time.sleep(4)


def print_item(it):
    ty = it.get("type")
    if ty == "userMessage":
        print("  > " + (it.get("text") or "").strip()[:400].replace("\n", "\n    "))
    elif ty == "agentMessage":
        print("  " + (it.get("text") or "").strip().replace("\n", "\n  "))
    elif ty == "commandExecution":
        cmd = it.get("command")
        cmd = " ".join(cmd) if isinstance(cmd, list) else (cmd or "")
        print(f"  $ {cmd[-240:]}  [{it.get('exitCode', it.get('status', ''))}]")
        if it.get("output"):
            print("    " + it["output"].strip()[-1500:].replace("\n", "\n    "))
    elif ty == "fileChange":
        print("  ~ " + ", ".join(it.get("files") or []))
    elif ty == "webSearch":
        print(f"  ? {it.get('query')}")


def cmd_read(a):
    w = core.worker(a.name)
    r = core.backend(w["account"]).read(thread_id=w["thread"], turns=a.turns, outputs=a.outputs)
    for t in r.get("turns") or []:
        print(f"--- turn {t.get('id', '')[:13]} {t.get('status', '')}")
        for it in t.get("items") or []:
            print_item(it)


def fork_point(w):
    """Diff the lane against where it forked from the base, so newer base commits don't show as reverts."""
    return lifecycle.git(w["workdir"], "merge-base", w["base"], "HEAD", check=False) or w["base"]


def cmd_diff(a):
    w = core.worker(a.name)
    if not w.get("isolated"):
        raise SystemExit(f"{a.name} is read-only; nothing to diff")
    subprocess.call(["git", "-C", w["workdir"], "diff", *(["--stat"] if a.stat else []), fork_point(w)])


# ------------------------------------------------------------------------------ decide
def cmd_park(a):
    """Keep a lane on purpose: off the NEEDS YOU list, ignored by the watchdog, never auto-merged."""
    w = core.worker(a.name)
    if a.undo:
        core.save_worker(a.name, state="idle", parked_why=None, waiting_since=time.time())
        print(f"{a.name}: back on the board")
        return
    if core.backend(w["account"]).status(thread_id=w["thread"]).get("running"):
        raise SystemExit(f"{a.name} is working; park it once it's finished")
    core.save_worker(a.name, state="parked", parked_why=" ".join(a.why) or None)
    print(f"{a.name}: parked. It stays on disk, off the board's NEEDS YOU list. Undo: mrm park {a.name} --undo")


def cmd_plan(a):
    w = core.worker(a.name)
    text = w.get("plan_text") or (w.get("report") if w.get("report_kind") == "PLAN" else None)
    if not text:
        raise SystemExit(f"{a.name} has no PLAN report yet; wait for the worker to finish its plan")
    dest = Path(a.out).expanduser() if a.out else core.HOME / "plans" / f"{a.name}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    branch = f"worker/{a.name}" if w.get("isolated") else "(read-only)"
    output = f"Lane: {w['workdir']}\nBranch: {branch}\n\n" + text
    dest.write_text(output, encoding="utf-8")
    print(output)
    print(f"\nSaved plan: {dest}")


def cmd_approve(a):
    w = core.worker(a.name)
    if not w.get("plan_first") or w.get("plan_approved"):
        raise SystemExit(f"{a.name} is not awaiting plan approval")
    if w.get("state") != "idle" or w.get("report_kind") != "PLAN":
        raise SystemExit(f"{a.name} has no finished PLAN to review yet")
    review = read_text_arg(a.review)
    if not review.strip():
        raise SystemExit("nothing to send")
    first = review.lstrip().splitlines()[0]
    revise = bool(re.match(r"VERDICT\s*:?\s*(REJECT|REVISE)\b", first, re.I))
    order = ("Revise your PLAN following the review. Make no code changes yet, then stop for plan review."
             if revise else "Build it now, following the review. Then send your final report.")
    core.backend(w["account"]).send(thread_id=w["thread"], env=w.get("env"),
                                    text="Manager plan review:\n\n" + review + "\n\n" + order)
    core.save_worker(a.name, plan_approved=not revise, state="working", waiting_since=None,
                     clock_from=None if revise else time.time(), wrapup_sent=None, stop_sent=None,
                     auto_note=None, resumed=0)
    print(f"{a.name}: " + ("requested a revised PLAN" if revise else "plan approved; build started and time box restarted"))


def cmd_nudge(a):
    w = core.worker(a.name)
    text = read_text_arg(" ".join(a.text)) if a.text else ""
    if not text.strip():
        raise SystemExit("nothing to send")
    r = core.backend(w["account"]).send(thread_id=w["thread"], env=w.get("env"), text="Manager: " + text)
    core.save_worker(a.name, state="working")
    print(f"sent to {a.name} ({'steered into its running turn' if r.get('steered') else 'started a new turn'})")


def cmd_reject(a):
    w = core.worker(a.name)
    text = ("Manager review: not accepted yet. " + read_text_arg(" ".join(a.text)).strip() +
            (f"\nFix that, make sure `{w['check']}` passes, commit, and send your report." if w.get("check")
             else "\nFix that, commit, and send your report."))
    core.backend(w["account"]).send(thread_id=w["thread"], env=w.get("env"), text=text)
    core.save_worker(a.name, state="working", clock_from=time.time(), wrapup_sent=None, stop_sent=None,
                     rejected=(w.get("rejected") or 0) + 1)
    print(f"sent {a.name} back with your feedback; its time box restarts")


def cmd_check(a):
    """Change a lane's acceptance check (and tell the worker), or run it now."""
    w = core.worker(a.name)
    if a.command:
        check = lanes.native_check(" ".join(a.command))
        core.save_worker(a.name, check=check)
        core.backend(w["account"]).send(thread_id=w["thread"], env=w.get("env"), text=(
            f"Manager: the acceptance check is now `{check}` (run it in your worktree). It must pass before "
            "you report DONE."))
        print(f"{a.name}: check set to {check}; the worker has been told")
        return
    if not w.get("check"):
        raise SystemExit(f"{a.name} has no check; set one: mrm check {a.name} \"command\"")
    ok, out = lanes.run_check(w["check"], w["workdir"], timeout=w.get("check_timeout"))
    print(("PASS" if ok else "FAIL") + f": {w['check']}\n" + "\n".join(out.splitlines()[-30:]))
    sys.exit(0 if ok else 1)


def cmd_accept(a):
    w = core.worker(a.name)
    if not w.get("isolated"):
        close(a.name, w, "accepted (read-only)")
        print(f"{a.name}: accepted and archived")
        return
    try:
        r = acc.accept(a.name, w, commit=a.commit, check=a.check, skip_check=bool(a.skip_check),
                       message=a.message, force=a.force)
    except acc.NotAccepted as e:
        hint = {"running": f"mrm wait {a.name}, or mrm nudge it to wrap up.",
                "dirty": f"mrm nudge {a.name} \"commit your work\", or accept --commit to commit it as-is.",
                "conflict": f"mrm wait {a.name}, then accept again.",
                "check": f"Send it back: mrm reject {a.name} \"the check fails: <what you saw>\"",
                "merge": ""}[e.kind]
        out = ("\n" + "\n".join(e.output.splitlines()[-30:])) if e.output else ""
        print(f"not merged: {e}{out}\n{hint}".rstrip())
        sys.exit(3)
    close(a.name, w, f"accepted: merged {r['merged_commits']} commit(s) into {r['base']}")
    print(f"{a.name}: merged {r['merged_commits']} commit(s) into {r['base']}"
          + (f" (now {r['head']})" if r.get("head") else "") + "; lane removed, thread archived")
    if r.get("cleanup_pending"):
        print(f"  the lane folder is still in use (a shell or editor open in it?), so it's queued for deletion: "
              f"{r['cleanup_pending']}. The daemon retries; `mrm gc` does too.")


def cmd_scrap(a):
    w = core.worker(a.name)
    try:
        core.backend(w["account"]).interrupt(thread_id=w["thread"])
    except SystemExit:
        pass
    if w.get("isolated"):
        if not a.force and lifecycle.git(w["repo"], "branch", "--list", f"worker/{a.name}") and \
                lifecycle.ahead(w["repo"], w["base"], f"worker/{a.name}"):
            raise lifecycle.LifecycleError(f"worker/{a.name} has unmerged commits; "
                                           f"scrap {a.name} --force to throw them away, or accept")
        lifecycle.discard_secondaries(a.name, w, force=a.force)
        lanes.kill_lane_processes(w["workdir"], w.get("port_base"), w.get("port_count") or 0, name=a.name)
        lifecycle.discard(w, a.name, force=a.force)
        lanes.close_lane(a.name, w, lanes.profile(w["repo"]))
    close(a.name, w, "scrapped")
    print(f"{a.name}: scrapped" + ("; lane removed" if w.get("isolated") else ""))


close = acc.close


def cmd_gc(a):
    stuck = lifecycle.retry_deletes()
    for p in stuck:
        print(f"still in use, can't delete yet: {p}")
    findings = lifecycle.audit(known_repos() + ([a.repo] if a.repo else []), core.workers())
    if not findings:
        print("clean: no worker worktrees or branches left anywhere" if not stuck else
              "no worker worktrees or branches left; the folders above are queued for deletion")
        return
    for f in findings:
        print(f"{f['kind']:<16} {f['branch']:<30} unmerged={f['unmerged']}  {f['worktree'] or '(no worktree)'}")
    if a.clean:
        for f in findings:
            if f["kind"] == "merged-leftover":
                lifecycle.cleanup(f["repo"], f["worktree"] or "", f["branch"], force=False)
                print(f"removed {f['branch']}")


# ------------------------------------------------------------------------------ accounts & setup
def cmd_history(a):
    hs = core.history()
    if a.name:
        hs = [h for h in hs if h.get("name") == a.name]
        if not hs:
            raise SystemExit(f"no closed worker called {a.name!r}")
        h = hs[-1]
        print(f"{h['name']}: {h.get('outcome', '')}\n{h.get('result', '')} {ago(h.get('closed'))} ago; "
              f"account {h.get('account')}; thread {h.get('thread')}\n")
        print((h.get("report") or "(no report)").strip())
        return
    for h in hs[-a.limit:]:
        print(f"{ago(h.get('closed')):>5} ago  {h.get('name', '?'):<22} {(h.get('result') or '')[:40]:<40}  "
              f"{h.get('outcome', '')[:70]}")


def cmd_usage(a):
    for name, acc in core.config()["accounts"].items():
        try:
            u = usage_of(name)
            resets = time.strftime("%a %d %b %H:%M", time.localtime(u["resets"] or 0))
            print(f"account {name} ({u['plan']}, {acc['home']}): {u['used']}% of {u['window_days']}-day window "
                  f"used, resets {resets}")
        except SystemExit as e:
            print(f"account {name}: {e}")
    print(f"automatic resets allowed: {core.config().get('auto_resets_allowed', 0)}")


def cmd_account(a):
    cfg = core.config()
    if a.action == "list":
        for n, acc in cfg["accounts"].items():
            print(f"{n}: {acc['home']}")
        return
    if a.action == "add":
        if not a.name:
            raise SystemExit("mrm account add NAME [--home PATH]")
        home = Path(a.home or Path.home() / f".codex-{a.name}")
        home.mkdir(parents=True, exist_ok=True)
        src = Path.home() / ".codex"
        if src.exists() and home != src:
            for f in ("AGENTS.md",):
                if (src / f).exists() and not (home / f).exists():
                    shutil.copy(src / f, home / f)
            if (src / "skills").exists() and not (home / "skills").exists():
                shutil.copytree(src / "skills", home / "skills")
        cfg["accounts"][a.name] = {"kind": "headless", "home": str(home)}
        core.save_config(cfg)
        print(f"account {a.name} uses {home}. Sign it in now (a browser window opens):")
        subprocess.call([codex_exe(), "login"], env=dict(os.environ, CODEX_HOME=str(home)))
        return
    if a.action == "remove":
        cfg["accounts"].pop(a.name, None)
        core.save_config(cfg)
        print(f"removed account {a.name} (its Codex home is untouched)")


def codex_exe():
    from .appserver import find_codex
    return find_codex()


def cmd_config(a):
    cfg = core.config()
    if a.key is None:
        print(json.dumps(cfg, indent=2))
        return
    target, key = (cfg["defaults"], a.key) if a.key in cfg["defaults"] else (cfg, a.key)
    if a.value is None:
        print(target.get(key))
        return
    v = a.value
    try:
        v = json.loads(v)
    except ValueError:
        pass
    target[key] = v
    core.save_config(cfg)
    print(f"{a.key} = {v!r}")


def cmd_setup(a):
    from . import setup
    setup.run(a)


def cmd_probe(a):
    from . import probe
    if not probe.run(a.account):
        raise SystemExit(1)


# ------------------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="mrm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("slot", help="run a command in a FIFO resource queue (no daemon)")
    s.add_argument("name")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD [ARG ...]")
    s.set_defaults(fn=slots.cmd_slot)

    s = sub.add_parser("assign", help="brief a worker in its own lane")
    s.add_argument("name")
    s.add_argument("outcome", help="what a person can do or see when it's done (text or an existing file)")
    s.add_argument("--repo", help="the repo or folder to work in (default: current directory)")
    s.add_argument("--check", help="command that must pass for the work to be accepted")
    s.add_argument("--no-check", metavar="WHY", help="no runnable check; say why")
    s.add_argument("--not-doing", metavar="TEXT", help="what's out of scope")
    s.add_argument("--read", nargs="*", help="files the worker should read first")
    s.add_argument("--detail", help="extra detail for the brief (text or an existing file; expand shell variables first)")
    s.add_argument("--size", choices=list(core.SIZES))
    s.add_argument("--timebox", type=int, metavar="MINUTES")
    s.add_argument("--readonly", action="store_true", help="research/review: no lane, no edits")
    plans = s.add_mutually_exclusive_group()
    plans.add_argument("--plan", dest="plan", action="store_true", default=None,
                       help="investigate, send a PLAN and stop for review before building")
    plans.add_argument("--no-plan", dest="plan", action="store_false", help="override repo plan_first default")
    s.add_argument("--account")
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--allow-leftovers", action="store_true")
    s.add_argument("--auto", action="store_true",
                   help="merge it myself when it reports DONE, the check passes, it's in budget and has no ceremony")
    s.set_defaults(fn=cmd_assign)

    sub.add_parser("status", help="the board").set_defaults(fn=cmd_status)
    s = sub.add_parser("plan", help="print and save a worker's PLAN for a reviewer")
    s.add_argument("name"); s.add_argument("--out", metavar="FILE"); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("approve", help="send a plan review verbatim and start the build (or request revision)")
    s.add_argument("name"); s.add_argument("review", help="review text or a file"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("board", help="open the live split-flap board")
    s.add_argument("--url", action="store_true", help="print the board URL")
    s.set_defaults(fn=cmd_board)
    s = sub.add_parser("wait", help="block until a worker needs you"); s.add_argument("names", nargs="*")
    s.add_argument("--timeout", type=float, default=3600); s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("review", help="report, check, diff and flags for one worker"); s.add_argument("name")
    s.add_argument("--diff", action="store_true"); s.add_argument("--no-check", action="store_true")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("accept", help="check passes -> merge and clean up"); s.add_argument("name")
    s.add_argument("--check"); s.add_argument("--skip-check", metavar="WHY"); s.add_argument("--commit", action="store_true")
    s.add_argument("--message"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("reject", help="send it back with feedback"); s.add_argument("name"); s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("nudge", help="steer a worker mid-task"); s.add_argument("name"); s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_nudge)
    s = sub.add_parser("scrap", help="throw the lane away"); s.add_argument("name"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_scrap)
    s = sub.add_parser("tail", help="follow a worker live"); s.add_argument("name"); s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=cmd_tail)
    s = sub.add_parser("read", help="a worker's recent turns"); s.add_argument("name"); s.add_argument("--turns", type=int, default=2)
    s.add_argument("--outputs", action="store_true"); s.set_defaults(fn=cmd_read)
    s = sub.add_parser("diff"); s.add_argument("name"); s.add_argument("--stat", action="store_true"); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("gc", help="audit lanes left on disk"); s.add_argument("--clean", action="store_true")
    s.add_argument("--repo"); s.set_defaults(fn=cmd_gc)
    s = sub.add_parser("attach", help="register a lane's secondary worktrees in another repo")
    s.add_argument("name"); s.add_argument("--repo", required=True); s.set_defaults(fn=cmd_attach)
    sub.add_parser("usage", help="quota on each account").set_defaults(fn=cmd_usage)
    s = sub.add_parser("park", help="keep a finished lane on purpose, off NEEDS YOU"); s.add_argument("name")
    s.add_argument("why", nargs="*"); s.add_argument("--undo", action="store_true"); s.set_defaults(fn=cmd_park)
    s = sub.add_parser("check", help="run a lane's check now, or change it"); s.add_argument("name")
    s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("history", help="closed workers, or one's final report"); s.add_argument("name", nargs="?")
    s.add_argument("--limit", type=int, default=20); s.set_defaults(fn=cmd_history)
    s = sub.add_parser("account", help="add/list/remove Codex accounts"); s.add_argument("action", choices=["add", "list", "remove"])
    s.add_argument("name", nargs="?"); s.add_argument("--home"); s.set_defaults(fn=cmd_account)
    s = sub.add_parser("config", help="show or set settings"); s.add_argument("key", nargs="?"); s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("setup", help="install everything and run a smoke test")
    s.add_argument("--skip-smoke", action="store_true"); s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("probe", help="prove each account can complete a real Codex turn (no daemon)")
    s.add_argument("account", nargs="?"); s.set_defaults(fn=cmd_probe)
    sub.add_parser("daemon", help="run the daemon (normally a background service)").set_defaults(
        fn=lambda a: __import__("foreman.daemon", fromlist=["main"]).main())

    # foreman-era names keep working
    aliases = {"start": "assign", "steer": "nudge", "finish": "accept", "discard": "scrap", "stop": "nudge"}
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in aliases:
        argv = legacy_args(argv)
    a = ap.parse_args(argv)
    a.fn(a)


def legacy_args(argv):
    """Translate foreman-era invocations (start --brief, steer, finish, discard, stop)."""
    cmd, rest = argv[0], argv[1:]
    if cmd == "start":
        name = rest[0]
        out, i, brief = [], 1, None
        while i < len(rest):
            if rest[i] == "--brief":
                brief = rest[i + 1]; i += 2
            elif rest[i] == "--project":
                out += ["--repo", rest[i + 1]]; i += 2
            elif rest[i] in ("--title", "--branch"):
                i += 2
            elif rest[i] in ("--shared",):
                out.append("--readonly"); i += 1
            elif rest[i] in ("--worktree", "--isolate", "--raw", "--force"):
                i += 1
            else:
                out.append(rest[i]); i += 1
        if "--check" not in out and "--readonly" not in out:
            out += ["--no-check", "foreman-era start without a check"]
        return ["assign", name, brief or "", *out]
    if cmd == "stop":
        return ["nudge", rest[0], "Stop the current work now, commit what works, and send your final report."]
    return [{"steer": "nudge", "finish": "accept", "discard": "scrap"}[cmd], *rest]


if __name__ == "__main__":
    main()
