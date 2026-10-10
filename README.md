# mr-manager

**Tired of ChatGPT wrapping your project in safety tape without delivering results? Let Opus keep
those dudes in line and delivering like the stallions they were meant to be.**

`mrm` turns OpenAI Codex into a crew of workers that a manager (you, or better, Claude Code) runs
like a lead engineer: a clear outcome, a check that must pass, a time box, and a merge at the end.
Codex workers are capable but drift. They build test harnesses, audit ledgers and "verification
frameworks" instead of the thing, stall waiting for permission, and leave half-merged branches
everywhere. mr-manager is opinionated about all of that, so you don't have to be.

```
mrm assign fix-neg "calc eval \"-(2+3)*2\" prints -10" --check "pytest -q" --auto
mrm status
```

## What it decides for you

- **Every assignment has a check.** `assign` refuses without a command that must pass (or an
  explicit `--no-check "why"`). The check is the contract, and it runs on what will actually land.
- **Every worker gets its own lane:** a git worktree on `worker/NAME`, its own block of ports,
  its own environment, and shared folders linked in. Parallel workers never collide.
- **Every lane ends in `accept` or `scrap`.** `accept` merges the base into the lane, runs the
  check on that combined state, merges, removes the worktree and branch, kills leftover lane
  processes and archives the thread. New work is refused while leftovers exist.
- **Workers are briefed to deliver, not perform.** A standing brief tells them to build the
  outcome, take the smallest path, not stall, stay in their lane, and end with a four-line report.
- **A watchdog enforces it while nobody's looking.** At the time box it orders a wrap-up, and at
  1.5× the box it orders a stop. It nudges when harness/ledger/audit-style files appear, and it
  records how long each finished worker has waited on you.
- **Auto-accept when you trust the check.** With `--auto`, a clean DONE (in budget, no ceremony,
  check passes) merges itself. A failing check goes back to the worker with the output, twice, before
  it escalates to you. `mrm status` lists what merged while you were away.
- **It survives restarts.** Lanes cut off mid-turn are told to pick up where they left off.
- **Merges are queued per repo,** so parallel accepts can't race the index or skip each other's
  changes.
- **Multiple accounts.** Add more Codex accounts. `assign` picks the one with the most headroom.

## Setup

Needs Python 3.11+, git and the Codex CLI (`npm i -g @openai/codex`), signed in.

```
git clone https://github.com/adamlevineagent/mr-manager
python mr-manager/mrm.py setup
```

`setup` checks prerequisites, signs in accounts and proves each can complete a real API turn before
installing anything. It then puts `mrm` on your PATH, installs the Claude Code
skill, runs the daemon as a background service (Task Scheduler, launchd or systemd), and proves it
end to end: a real worker does a tiny task in a scratch repo and gets checked and merged.

## Status board

Run `mrm board` to open a live train-station split-flap board in a small app window
(Edge or Chrome on Windows, Chrome on macOS, your browser on Linux). It starts the
daemon if needed. `mrm board --url` prints its local URL without opening a window.
The self-contained page polls every three seconds, shows lanes needing attention
first, highlights time boxes, and keeps recent arrivals below the departures.
Sound is off by default; the page respects reduced-motion preferences.

To also display an older local foreman crew, add a `watch` list to your mrm config
(`mrm config watch` shows it; it defaults to an empty list):

```json
"watch": [
  {"label": "foreman", "registry": "~/.codex-foreman/workers.json", "daemon": "http://127.0.0.1:8777"}
]
```

These lanes appear in a separate section with source and repo tags. Their registry
is only read. The board calls only that daemon's `status` method, caches replies
for about five seconds, and shows `?` when it cannot answer within one second.
Watched lanes are display only; they never receive management or auto-merge actions.

## The loop

```
mrm assign NAME "outcome" --repo PATH --check "CMD"   # brief a worker in its own lane
mrm wait                                            # until a worker needs you
mrm status                                          # NEEDS YOU / WORKING / MERGED WHILE YOU WERE AWAY
mrm review NAME                                     # report, check, diff, flags
mrm accept NAME                                     # integrate -> check -> merge -> clean up
mrm reject NAME "what's wrong"                      # send it back; its time box restarts
mrm nudge NAME "correction"                         # steer mid-turn (lands at its next step)
mrm scrap NAME                                      # throw the lane away
```

