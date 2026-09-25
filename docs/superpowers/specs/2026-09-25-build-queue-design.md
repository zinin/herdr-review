# Build queue: heavy commands take turns — design

Date: 2026-09-25. Branch: `feat/build-queue`. The owner approved each section of this design in brainstorming;
this document records it for the implementation plan.

## Summary

Every reviewer of a run works in the owner's working tree, and nothing stops two of them from building or testing
at the same time. A new command, `herdr-review exclusive -- <command>`, runs a heavy command — a build, tests, a
dependency install, a server — only while it holds a lock shared by every review on the machine, and the reviewer
and fixer prompts require it for such commands. Each reviewer still decides what to build; builds only take turns.

## Problem

- All reviewers start in the repository, in one working tree: `Runner._place_tabs` and `Runner._place_grid` create
  their tabs and panes with `cwd=self.repo`.
- `prompts/reviewer.md` allows the project's tests in that tree "as long as they write only into gitignored paths";
  the clone under `scratch/<profile>/` is meant for experiments.
- The orchestrator confirms "the project's own tests or build" for any reviewer, whatever the others are doing, and
  it sees a command only when the agent's CLI raises a dialog about it — in yolo mode, never.
- Reviewers do build. In run `vpn-director/20260924-163112-hrrn0u` three reviewers each ran the full suite:
  `deepseek-v4-pro` in the working tree (`go test ./...`, 648 bats tests, `npm run build`), `glm-5-3-flash`
  (`go test`, bats) and `claude-opus` in its clone.

What goes wrong when two agents build at once:

- **One working tree.** Builds overwrite each other's output (`target/`, `build/`, `dist/`) mid-test, and the
  phantom failures become false findings. `mvn clean`, `gradle clean`, `npm ci` (which deletes `node_modules`) and
  `uv sync` break a neighbour's run. Gradle and cargo wait on each other's locks until the agent's command times out.
- **One machine**, even in separate clones. Fixed ports fail with `Address already in use`. Docker Compose names a
  project after its directory, and every clone lives in `scratch/<profile>/repo`, so every clone is project `repo`.
  Tests truncate a shared local database. `mvn install` puts the branch's SNAPSHOT into `~/.m2`. Several JVM builds
  exhaust memory and CPU.
- **Overlap in time.** `run fail` leaves the agent running, so a failed reviewer's build can overlap the fixer's
  tests. A second run of the same repository only draws a warning from `launch`.
- **git.** The agents' `git status` and `git diff` take `.git/index.lock` opportunistically, so the owner's
  `git commit` can fail with `index.lock exists`. The runner avoids this with `GIT_OPTIONAL_LOCKS=0`
  (`gitutil._git_env`); the agents do not.

## Goals

1. At most one heavy command runs at a time among all agents of all reviews on the machine.
2. Each reviewer still decides whether and what to build. A busy queue delays a build; it never forbids one.
3. One mechanism serves every agent kind (claude, codex, gemini, grok, alt-provider claude) in auto and in yolo mode.
4. A dead or stuck holder cannot block the queue for long.
5. The owner sees who holds the queue and can queue their own builds.

## Design

### 1. `herdr-review exclusive`

```
herdr-review exclusive [--wait SEC] [--timeout SEC] -- COMMAND [ARG...]
```

A top-level command, not a `run` subcommand: every `run` command prints one JSON object (the orchestrator's ground
rule 4), while `exclusive` passes its command's output through; and it needs no run, so the owner can queue their
own build with it.

The wrapper's options come first. The command starts after `--`, or at the first argument that is not an option of
the wrapper, and every argument from there on belongs to it, a later `--` included. The wrapper executes the command
directly, without a shell (`sh -c '…'` gives one).

Behaviour:

1. Take an exclusive `flock` on the queue file (section 2), polling with `LOCK_NB` about once a second.
2. While the lock is busy, print to stderr at once and then every 15 s:
   `herdr-review exclusive: waiting — <holder> has run "<command>" for <duration>`. `<holder>` reads
   `<agent> (run <run_id>)`, or `pid <pid> outside a review`.
3. With no turn after `--wait` seconds (default 60; `0` tries once), print
   `herdr-review exclusive: busy — <holder> has run "<command>" for <duration>; nothing was run. Do other work and run the same command again later.`
   and exit 75.
4. With the lock: write the holder file (section 2), start the command, wait for it, remove the holder file, release
   the lock and exit with the command's code (128+N when signal N killed it). A wrapper that had to wait prints
   `herdr-review exclusive: your turn after <duration>` before it starts the command.
