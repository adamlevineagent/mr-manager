"""Accepting a lane: the one implementation used by `mrm accept` and by the watchdog's auto-accept.

Order matters:
  1. the worker must be idle and its lane committed
  2. the base's newer commits are merged INTO the lane (conflicts go back to the worker)
  3. the check runs on that integrated state, i.e. exactly what will land
  4. lane processes are stopped, the lane is merged into the base, then removed
"""
import subprocess
import time
from contextlib import ExitStack, nullcontext
from pathlib import Path

from . import core, lanes, lifecycle


class NotAccepted(Exception):
    def __init__(self, kind, message, output="", integrated=0):
        super().__init__(message)
        self.kind, self.output = kind, output   # kind: running | dirty | conflict | check | merge
        self.integrated = integrated            # newer base commits merged into the lane just before the check


def integrate_base(name, w, notify_worker=True):
    wt, base = w["workdir"], w["base"]
    behind = int(lifecycle.git(wt, "rev-list", "--count", f"HEAD..{base}", check=False) or 0)
    if not behind:
        return 0
    r = subprocess.run(["git", "-C", wt, *lifecycle.ident(wt), "merge", "--no-edit", "-m", f"Merge {base} into worker/{name}", base],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return behind
    conflicts = lifecycle.git(wt, "diff", "--name-only", "--diff-filter=U", check=False)
    lifecycle.git(wt, "merge", "--abort", check=False)
    if not conflicts:
        raise NotAccepted("merge", f"couldn't bring {base} into the lane: {(r.stderr or r.stdout).strip()}")
    files = conflicts.replace("\n", ", ")
    if notify_worker:
        core.backend(w["account"]).send(thread_id=w["thread"], env=w.get("env"), text=(
            f"Manager: `{base}` moved on and conflicts with your branch in: {files}. Merge `{base}` into your "
            "branch (or rebase onto it) and resolve the conflicts keeping both intents. Don't drop or undo "
            f"`{base}`'s side. Make sure the check passes, commit, and send your report."))
        # a conflict resolution reconciles someone else's work: a person looks at it, even on --auto lanes
        core.save_worker(name, state="working", clock_from=time.time(), wrapup_sent=None, stop_sent=None,
                         **({"auto": False, "auto_off": f"resolved a merge conflict with {base}; review it"}
                            if w.get("auto") else {}))
    raise NotAccepted("conflict", f"{base} moved on and conflicts with {name} in: {files}. "
                      + ("The worker has been asked to resolve it." if notify_worker else
                         f"Merge {base} in {wt}, resolve and commit, then accept {name} again."))


def accept(name, w, commit=False, check=None, skip_check=False, message=None, force=False, log=print):
    """Merge the lane if it's ready. Raises NotAccepted with the reason otherwise."""
    branch = f"worker/{name}"
    listed = lifecycle.git(w["repo"], "worktree", "list", "--porcelain", check=False)
    if not any(Path(l[9:]) == Path(w["workdir"]) for l in listed.splitlines() if l.startswith("worktree ")):
        # git no longer knows the lane (an earlier accept merged it, then hit a locked folder): finish the cleanup
        if lifecycle.git(w["repo"], "branch", "--list", branch, check=False) and \
                lifecycle.ahead(w["repo"], w["base"], branch):
            raise NotAccepted("merge", f"{name}'s worktree is gone but {branch} has unmerged commits. "
                                       f"Merge it by hand (git merge {branch}), or scrap --force to drop them.")
        merge_secondaries(name, w, commit=commit, log=log)
        pending = lifecycle.cleanup(w["repo"], w["workdir"], branch, force=False)
        return {"merged_commits": 0, "base": w["base"], "head": None, "cleanup_pending": pending}
    st = core.backend(w["account"]).status(thread_id=w["thread"])
    if st.get("running") and not force:
        raise NotAccepted("running", f"{name} is still working")
    dirty = lifecycle.dirty_files(w["workdir"])
    if dirty and not commit:
        raise NotAccepted("dirty", f"{name} has uncommitted changes:\n  " + "\n  ".join(dirty[:15]))
    if dirty:
        lifecycle.git(w["workdir"], "add", "-A")
        lifecycle.git(w["workdir"], *lifecycle.ident(w["workdir"]), "commit", "-m", f"{name}: uncommitted work at accept")
    secondary = prepare_secondaries(name, w, commit=commit)
    # Cross-repo lanes take locks in the same order, so two accepts cannot deadlock.
    with ExitStack() as locks:
        for repo in sorted({str(Path(r).resolve()) for r in [w["repo"]] + [s["repo"] for s in secondary]}):
            locks.enter_context(core.merge_lock(repo, waiting=lambda: log(
                "another accept into this repo is running; queued behind it")))
        behind = integrate_base(name, w)
        if behind:
            log(f"brought {behind} newer commit(s) from {w['base']} into the lane before checking")
        check = lanes.native_check(check) or w.get("check")
        if check and not skip_check:
            ok, out = lanes.run_check(check, w["workdir"], timeout=w.get("check_timeout"))
            if not ok:
                raise NotAccepted("check", f"check failed: {check}", out, integrated=behind)
            log(f"check passed: {check}")
        killed = lanes.kill_lane_processes(w["workdir"], w.get("port_base"), w.get("port_count") or 0, name=name)
        if killed:
            log(f"stopped {len(killed)} process(es) still running in the lane")
        try:
            merge_secondaries(name, w, commit=commit, log=log, locked=True)
            return lifecycle.finish(w, name, message=message or f"Merge worker/{name}: {w.get('outcome', '')[:60]}")
        except lifecycle.LifecycleError as e:
            raise NotAccepted("merge", str(e))


def prepare_secondaries(name, w, commit=False):
    owned = lifecycle.secondaries(name, w)
    for s in owned:
        if not s["base"] or s["base"] == "HEAD" or s["base"].startswith("worker/"):
            raise NotAccepted("merge", f"{s['repo']} needs its destination branch checked out; "
                              f"check out main (or the intended base), then accept {name} again")
        if s["workdir"] and Path(s["workdir"]).exists():
            dirty = lifecycle.dirty_files(s["workdir"])
            if dirty and not commit:
                raise NotAccepted("dirty", f"{s['branch']} in {s['workdir']} has uncommitted changes:\n  "
                                  + "\n  ".join(dirty[:15]))
    for s in owned:
        if commit and s["workdir"] and Path(s["workdir"]).exists() and lifecycle.dirty_files(s["workdir"]):
            lifecycle.git(s["workdir"], "add", "-A")
            lifecycle.git(s["workdir"], *lifecycle.ident(s["workdir"]), "commit", "-m",
                          f"{name}: uncommitted secondary work at accept")
    return owned


def merge_secondaries(name, w, commit=False, log=print, locked=False):
    """Merge owned secondary branches into each repo's checked-out base, then clean up."""
    results = []
    for s in prepare_secondaries(name, w, commit=commit):
        try:
            with nullcontext() if locked else core.merge_lock(s["repo"]):
                if s["workdir"] and Path(s["workdir"]).exists():
                    integrate_base(name, s, notify_worker=False)
                    lanes.kill_lane_processes(s["workdir"], None, 0, name=name)
                r = lifecycle.finish(s, name)
                results.append(r)
                log(f"{s['branch']}: merged {r['merged_commits']} commit(s) into {s['base']} in {s['repo']}")
        except lifecycle.LifecycleError as e:
            raise NotAccepted("merge", str(e))
    return results


def close(name, w, result, extra=None):
    """Archive the thread and move the worker to history (keeping its outcome and final report)."""
    try:
        core.backend(w["account"]).archive(thread_id=w["thread"])
    except SystemExit:
        pass
    core.add_history(dict({k: v for k, v in w.items() if k not in ("brief_text", "env", "auto")}, name=name,
                          result=result, closed=time.time(), **(extra or {})))
    core.remove_worker(name)