Also: `mrm probe [ACCOUNT]` (a tiny real turn for one or all accounts, without the daemon; shows the CLI,
version and actual error on failure), `park NAME [why]` (keep a finished lane on purpose, off NEEDS YOU; `--undo`), `check NAME [CMD]`, `tail NAME -f`, `read NAME`, `diff NAME`, `history [NAME]`, `usage`,
`gc [--clean]`, `attach NAME --repo PATH`, `account add|list|remove`, `config`, `slot NAME -- CMD`.

## Shared build and database slots

Use `mrm slot build -- cargo test -j 16` to serialize native builds across repos,
or `mrm slot db -- python integration_check.py` to limit concurrent database work.
The command inherits your directory, environment and terminal; its exit code is returned.
No daemon is needed. Commands start in FIFO ticket order per resource, up to its capacity.

The `slots` object in `~/.mr-manager/config.json` defaults to `{"build": 1, "db": 8}`.
Change it with `mrm config slots '{"build":1,"db":8}'` (or edit the JSON file).
Capacities must be positive integers; unlisted names have capacity 1. All repos and
lanes must use the same mrm home (`MRM_HOME` overrides it) to share the machine's queue.
Tickets live in `slots.sqlite3` there; dead waiters and holders are pruned by PID on
admission and status. If a wrapper dies while its command is still running, that
command keeps its seat until it exits. `mrm status` and the `slots` field in `/board.json`
show live holders and queue lengths.
Capacity changes apply to new invocations; let existing invocations finish before
changing a resource's budget. Wrap the full resource-consuming command, including
any waits, and do not launch detached work from it.

A lane can work in another repo too: create a worktree there on `worker/NAME` or
`worker/NAME-<suffix>`, then run `mrm attach NAME --repo PATH` (the main repo or its
worktree path). This registers the repo; matching secondary worktrees in repos already
known to mr-manager are discovered automatically. Exact lane names take priority over
suffix matches. `gc` treats these as active, `accept` merges them into that repo's
checked-out branch and removes them, and `scrap` removes them with the parent lane.
Commit work in every repo first, or use `accept --commit`; `scrap --force` is required
to throw away unmerged or uncommitted secondary work. Check out the intended destination
branch in each secondary repo before accepting. Merges across repos are sequential;
if one fails, resolve the reported problem and accept again (earlier merges stay landed).

Assignment options: `--not-doing` (scope), `--read FILE...` (read first), `--detail` (more brief),
`--size small|medium|large`, `--timebox MIN`, `--readonly` (research/review, no lane),
`--account`, `--model`, `--effort`.

The outcome and `--detail` accept inline text or an existing UTF-8 file. Before creating a lane
or contacting an account, `assign` refuses unexpanded shell variables (`$n`, `${lane}`, `%LANE%`)
and missing file paths. Paths include absolute paths, `./` or `../` paths, backslash paths, and
single words containing `/` or ending in `.md`, `.markdown`, `.txt`, or `.rst`. For paths with
spaces, use an absolute path or a `./` prefix. Expand variables in your shell or pass a literal
path to an existing file.

## Per-repo profile: `.mrm.toml`

```toml
[mrm]
check = "pytest -q"            # default check for this repo
ports = 10                     # ports per lane ($MRM_PORT_BASE, $PORT)
share = ["node_modules"]       # folders linked into every lane instead of rebuilt
notes = "Use pnpm. Never touch migrations/."
timebox = 60                   # minutes; default for lanes in this repo (e.g. slow native builds)
size = "large"                 # small | medium | large
check_timeout = 7200           # seconds the check may take (default 3600), e.g. when builds queue on a lock
max_lanes = 4                  # at most this many lanes at once in this repo (laptops, shared build locks)
exclude = ["audits/*observation*/"]   # left out of each lane's checkout (big recorded artifacts); merges unaffected
on_close = ["docker compose -p {lane} down"]   # optional cleanup commands on accept or scrap

[mrm.env]                      # extra environment for workers; {repo} = the main checkout
APP_ENV = "test"
```