5. After `--timeout` seconds of running (default 1800, counted from the start of the command, not of the wait), stop
   the command (below), print `herdr-review exclusive: timed out after <duration>; stopped "<command>"` and exit 124.
6. A wrapper whose agent the run took off (`run fail` leaves it `failed` in the run's `status.json`) runs nothing: it
   prints `herdr-review exclusive: <agent> was taken off this review (run fail); nothing was run` and exits 1. It
   checks before it waits and again when its turn comes; a `status.json` it cannot read takes nobody off.

The command:

- inherits the wrapper's working directory, environment, stdout and stderr; its stdin is `/dev/null`, so a prompt
  for input fails at once instead of hanging while the lock is held;
- stays in the wrapper's process group, so whatever the agent's CLI signals to cancel a command reaches both;
- gets `HERDR_REVIEW_EXCLUSIVE=1` in its environment. A wrapper that finds this variable set runs inside another
  wrapper's turn: it runs its command at once, under the outer lock and the outer timeout;
- on Linux, gets `PR_SET_PDEATHSIG=SIGKILL`, so it dies even when the wrapper is killed with SIGKILL.

Stopping the command, on `--timeout` or on SIGTERM, SIGINT or SIGHUP to the wrapper: the wrapper sends the signal
(SIGTERM for a timeout), and SIGCONT after it so that a stopped process handles it, to the command and to every
process under it, found through the process table (`/proc` on Linux, `ps -A -o pid=,ppid=` on macOS). A SIGINT or
SIGHUP goes only to the processes that left the wrapper's process group: a terminal sends it to its whole foreground
group, so the others have it already, and a second Ctrl-C makes a program such as `docker compose up` skip its
graceful stop. After 10 s it sends SIGKILL to whatever is left (on Linux, to what is under the wrapper then), then
releases the lock and exits: with 124 after a timeout, with 128+N after signal N, whatever the command's own code. On
Linux the wrapper is the subreaper of the command's processes (`PR_SET_CHILD_SUBREAPER`): a process whose parent died
is re-parented to the wrapper and still counts as under it, so the stop is the same when the signal reached the whole
process group (Ctrl-C) and the command's first process already died of it. A signal that arrives while the wrapper
still waits for its turn ends the wrapper at once with 128+N; nothing runs.

The lock lives exactly as long as the wrapper. The command does not inherit the queue file's descriptor
(`O_CLOEXEC`), so a background process the command leaves behind holds no lock, and the kernel releases the lock of
a wrapper that died. The queue file is never deleted: `flock` excludes only the processes that lock the same inode.

Exit codes:

| Code | Meaning |
|---|---|
| the command's | The command ran; 128+N when signal N killed it. |
| 75 | Busy: no turn within `--wait`; the command did not run. |
| 124 | The command ran past `--timeout` and was stopped. |
| 126, 127 | The command is not executable, or not found. |
| 2 | Usage error: no command, a bad option. |
| 1 | The wrapper could not work: the queue file, the config or the run directory is unusable. |

A command may itself exit 75, 124 or 1; the wrapper's own outcomes always come with a `herdr-review exclusive:`
line on stderr.

When `HERDR_REVIEW_RUN` names a run, the wrapper appends to that run's `runner.log`, in the runner's line format:
`exclusive: <agent> running "<command>" after <duration> of waiting`, `exclusive: <agent> done after <duration>,
exit <code>`, `exclusive: <agent> busy after <duration>: <holder>`, `exclusive: <agent> timed out after <duration>`.

### 2. The queue: one per machine

Two files in the root of `runs_dir`:

- `exclusive.lock` — the `flock` target, mode 600. The first wrapper creates it, with `runs_dir` itself (mode 700)
  when that is missing. Nothing deletes it.
- `exclusive.json` — the holder: `{"pid", "agent", "run_id", "run_dir", "command", "cwd", "started_at"}`. The wrapper
  writes it atomically (a temporary file and `os.replace`) after it takes the lock and removes it before it releases
  the lock. `command` is `shlex.join` of the argv, cut to 200 characters. `agent` comes from `HERDR_REVIEW_AGENT`,
  `run_id` and `run_dir` from `HERDR_REVIEW_RUN`; each is `null` outside a review.

One queue serves every review on the machine that uses the same `runs_dir`, and every call the owner makes. Ports,
Docker, local databases, `~/.m2` and memory belong to the machine, so a lock per run or per repository would leave
most collisions in place. The price: two runs in different repositories wait for each other's builds.

The files live in `runs_dir` because the agents can write there in auto mode: the codex profiles get the directory
through `--add-dir`, and the codex sandbox would refuse a queue file in `/tmp` or `~/.cache`.

The wrapper finds `runs_dir`:

- with `HERDR_REVIEW_RUN` set, as in every agent of a run: in the run's `run.json`, where `launch` now records the
  resolved absolute `runs_dir`; for a run launched before this change, as the run directory's grandparent
  (`<runs_dir>/<project>/<run>`);
- without it, in the owner's shell: `settings.runs_dir` from the config, resolved against the current directory as
  `launch` resolves it.

The holder file is a hint, never proof: a wrapper killed with SIGKILL leaves it behind. Whoever reports the holder
first learns whether the lock is held. A waiting wrapper knows from its failed attempt; `status` and the stop of
section 6 try `LOCK_NB` themselves and release the lock at once when they get it.

### 3. Heavy commands and the prompts

A new template, `prompts/exclusive.md`, rendered with `RUNNER`, holds the rules shared by the reviewer and the
fixer. `reviewer.md` embeds it as `{EXCLUSIVE_RULES}` in a new section `## Heavy Commands`; `fixer-auto.md` and
`fixer-decision.md` embed it the same way, following the pattern of `{SCOPE_STEPS}` and `{COMMIT_RULES}`. The text,
whose wording the plan may polish but whose meaning it keeps:

> Heavy commands share this machine with every agent of every review running on it. Run each one through
> `"{RUNNER}" exclusive -- <command> [args…]`, which runs one heavy command at a time. A command is heavy when it
> runs the project's build tool, test runner or package manager (even for one test), installs or updates
> dependencies, starts a server, a container, a database or anything else that listens on a port, or does any of
> this indirectly: `make`, a project script, a commit whose hooks build or test. When unsure, treat it as
> heavy. Reading files, searching, `git diff`, `git log`, `git show`, `git status` and `git clone` are not heavy.
>
> - Put steps that must run back to back into one call: `"{RUNNER}" exclusive -- sh -c 'npm ci && npm test'`. The
>   command gets no input: its stdin is empty.
> - The wrapper waits up to 60 s for its turn. Exit code 75 with a `herdr-review exclusive: busy` line means the
>   command did not run. Run the same call again later yourself, never in a shell loop (`until …; do sleep …;
>   done`): such a loop holds one tool call for as long as the queue stays busy, and it never ends when the command
>   itself fails.
> - A command still running after 30 minutes is stopped: exit code 124 with a `herdr-review exclusive: timed out`
>   line. For a build you know takes longer, pass `--timeout <seconds>` before the `--`.
> - Stop everything you start inside the same call: `sh -c 'docker compose up -d && …; docker compose down'`. No
>   server, container or watcher may outlive the call.
> - Exit code 1 with a `herdr-review exclusive:` error line means the wrapper itself failed. Never run the command
>   without it.

After the embedded rules, each role adds its own lines.

- **Reviewer:** "While you wait for your turn, do other review work: read code, draft findings. Do not give up a
  check you need because the queue is busy. If the wrapper failed, or you still could not run a check when the rest
  of the review is done, say so in the review." The first Hard Rule keeps its words about gitignored paths and adds
  that the project's tests run through the wrapper (see Heavy Commands). Where to build, in the working tree or in
  the clone, stays the reviewer's choice.
- **Fixer:** rule 2 becomes "If the project has tests relevant to the changed code, run them as the section Heavy
  commands below says. Fix a failure only if your change caused it." Its own line: "While you wait for your turn,
  apply your next fix, or wait and run the call again. Never skip the tests because the queue is busy; if the
  wrapper failed, report the tests as not run, with the reason." The commit rules (`fixer-commit-auto.md`,
  `fixer-commit-decision.md`, which gain `RUNNER`) commit through the wrapper too,
  `"{RUNNER}" exclusive -- git commit --only …`, since the repository's commit hooks may build or test: exit 75 means
  nothing was committed, and a failed wrapper leaves the fix `applied, not committed: the build queue failed: <line>`.

