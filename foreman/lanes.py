"""Lanes: each worker's private workspace, and the repo profile that shapes it.

A lane is a git worktree on branch worker/NAME plus:
  - a block of ports nobody else uses (MRM_PORT_BASE .. +MRM_PORT_COUNT-1, and PORT),
  - environment from the repo profile (e.g. a shared build cache),
  - shared folders linked from the main checkout (e.g. node_modules), removed safely on close,
  - process cleanup on close (dev servers and builds started in the lane).

Repo profile: an optional `.mrm.toml` at the repo root captures project lessons once:

    [mrm]
    check = "npm test"            # default acceptance check for lanes in this repo
    share = ["node_modules"]       # link these folders from the main checkout into each lane
    ports = 10                     # ports reserved per lane
    notes = "Build with ./build.ps1; never run the full e2e suite."   # added to every brief
    [mrm.env]
    CARGO_TARGET_DIR = "{repo}/../shared-target"
"""
import os
import posixpath
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

PORT_FLOOR = 20000
GB = 1_000_000_000


def disk_usage(paths):
    """Total file bytes in these lanes, excluding symlinks and Windows junctions.

    Missing or unreadable files are skipped: a worker can change files during the walk.
    lstat's reparse flag also catches junctions on Python 3.11, before isjunction existed.
    """
    total = 0
    pending = [Path(p) for p in paths]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or \
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    pending.extend(Path(e.path) for e in entries)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
        except OSError:
            continue
    return total

# File names that usually mean ceremony rather than the deliverable (unless the brief asked).
CEREMONY = re.compile(r"(harness|ledger|receipt|evidence|audit|attest|proof|instrument|telemetry|"
                      r"scaffold|verifier|witness|checklist|_report\b|report\.(md|json))", re.I)


def profile(repo):
    p = Path(repo) / ".mrm.toml"
    if not p.exists() or tomllib is None:
        return {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8")).get("mrm", {})
    except Exception as e:
        raise SystemExit(f"{p} doesn't parse: {e}")
    env = {k: str(v).replace("{repo}", str(repo)) for k, v in (data.get("env") or {}).items()}
    return dict(data, env=env)


def allocate_ports(taken_bases, count):
    base = PORT_FLOOR
    step = max(10, count)
    while base in taken_bases:
        base += step
    return base


def lane_env(port_base, port_count, prof, name):
    env = {"MRM_LANE": name, "MRM_PORT_BASE": str(port_base), "MRM_PORT_COUNT": str(port_count),
           "PORT": str(port_base)}
    env.update(prof.get("env") or {})
    return env


def link_shared(repo, wt, folders):
    """Link shared folders (e.g. node_modules) from the main checkout. Junctions on Windows."""
    made = []
    for f in folders or []:
        src, dst = Path(repo) / f, Path(wt) / f
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True)
            ok = r.returncode == 0
        else:
            try:
                os.symlink(src, dst, target_is_directory=True)
                ok = True
            except OSError:
                ok = False
        if ok:
            made.append(f)
    return made


