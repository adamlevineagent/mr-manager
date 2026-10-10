---
name: mr-manager
description: Manage OpenAI Codex workers with `mrm` (mr-manager) so they deliver working results instead of ceremony. Use when a task can be handed off as a clear outcome with a runnable check (implementation lanes, parallel independent parts, big reading or review passes) or when work would burn your own context. Covers assigning with a check, waiting, reviewing, accepting (integrate, check, merge, clean up), rejecting, steering, and auto-accept.
---

# mr-manager: you manage, Codex workers build

You're the manager. Codex workers do the labor in their own lanes; you own the outcome and the
merge. Every worker runs in an isolated git worktree with its own ports and environment, under a
standing brief that tells it to deliver the outcome, not ceremony. A background daemon enforces time
boxes, nudges ceremony, and records when each worker starts waiting on you.

## The loop

```
mrm assign NAME "outcome a person can see" --repo PATH --check "command that must pass" [--auto]
mrm wait                    # run in the background; returns when a worker needs you (or auto-merged)
mrm status                  # NEEDS YOU / WORKING / MERGED WHILE YOU WERE AWAY
mrm review NAME             # report + check result + diff stat + flags
mrm accept NAME             # brings the base in, runs the check on the combined result, merges, cleans up
mrm reject NAME "what's wrong, concretely"     # back to work; its time box restarts
```

Also: `mrm nudge NAME "correction"` (steers mid-turn; never queued), `mrm check NAME [CMD]` (run the
check now, or change it and tell the worker), `mrm tail NAME -f`, `mrm read NAME --outputs`,
`mrm diff NAME`, `mrm scrap NAME`, `mrm park NAME "why"` (keep a finished lane on purpose; it leaves
NEEDS YOU and the watchdog ignores it), `mrm history [NAME]`, `mrm usage`, `mrm gc`.

Lanes started by older versions show up "adopted": tracked on the board, never woken, nudged or
auto-merged. Slow repos set `timebox`, `size` and `check_timeout` in `.mrm.toml`.

`accept` exits 3 when it didn't merge (still running, uncommitted work, conflict, or failed check)
and prints the next move. On a conflict the worker has already been told to resolve it: `mrm wait
NAME`, then accept again.

## Writing the assignment (this decides everything)

- **The outcome is something a person can do or see:** "`calc eval "-(2+3)*2"` prints -10", not
  "refactor the parser".
- **The check is required.** It's the contract. Make it test the outcome directly and cover the
  edges you care about. `assign` refuses without one. For truly uncheckable work, pass
  `--no-check "why"`. Repos can set a default `check` in `.mrm.toml`.
- **Say what's out of scope** with `--not-doing`. Point at key files with `--read`. Use `--size
  small|medium|large` and `--timebox MIN` when the defaults (medium, 25m) are wrong.
- **Read-only work** (research, review): `--readonly`. No lane, no edits.

## Judging the work

- **A passing check is not a full review.** The worker writes to the check. Before `accept`, read
  the diff (`mrm diff NAME`) and try the outcome yourself once, especially the edges the check
  doesn't cover. If something's off, `reject` with the exact command and what you saw.
- **Flags on the board mean look harder:** `big` (over the size budget), `ceremony nudged`
  (harness/ledger/audit-style files appeared). The worker should have deleted scaffolding; confirm it did.
- **PARTIAL or BLOCKED reports** are real answers. Decide: give it what it needs (`reject` with the
  answer), narrow the scope, or `scrap`.

## Auto-accept

`--auto` lets the daemon accept a lane itself when the worker reports DONE, the lane is in its size
budget, there are no ceremony files, and the check passes on the integrated result. A failing check
goes back to the worker with the output, twice, before it lands in NEEDS YOU. Use it for
well-checked, low-risk lanes and parallel batches. Look over **MERGED WHILE YOU WERE AWAY** on the
next `mrm status`: auto-accept trusts the check, so a weak check means weak merges.

## Hygiene is categorical

- Every lane ends in `accept` or `scrap`. `assign` refuses new work while leftovers exist; `mrm gc`
  must say clean before you call work finished.
- Accepts into one repo are queued, so parallel lanes never race the index. Each one integrates
  the result of the one before it.
- When you commit in a checkout other agents use, stage explicit paths. Never `git add -A` there.

## Accounts and quota

`mrm usage` shows each account's window. `assign` picks the account with the most headroom; pin one
with `--account`. Add accounts with `mrm account add NAME` (it signs in). The daemon runs as a
service, and `mrm setup` installs it.