The orchestrator (`prompts/orchestrator.md`) learns three things:

- Phase 2, permission dialogs: a command run through `"{RUNNER}" exclusive -- <command>` is judged by `<command>`;
  the wrapper itself writes only its queue files in the runs directory and the run's log. A heavy command that a
  reviewer or the fixer runs without the wrapper → refuse with the dialog's own "no" option, then
  `herdr agent prompt <name> "Run builds, tests, dependency installs, servers and containers through \"{RUNNER}\" exclusive -- <command>, as your prompt says."`
  The reviewer's rule to confirm "the project's own tests or build" now applies to them run through the wrapper.
- Phase 2, a stuck agent: a wrapper that waits for its turn or runs its command is a long tool call — keep waiting;
  `"{RUNNER}" status --run "{RUN_DIR}"` names the holder.
- Red flags: "Confirming a heavy command run without the wrapper | Refuse; point the agent at `exclusive`."

Placeholder sets: `reviewer.md`, `fixer-auto.md` and `fixer-decision.md` gain `EXCLUSIVE_RULES`; `exclusive.md`
has `RUNNER`. `launch` renders the reviewer prompts with it; `scope.fixer_skeleton` takes the runner path and
renders the fixer skeletons with it.

### 4. Agent environment

- `Runner._base_env()` adds `GIT_OPTIONAL_LOCKS=0` for every reviewer and the fixer; `launch` adds it to the
  orchestrator's environment. Their `git status` stops taking `.git/index.lock` (`git diff` still rewrites the
  index after a stat-only change, as git 2.53 does); the locks that `git add` and `git commit` need stay as they are.
