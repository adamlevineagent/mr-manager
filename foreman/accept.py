"""Accepting a lane: the one implementation used by `mrm accept` and by the watchdog's auto-accept.

Order matters:
  1. the worker must be idle and its lane committed
  2. the base's newer commits are merged INTO the lane (conflicts go back to the worker)
  3. the check runs on that integrated state, i.e. exactly what will land
  4. lane processes are stopped, the lane is merged into the base, then removed
"""
import subprocess
import time

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
                                  f"The worker has been asked to resolve it.")


def accept(name, w, commit=False, check=None, skip_check=False, message=None, force=False, log=print):
    """Merge the lane if it's ready. Raises NotAccepted with the reason otherwise."""
    st = core.backend(w["account"]).status(thread_id=w["thread"])
    if st.get("running") and not force:
        raise NotAccepted("running", f"{name} is still working")
    dirty = lifecycle.dirty_files(w["workdir"])
    if dirty and not commit:
        raise NotAccepted("dirty", f"{name} has uncommitted changes:\n  " + "\n  ".join(dirty[:15]))
    if dirty:
        lifecycle.git(w["workdir"], "add", "-A")
        lifecycle.git(w["workdir"], *lifecycle.ident(w["workdir"]), "commit", "-m", f"{name}: uncommitted work at accept")
    with core.merge_lock(w["repo"], waiting=lambda: log("another accept into this repo is running; queued behind it")):
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
            return lifecycle.finish(w, name, message=message or f"Merge worker/{name}: {w.get('outcome', '')[:60]}")
        except lifecycle.LifecycleError as e:
            raise NotAccepted("merge", str(e))


def close(name, w, result, extra=None):
    """Archive the thread and move the worker to history (keeping its outcome and final report)."""
    try:
        core.backend(w["account"]).archive(thread_id=w["thread"])
    except SystemExit:
        pass
    core.add_history(dict({k: v for k, v in w.items() if k not in ("brief_text", "env", "auto")}, name=name,
                          result=result, closed=time.time(), **(extra or {})))
    core.remove_worker(name)