def changed_files(repo, base, wt):
    """(path, status) for everything the lane changed vs base, committed or not."""
    out = []
    r = subprocess.run(["git", "-C", wt, "diff", "--name-status", f"{base}...HEAD"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[-1], parts[0][0]))
    r = subprocess.run(["git", "-C", wt, "status", "--porcelain"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if len(line) > 3:
            st = "A" if line[:2] == "??" else line.strip()[0]
            out.append((line[3:].strip().strip('"'), st))
    seen, uniq = set(), []
    for p, s in out:
        if p not in seen:
            seen.add(p)
            uniq.append((p, s))
    return uniq


def changed_lines(base, wt):
    total = 0
    for args in (["diff", "--shortstat", f"{base}...HEAD"], ["diff", "--shortstat"]):
        r = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True)
        total += sum(int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", r.stdout))
    return total


def ceremony_flags(files, brief):
    """New files that look like ceremony and that the brief didn't mention."""
    brief_l = (brief or "").lower()
    flags = []
    for path, status in files:
        if status != "A":
            continue
        name = Path(path).name
        if CEREMONY.search(name) and name.lower() not in brief_l and Path(name).stem.lower() not in brief_l:
            flags.append(path)
    return flags


def posix_lane_pids(name, worktree, ps_text, cwd_text):
    """Match exact MRM_LANE values and cwd paths in ps eww / lsof -F pn output."""
    pids = set()
    lane_env = re.compile(r"(?:^|\s)MRM_LANE=" + re.escape(name) + r"(?:\s|$)")
    for line in ps_text.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and lane_env.search(fields[1]):
            pids.add(int(fields[0]))

    worktree = posixpath.normpath(str(worktree))
    prefix = worktree.rstrip("/") + "/"
    pid = None
    for line in cwd_text.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid is not None:
            cwd = posixpath.normpath(line[1:])
            if cwd == worktree or cwd.startswith(prefix):
                pids.add(pid)
    return sorted(pid for pid in pids if pid > 0)


def kill_lane_processes(wt, port_base=None, port_count=0, name=None):
    """Stop lane processes and port listeners; on POSIX, allow 5s before SIGKILL.
    Windows keeps its command-line matching and taskkill behavior."""
    killed = []
    if os.name == "nt":
        wt = str(Path(wt)).lower()
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.ToLower()"
              f".Contains('{wt.replace(chr(39), chr(39) * 2)}') }} | ForEach-Object {{ $_.ProcessId }}")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        pids = {int(x) for x in r.stdout.split() if x.strip().isdigit()}
        if port_base:
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
            for line in r.stdout.splitlines():
                m = re.search(r":(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
                if m and port_base <= int(m.group(1)) < port_base + max(port_count, 1):
                    pids.add(int(m.group(2)))
        pids.discard(os.getpid())
        for pid in pids:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            if r.returncode == 0:
                killed.append(pid)
    else:
        name = name if name is not None else Path(wt).name
        wt = str(Path(wt).resolve())
        r = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True)
        parents = {}
        for line in r.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and all(field.isdigit() for field in fields):
                parents[int(fields[0])] = int(fields[1])

        if sys.platform.startswith("linux"):
            # /proc gives exact environment entries, and works without lsof installed.
            ps_rows, cwd_rows = [], []
            env_entry = os.fsencode(f"MRM_LANE={name}")
            for pid in parents:
                try:
                    if env_entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                        ps_rows.append(f"{pid} MRM_LANE={name}")
                except OSError:  # exited or belongs to another user
                    pass
                try:
                    cwd_rows.extend((f"p{pid}", f"n{os.readlink(f'/proc/{pid}/cwd')}"))
                except OSError:
                    pass
            ps_text, cwd_text = "\n".join(ps_rows), "\n".join(cwd_rows)
        else:
            r = subprocess.run(["ps", "eww", "-axo", "pid=,command="], capture_output=True, text=True)
            ps_text = r.stdout
            r = subprocess.run(["lsof", "-a", "-d", "cwd", "-F", "pn"], capture_output=True, text=True)
            cwd_text = r.stdout
        pids = set(posix_lane_pids(name, wt, ps_text, cwd_text))
        if port_base:
            for port in range(port_base, port_base + max(port_count, 1)):
                try:
                    r = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                       capture_output=True, text=True)
                except FileNotFoundError:
                    break
                pids |= {int(x) for x in r.stdout.split() if x.isdigit()}

        protected = {os.getpid()}
        pid = os.getppid()
        while pid > 0 and pid not in protected:
            protected.add(pid)
            pid = parents.get(pid, 0)
        pids -= protected | {0}
        remaining = set()
        for pid in sorted(pids):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
                remaining.add(pid)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        while remaining:
            for pid in list(remaining):
                try:
                    os.kill(pid, 0)
                except OSError:
                    remaining.discard(pid)
            if not remaining or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return killed


def last_commit_time(wt):
    r = subprocess.run(["git", "-C", wt, "log", "-1", "--format=%ct"], capture_output=True, text=True)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def native_check(cmd):
    """Git Bash/MSYS paths (/c/Users/...) in a check given on Windows -> C:/Users/..., so cmd and the
    worker's shell can both run it. Managers often live in Git Bash; the check runs under cmd."""
    if os.name != "nt" or not cmd:
        return cmd
    return re.sub(r"(?<![\w/.:-])/([a-zA-Z])/", lambda m: f"{m.group(1).upper()}:/", cmd)


def run_check(cmd, wt, timeout=None):
    """Run the acceptance check in the lane. Returns (passed, tail of output).
    Default timeout is an hour; repos with slow or lock-queued builds set check_timeout (seconds)."""
    timeout = int(timeout or 3600)
    if not cmd:
        return None, "no check declared"
    try:
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {timeout}s"
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-3000:]