- Every agent gets `HERDR_REVIEW_AGENT=<its agent name>`: the reviewers in `_place_tabs` and `_place_grid`, the
  fixer in `start_fixer`, the orchestrator in `launch`.
- Order of the environment: the base variables, then the profile's `env` (a profile may override
  `GIT_OPTIONAL_LOCKS`), then `HERDR_REVIEW_AGENT`.

### 5. Status

`herdr-review status` reports the queue for whatever run it shows, since the queue belongs to the machine:

- text, after the drift line: `очередь сборок: занята — <agent> (прогон <run_id>): <command>, <N> мин`
  (`<N> с` under a minute; `pid <pid> вне ревью` for a holder outside a review;
  `очередь сборок: занята — процессом, который себя не назвал` when the lock is held but no holder file names
  anyone), `очередь сборок: свободна`, or `очередь сборок: не удалось проверить (<reason>)`;
- `--json`: `"exclusive": {"held": true, "agent": …, "run_id": …, "pid": …, "command": …, "since_sec": …}`,
  `{"held": false}`, or `{"held": null, "error": "<reason>"}`.

`run wait` does not report the queue: the orchestrator already treats a long tool call as work, and `status` names
the holder when it needs to know.

### 6. Stopping a run's holder

A run's leftover — a failed reviewer's build, a background command that a CLI kept alive — could hold the queue for
up to `--timeout`. The runner stops the holder that belongs to its own run:

- `run fail <name>`: when `<name>` holds the queue; afterwards the wrapper runs nothing more for `<name>` (section 1),
  since its CLI may still run and call it again;
- `run finish`: when any agent of the run holds it, before `scratch/` is removed, since the command may run there;
- `close`, with or without `--force`, once its session and phase checks pass: when any agent of the run holds it,
  before the tabs close, since a CLI's background command can outlive its tab.

The stop takes `LOCK_NB` first; a free queue means nothing to stop. Otherwise it reads the holder file and acts only
when the holder's `run_id` is this run's (and its `agent` is `<name>` for `run fail`) and its `pid` still runs
`herdr-review exclusive` (`/proc/<pid>/cmdline` on Linux, `ps -p <pid> -o command=` on macOS). It sends that pid
SIGTERM; the wrapper stops its command as section 1 says and releases the lock. The stop waits up to 15 s for the
lock to come free. When it does not, the stop logs that and moves on: it never sends the wrapper SIGKILL, which
would orphan the command without the lock, and `--timeout` still bounds the command. The JSON of `run fail`,
`run finish` and `close` gains `"exclusive_stopped": "<agent>: <command>"` when a stop happened, and `runner.log`
records it.

### 7. Documentation

- README: in "During the run", a **Builds and tests** bullet: heavy commands take turns through
  `herdr-review exclusive`, one at a time on the machine, and a reviewer reads code while it waits. In "The CLI",
  `herdr-review exclusive -- ./gradlew build` for the owner's own builds, with `--wait` and `--timeout`. In
  Troubleshooting, a reviewer that waits long for the queue (`status` names the holder), and the codes 75 and 124.