Every lane gets `MRM_SCRATCH`, a private folder at `<MRM_HOME>/scratch/<lane>` outside
its worktree (default home: `~/.mr-manager`). Put disposable builds, caches and temp files
there; `accept` and `scrap` delete it. Workers receive this path in their environment and brief.
Profile `on_close` commands run in the main repo with the lane environment, substituting
`{lane}` with its name and `{repo}` with the main checkout path. Quote placeholders when
used as paths. Each command has a 60-second timeout; failures are reported, remaining commands
and scratch cleanup still run. A locked scratch folder is queued for the daemon or `mrm gc`
to retry. A refused accept or scrap keeps the lane's scratch and does not run its hooks.

## On macOS

`setup` installs a launchd agent (`~/Library/LaunchAgents/dev.mr-manager.daemon.plist`) that starts at
sign-in, restarts on exit and logs to `~/.mr-manager/daemon.log`. Services start with a bare PATH, so the
agent carries one with Homebrew and `~/.local/bin`, and the interpreter path is Homebrew's stable symlink
rather than a versioned Cellar path a `brew upgrade` would delete. As on Windows, mrm prefers the Codex
CLI bundled with the ChatGPT desktop app (kept current by the app); set `MRM_CODEX` to pick another.
`mrm` lands in `~/.local/bin`; if that isn't on your PATH, add `export PATH="$HOME/.local/bin:$PATH"` to
`~/.zshrc`.

Pause new work while something is wrong (a disk-space guard, say): `mrm config stop_file /path/to/flag`.
While that file exists, `assign` refuses and shows the file's text as the reason.

Keep new lanes in one folder with `mrm config lanes_dir /path/to/lanes`. They go into
`/path/to/lanes/<repo folder name>/<lane name>`; the default `""` keeps today's `<repo>-wt/<lane>`
layout. Existing lanes stay where they are, and `gc` finds both layouts through Git's worktree list.

When there are lanes, `mrm status` ends with their total disk use and the biggest lane. It says
`lanes on disk: measuring…` until the first measurement. The daemon caches file sizes every
three minutes on one background thread, including idle and parked lanes; shared symlinks and
junctions are excluded. Set `mrm config max_lanes_gb 100` to refuse new assignments while the
cached total exceeds 100 GB (1 GB = 1,000,000,000 bytes), with the biggest lanes listed so you can accept or
scrap them. The default `0` disables the limit. This blocks new work; running lanes can still grow
between scans.

## With Claude Code

`setup` installs a skill so Claude Code knows how to run workers: assign with a real check, wait in
the background, review the diff itself (a passing check isn't a full review), and accept or reject.
You talk to Claude; Claude manages the crew.

## How it works

A daemon runs one `codex app-server` per account in an isolated `CODEX_HOME` under
`~/.mr-manager/homes/`, and the CLI talks to it over localhost. The worker home shares the account's
`auth.json`, synced both ways at startup, around turns and every watchdog tick (20 seconds), so
desktop and worker token refreshes stay shared. Workers do not inherit the account's `AGENTS.md`,
memories, skills, prompts or config defaults. Threads started before isolation keep using their
original account home so they remain readable, steerable and archivable.

To opt in to specific account files or folders, set `worker_inherit` in
`~/.mr-manager/config.json` (default `[]`), for example
`"worker_inherit": ["AGENTS.md", "skills", "config.toml"]`. Only top-level names are accepted;
copies are refreshed when app-server starts, and removing a name removes its inherited copy.
Restart the daemon after changing this setting. Notify hooks are off; inherited MCP servers
are off unless listed in `worker_mcp_allow`.

Steering uses `turn/steer`, so a correction lands in the running turn instead of a queue.
State lives in `~/.mr-manager/`.

Workers run with approvals off and full access inside their lanes. Brief accordingly, and don't
point them at live services or credentials.

MIT licensed.
