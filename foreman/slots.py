"""FIFO resource tickets shared by every repo using the same mrm home."""
import json
import os
import sqlite3
import subprocess
import time
from contextlib import contextmanager

from . import core


def capacities():
    slots = core.config().get("slots", {})
    if not isinstance(slots, dict) or any(
            not isinstance(n, str) or not n or type(v) is not int or v < 1
            for n, v in slots.items()):
        raise SystemExit('slots must map names to positive integer capacities; '
                         'set mrm config slots \'{"build":1,"db":8}\'')
    return slots


def connect():
    core.HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(core.HOME / "slots.sqlite3", timeout=30, isolation_level=None)
    db.execute("""CREATE TABLE IF NOT EXISTS tickets (
        ticket INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        pid INTEGER NOT NULL, child_pid INTEGER, command TEXT NOT NULL,
        since REAL NOT NULL, holding INTEGER NOT NULL DEFAULT 0)""")
    return db


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def prune(db):
    for ticket, pid, child in db.execute("SELECT ticket, pid, child_pid FROM tickets").fetchall():
        # A killed wrapper may leave its command running. Keep its seat until that
        # process exits, so killing the wrapper cannot exceed the resource budget.
        if not core.pid_alive(pid) and not (child and core.pid_alive(child)):
            db.execute("DELETE FROM tickets WHERE ticket = ?", (ticket,))


def run(name, command):
    if not name or not command:
        raise SystemExit("give a resource and command: mrm slot NAME -- CMD [ARG ...]")
    capacity = capacities().get(name, 1)
    db, ticket, child = connect(), None, None
    try:
        with transaction(db):
            prune(db)
            ticket = db.execute(
                "INSERT INTO tickets (name, pid, command, since) VALUES (?, ?, ?, ?)",
                (name, os.getpid(), json.dumps(command), time.time())).lastrowid
        told = False
        while child is None:
            with transaction(db):
                prune(db)
                holders = db.execute("SELECT COUNT(*) FROM tickets WHERE name = ? AND holding = 1",
                                     (name,)).fetchone()[0]
                first = db.execute("SELECT ticket FROM tickets WHERE name = ? AND holding = 0 "
                                   "ORDER BY ticket LIMIT 1", (name,)).fetchone()
                if holders < capacity and first and first[0] == ticket:
                    # Start and record the child under the admission lock; another
                    # waiter cannot pass us before our command has started.
                    child = subprocess.Popen(command)
                    db.execute("UPDATE tickets SET holding = 1, child_pid = ? WHERE ticket = ?",
                               (child.pid, ticket))
            if child is None:
                if not told:
                    print(f"slot {name}: waiting for a seat (capacity {capacity})", flush=True)
                    told = True
                time.sleep(0.1)
        return child.wait()
    except KeyboardInterrupt:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        return 130
    except OSError as e:
        raise SystemExit(f"cannot run slot {name}: {e}; check the command and mrm home permissions")
    finally:
        # Never release a seat for a command that is still running.
        if ticket is not None and (child is None or child.poll() is not None):
            with transaction(db):
                db.execute("DELETE FROM tickets WHERE ticket = ?", (ticket,))
        db.close()


def snapshot():
    """Live holders and queue lengths, also usable by a future JSON board."""
    if not (core.HOME / "slots.sqlite3").exists():
        return {}
    limits, result = capacities(), {}
    db = connect()
    try:
        with transaction(db):
            prune(db)
            for name, pid, child, command, since, holding in db.execute(
                    "SELECT name, pid, child_pid, command, since, holding FROM tickets ORDER BY ticket"):
                entry = result.setdefault(name, {"capacity": limits.get(name, 1), "holders": [], "queued": 0})
                if holding:
                    entry["holders"].append({"pid": pid, "child_pid": child,
                                             "command": json.loads(command), "since": since})
                else:
                    entry["queued"] += 1
        return result
    finally:
        db.close()


def cmd_slot(a):
    raise SystemExit(run(a.name, a.command))


def print_status(data=None):
    for name, entry in (snapshot() if data is None else data).items():
        pids = ", ".join(str(h["child_pid"] or h["pid"]) for h in entry["holders"]) or "none"
        print(f"slot {name}: {len(entry['holders'])}/{entry['capacity']} holders "
              f"(PID {pids}), {entry['queued']} queued")