- CHANGELOG: an `[Unreleased]` entry.
- `config.example.yaml`: the `runs_dir` comment says that the directory also holds the machine's build queue.
- `tests/SMOKE.md`: a new item, described under Testing.

## Testing

A new `tests/unit/test_exclusive.py` runs real processes with short timeouts and polls with deadlines instead of
fixed sleeps:

- no contention: the command runs; its exit code passes through (0 and 3); the holder file exists during the run
  and is gone after it;
- contention: while one wrapper runs `sleep`, a second with `--wait 1` exits 75, its command never runs (a marker
  file stays absent), and its message names the holder; with `--wait 10` the second runs once the first finishes;
- `--timeout 1` on `sh -c 'sleep 30 & sleep 30'`: exit 124 within seconds, both sleeps gone, the lock free;
- SIGTERM to a running wrapper: the command receives it, the lock comes free, exit 143; SIGTERM to a waiting
  wrapper: exit 143, nothing run;
- nesting: `exclusive -- <runner> exclusive -- true` finishes without waiting;
- argv: `exclusive -- printf '%s\n' -- a` prints `--` and `a`;
- stdin: `exclusive -- cat` returns at once with empty output;
- errors: no command → 2; a missing program → 127; an unwritable `runs_dir` → 1;
- `runs_dir` from `HERDR_REVIEW_RUN` and `run.json`, from the grandparent for an older `run.json`, and from the config
  without a run; the `runner.log` lines when `HERDR_REVIEW_RUN` is set.

Existing test modules gain:

- runner: `GIT_OPTIONAL_LOCKS=0` and `HERDR_REVIEW_AGENT` in the environment of reviewer tabs, grid panes and the
  fixer (the fake herdr records it), with the profile's `env` still applied;
- launch: `runs_dir` in `run.json`; both variables in the orchestrator's environment;
- status: the `exclusive` field and the text line for a held queue, a free one, a stale holder file and an
  unreadable queue file;
- `run fail`, `run finish`, `close`: each stops this run's holder, leaves another run's holder and a free queue
  alone, and reports `exclusive_stopped`;
- prompts: the new placeholder sets; the key sentences of `exclusive.md`, the role lines, the orchestrator's
  permission rule and its red flag.

bats: two `bin/herdr-review exclusive` calls started together record intervals that do not overlap.

SMOKE (real herdr): a preset with two reviewers on a repository with a test suite. Both reviewers run the tests;
`runner.log` shows their `exclusive:` lines without overlap, and `status` names the holder while a suite runs.

## Limitations

- Discipline: an agent that ignores the rule collides as it would today. Only the orchestrator's dialog check
  enforces the rule, in auto mode, when the CLI asks.
- A wrapper killed with SIGKILL releases the lock at once. On Linux the command's first process dies with it, but
  the processes it started can live on without the lock; on macOS the whole command can.
- On macOS a stop signal sent to the whole process group can leave behind a process whose parent died of it, such
  as a background job of `sh -c`, which ignores SIGINT: the wrapper no longer finds it and releases the lock at once.
- A SIGINT or SIGHUP sent to the wrapper alone, not to its process group, reaches the command only as the SIGKILL
  10 s later: the wrapper takes these signals for a terminal's, which the group already has.
- On Linux a stop ends the daemons that the command started, such as a Gradle daemon, so the next build starts a new
  one; a command that ends by itself leaves them running.
- No FIFO order: when the lock comes free, the waiter that polls first takes it. With a handful of agents this
  costs little.
- A background process that a command leaves behind, against the rule, holds no lock.
- One queue per `runs_dir`: two configs with different `runs_dir` make two queues, and a relative `runs_dir` makes
  the owner's own calls queue per current directory.
- Throughput: all builds on the machine run one at a time, so the last of several reviewers may wait for several
  builds.

## Out of scope

- Builds only in clones: cold builds and missing gitignored artifacts (`web/dist`, `.env`) cost more than they save
  once builds take turns.
- Sequential reviewers: N times the time even when nobody builds; a setting for it can come later.
- Wrappers for `mvn`, `npm` or `go` in `PATH`: they miss `./gradlew`, `./mvnw`, `make` and scripts, and would queue
  harmless calls such as `go doc`.
- A setting for the queue's scope: run, repository or machine.
- In scope `commits`, tests in the working tree run with the owner's uncommitted edits; and a build may write tracked
  files, which the reviewer's first Hard Rule already forbids and drift detection catches.
- The owner's IDE builds; rate limits of shared accounts and package registries.
