"""Turn an assignment into the worker's brief.

The brief is opinionated on purpose. Codex workers are capable but drift into ceremony:
harnesses, proof frameworks, instrumentation and reports about the work instead of the work.
Every brief therefore states the outcome, how it will be checked, what's out of scope, the
time box, and the standing rules, and asks for a four-line report at the end.
"""
from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parent.parent / "BRIEF-TEMPLATE.md")


def build(name, outcome, where, check=None, out_of_scope=None, context=None, timebox=None, size=None,
          readonly=False, lane=None, notes=None, branch=None, base=None, plan_first=False):
    parts = [f"## Your assignment: {name}", "", f"**Outcome:** {outcome.strip()}", ""]
    if readonly:
        parts += [f"**Where:** `{where}`. This is read-only work: don't edit, create or delete files, "
                  "and don't run git commands that change anything.", ""]
    else:
        parts += [f"**Where:** your own git worktree `{where}` on branch `{branch}` (made from `{base}`). "
                  "Work only there. Commit your work on that branch before your final report; the manager "
                  "reviews and merges it.", ""]
    if check:
        parts += [f"**How it will be checked:** the manager will run `{check}` in your worktree and it must "
                  "pass. Run it yourself before you report.", ""]
    if lane:
        parts += [f"**Ports and environment:** if you start any server, use only ports {lane['port_base']}–"
                  f"{lane['port_base'] + lane['port_count'] - 1} (also in $MRM_PORT_BASE and $PORT). Stop "
                  "anything you started before your final report. Put disposable build and temp files in "
                  "$MRM_SCRATCH (outside the worktree); mr-manager deletes it on accept or scrap.", ""]
    if timebox:
        parts += [f"**Time box:** about {timebox} minutes. If you're not done by then, commit what works and "
                  "report PARTIAL with what's left, rather than expanding the work.", ""]
    if size:
        parts += [f"**Expected size:** {size}. If it's turning out much bigger, stop and say why in your report "
                  "instead of pressing on.", ""]
    if out_of_scope:
        parts += [f"**Out of scope:** {out_of_scope.strip()}", ""]
    if notes:
        parts += [f"**Notes for this repo:** {notes.strip()}", ""]
    if context:
        parts += ["**Read first:**"] + [f"- `{c}`" for c in context] + [""]
    if plan_first:
        parts += ["## Plan first", "",
                  "First investigate the task and the owner's standing rules. Reply with a message starting "
                  "PLAN: what you will change and why, the files involved, how you will verify it, and risks. "
                  "Make no code changes yet. Stop and wait until the manager reviews your plan and tells you "
                  "to build. This PLAN replaces the final report for this turn; the time box starts at approval.", ""]
    body = "\n".join(parts).strip()
    rules = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else "{brief}"
    return rules.replace("{brief}", body)


def wrapup_order(minutes):
    return (f"Manager: you're past the {minutes}-minute time box. Wrap up now: keep only what's needed for "
            "the outcome, delete any scaffolding you added, make sure the check passes or note exactly why "
            "not, commit, stop any servers you started, and send your final report (DONE / PARTIAL / BLOCKED).")


def stop_order():
    return ("Manager: stop now. Commit whatever works as-is, stop any servers, and send your final report "
            "in the four-line format. Don't start anything new.")


def ceremony_nudge(paths):
    listed = ", ".join(f"`{p}`" for p in paths[:8])
    return (f"Manager: you've added {listed}. Unless the brief asked for it, that's scaffolding, not the "
            "outcome. Delete it and put the effort into making the outcome work. If it truly is needed for "
            "the outcome, keep it and say why in one line in your report.")
