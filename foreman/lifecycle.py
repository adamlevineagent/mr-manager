"""Worker lifecycle: every isolated worker ends merged-and-cleaned or discarded. Nothing lingers.

  start --isolate  -> git worktree <repo>-wt/<name> on branch worker/<name>, base branch recorded
  finish NAME      -> worker idle + worktree clean -> merge into base -> remove worktree + branch
  discard NAME     -> remove worktree + branch without merging (unmerged commits need --force)
  gc               -> audit every repo the foreman has touched; --clean removes merged leftovers

Merging is never silent: conflicts abort the merge and leave everything in place, and a dirty or
moved main checkout is refused with the exact reason.
"""
import os
import subprocess
from pathlib import Path

from . import core


class LifecycleError(SystemExit):
    pass


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise LifecycleError(f"git {' '.join(args)} failed in {repo}:\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def ident(repo):
    """git -c args so mr-manager's own commits work in a repo with no user identity configured."""
    if git(repo, "config", "user.email", check=False):
        return []
    return ["-c", "user.name=mr-manager", "-c", "user.email=mr-manager@localhost"]


def repo_top(path):
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode:
        raise LifecycleError(f"{path} isn't inside a git repo")
    return str(Path(r.stdout.strip()))


def current_branch(repo):
    return git(repo, "branch", "--show-current", check=False)


def make_worktree(project, name, exclude=()):
    """Returns (worktree path, repo top, base branch). `exclude` (repo profile) lists gitignore-style
    patterns left out of the lane's checkout: big recorded artifacts a worker never needs (they stay in
    the index, so commits and merges are unaffected)."""
    top = repo_top(project)
    base = current_branch(top)
    if not base:
        raise LifecycleError(f"{top} is on a detached HEAD; check out the branch workers should merge into")
    lanes_dir = core.config().get("lanes_dir")
    folder = Path(lanes_dir).expanduser().absolute() / Path(top).name if lanes_dir else \
        Path(top).parent / f"{Path(top).name}-wt"
    wt = folder / name
    branch = f"worker/{name}"
    if wt.exists() or git(top, "branch", "--list", branch):
        raise LifecycleError(f"{wt} or branch {branch} already exists; finish or discard the old worker first "
                             f"(foreman gc shows leftovers)")
    wt.parent.mkdir(parents=True, exist_ok=True)
    if exclude:
        git(top, "worktree", "add", "--no-checkout", "-b", branch, str(wt), base)
        git(str(wt), "sparse-checkout", "set", "--no-cone", "/*", *[f"!{p}" for p in exclude])
        git(str(wt), "checkout", branch)
    else:
        git(top, "worktree", "add", "-b", branch, str(wt), base)
    return str(wt), top, base


def dirty_files(path):
    return [l for l in git(path, "status", "--porcelain").splitlines() if l.strip()]


def ahead(repo, base, branch):
    return int(git(repo, "rev-list", "--count", f"{base}..{branch}") or 0)


def finish(w, name, commit_leftovers=False, message=None):
    """Merge the worker's branch into its base, then remove its worktree and branch."""
    top, base, wt = w["repo"], w["base"], w["workdir"]
    branch = f"worker/{name}"
    if Path(wt).exists():
        dirty = dirty_files(wt)
        if dirty:
            if not commit_leftovers:
                raise LifecycleError(f"{name}'s worktree has uncommitted changes:\n  " + "\n  ".join(dirty[:20]) +
                                     "\nAsk the worker to commit (mrm nudge), or accept --commit to commit them as-is, "
                                     "or scrap.")
            git(wt, "add", "-A")
            git(wt, *ident(wt), "commit", "-m", f"{name}: uncommitted work at finish")
    n = ahead(top, base, branch)
    merged = None
    if n:
        if current_branch(top) == base:
            main_dirty = [l for l in dirty_files(top) if not l.startswith("??")]
            if main_dirty:
                raise LifecycleError(f"the main checkout {top} has uncommitted changes on {base}; "
                                     "commit or stash them, then finish again:\n  " + "\n  ".join(main_dirty[:20]))
            r = subprocess.run(["git", "-C", top, *ident(top), "merge", "--no-ff", "-m",
                                message or f"Merge {branch}", branch], capture_output=True, text=True)
            if r.returncode:
                conflicts = git(top, "diff", "--name-only", "--diff-filter=U", check=False)
                git(top, "merge", "--abort", check=False)
                if conflicts:
                    raise LifecycleError(f"merging {branch} into {base} conflicts in:\n  "
                                         + conflicts.replace("\n", "\n  ") +
                                         f"\nNothing was changed. Have the worker rebase onto {base} "
                                         f"(mrm reject {name} \"rebase onto {base} and resolve conflicts\"), then accept.")
                raise LifecycleError(f"merging {branch} into {base} failed (nothing was changed):\n"
                                     f"{(r.stderr or r.stdout).strip()}")
        else:
            # base isn't checked out in the main tree: only a fast-forward can move it safely
            r = subprocess.run(["git", "-C", top, "fetch", ".", f"{branch}:{base}"], capture_output=True, text=True)
            if r.returncode:
                raise LifecycleError(f"{top} is on {current_branch(top) or 'a detached HEAD'}, not {base}, and {branch} "
                                     f"isn't a fast-forward of {base}. Check out {base} in {top}, then finish again.")
        merged = git(top, "rev-parse", "--short", base)
    cleanup(top, wt, branch, force=False)
    return {"merged_commits": n, "base": base, "head": merged}


def discard(w, name, force=False):
    top, base, wt = w["repo"], w["base"], w["workdir"]
    branch = f"worker/{name}"
    exists = bool(git(top, "branch", "--list", branch))
    if exists and not force:
        n = ahead(top, base, branch)
        if n:
            raise LifecycleError(f"{branch} has {n} unmerged commit(s). discard --force to throw them away, or finish.")
    cleanup(top, wt, branch, force=True)


def unlink_links(wt):
    """Remove symlinks/junctions inside a worktree without touching their targets.
    `git worktree remove` on Windows follows junctions (e.g. a node_modules junction to the
    main checkout) and deletes the target's files. Unlink them first."""
    for root, dirs, files in os.walk(wt, followlinks=False):
        for d in list(dirs):
            p = os.path.join(root, d)
            if os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
                try:
                    os.unlink(p)
                except OSError:
                    os.rmdir(p)  # a directory junction is removed by rmdir, target untouched
                dirs.remove(d)
            elif d in (".git",):
                dirs.remove(d)
        for f in files:
            p = os.path.join(root, f)
            if os.path.islink(p):
                os.unlink(p)


def cleanup(top, wt, branch, force):
    if Path(wt).exists():
        unlink_links(wt)
        git(top, "worktree", "remove", *(["--force"] if force else []), wt)
    if git(top, "branch", "--list", branch):
        git(top, "branch", "-D" if force else "-d", branch)
    git(top, "worktree", "prune", check=False)
    try:  # remove the empty <repo>-wt folder when its last worktree goes
        Path(wt).parent.rmdir()
    except OSError:
        pass


def audit(repos, registry):
    """Every worker worktree/branch in these repos, classified. Returns a list of findings."""
    out = []
    for top in sorted(set(repos)):
        if not Path(top).exists():
            continue
        wts = {}
        cur = {}
        for line in git(top, "worktree", "list", "--porcelain", check=False).splitlines() + [""]:
            if not line:
                if cur.get("branch", "").startswith("refs/heads/worker/"):
                    wts[cur["branch"][len("refs/heads/"):]] = cur["worktree"]
                cur = {}
            elif " " in line:
                k, v = line.split(" ", 1)
                cur[k] = v
        branches = [b.strip("* +").strip() for b in git(top, "branch", "--list", "worker/*", check=False).splitlines()]
        for branch in sorted(set(branches) | set(wts)):
            name = branch[len("worker/"):]
            w = registry.get(name)
            base = (w or {}).get("base") or current_branch(top) or "HEAD"
            try:
                n = ahead(top, base, branch)
            except SystemExit:
                n = None
            kind = ("active" if w else "orphan")
            if n == 0:
                kind = "merged-leftover" if not w or w.get("finished") else kind
            out.append({"repo": top, "name": name, "branch": branch, "worktree": wts.get(branch),
                        "unmerged": n, "kind": kind, "base": base})
    return out
