# Build Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Heavy commands of the agents of every review on a machine — builds, tests, dependency installs, servers — run one at a time through a new `herdr-review exclusive` wrapper, while each reviewer still decides what to build.

**Architecture:** A new module `herdr_review/exclusive.py` owns the machine's build queue: an exclusive `flock` on `<runs_dir>/exclusive.lock` held by the wrapper process for exactly as long as it lives, and `<runs_dir>/exclusive.json` naming the holder. The CLI gains a top-level `exclusive` command; the runner gives every agent `HERDR_REVIEW_AGENT` and `GIT_OPTIONAL_LOCKS=0`, and stops its own run's holder on `run fail`, `run finish` and `close`; `status` reports the queue; a shared prompt template `prompts/exclusive.md` tells the reviewer and the fixer to run heavy commands through the wrapper, and the orchestrator how to judge them.

**Tech Stack:** Python ≥ 3.11 standard library (`fcntl`, `signal`, `subprocess`, `ctypes`), PyYAML (already a dependency), `unittest`, bats.

**Spec:** `docs/superpowers/specs/2026-09-25-build-queue-design.md` — read it first; it is authoritative on behaviour, this plan on the code.

## Global Constraints

- Python ≥ 3.11 syntax (no nested f-strings reusing the outer quote), standard library plus PyYAML only; POSIX (Linux, macOS). Linux-only calls (`PR_SET_PDEATHSIG`, `/proc`) are guarded and have a macOS fallback (`ps`).
- Queue files: `<runs_dir>/exclusive.lock` (mode 600, created on first use with `runs_dir` mode 700, never deleted) and `<runs_dir>/exclusive.json` (the holder: `pid`, `agent`, `run_id`, `run_dir`, `command`, `cwd`, `started_at`).
- Environment names: `HERDR_REVIEW_RUN` (existing), `HERDR_REVIEW_AGENT`, `HERDR_REVIEW_EXCLUSIVE=1` (set for a command inside a turn), `GIT_OPTIONAL_LOCKS=0`; `HERDR_REVIEW_POLL_SEC` (existing test knob) also sets the wrapper's lock polling.
- Defaults: `--wait` 60 s (`0` tries once), `--timeout` 1800 s, waiting notice at once and then every 15 s, lock polling 1 s, SIGKILL 10 s after the stop signal, the runner's stop waits 15 s.
- Exit codes of `exclusive`: the command's own; 128+N when signal N killed it or stopped the wrapper; 75 busy; 124 timed out; 126 not executable; 127 not found; 2 usage error; 1 the wrapper could not work.
- The wrapper's own messages are English and start with `herdr-review exclusive:`. `status` and `close` text stays Russian, like the rest of their output.
- Prompts to agents, code, comments, docs and commit messages are in English; talk to the owner in Russian.
- Commits: the repository's subject style (`feat: …`, `docs: …`), no trailers or attribution lines; stage explicit paths only, never `git add -A` or `git add .`. The owner's untracked files (`docs/2026-09-23-…`, `docs/2026-09-24-…`, and the older files in `docs/superpowers/plans/`) stay untracked.
- Tests: `tests/run.sh` (unit + bats), `tests/run.sh unit`, one module with `python3 -m unittest tests.unit.<module> -v`. In the main session build and test commands go through the `claude-forge:build-runner` agent (the owner's plugin rule); a subagent runs them directly.
- Tests never touch the machine's real queue: every test works in its own temporary `runs_dir`, and `clean_env()` strips every `HERDR_REVIEW_*` variable from the environment the tests inherit.

## Review Focus

1. **The agent cancels a running command with Ctrl-C to the whole process group** — the wrapper and its command both get SIGINT: the command stops, the wrapper exits 130, the queue is free. Pinned in Task 4 (`test_ctrl_c_to_the_whole_group_stops_the_command_and_frees_the_queue`).
2. **A command that leaves a background process behind and exits** (`sh -c 'sleep 30 & exit 0'`) — the wrapper returns at once with the command's code and the queue is free; the background process is not the wrapper's to kill. Pinned in Task 3 (`test_a_command_that_leaves_a_background_process_returns_at_once_and_frees_the_queue`).
3. **The lock is held but no holder file exists** (a holder killed with SIGKILL before it wrote the file, or between removing it and releasing the lock) — waiters say "another process holds the queue", `status` says «занята — процессом, который себя не назвал», nothing crashes. Pinned in Task 1 (`test_a_held_queue_without_a_holder_file_has_an_unnamed_holder`) and Task 5 (`test_a_held_queue_without_a_holder_file`).
4. **First use on a machine without `runs_dir`** (the owner's own call before any review) — the wrapper creates `runs_dir` (700) and `exclusive.lock` (600) and runs the command. Pinned in Task 3 (`test_outside_a_review_the_config_names_the_queue_and_creates_it`).
5. **A command line with spaces, quotes and non-ASCII text** — every argument reaches the command verbatim, and the holder's `command` is its `shlex.join`, readable in `exclusive.json` and `runner.log`. Pinned in Task 3 (`test_spaces_quotes_and_non_ascii_reach_the_command_and_the_holder_file`).

---

### Task 1: The queue's files and state

✅ Done — see commit(s): `9a254f2`

---

### Task 2: Every agent names itself and leaves the index lock alone; run.json records runs_dir

✅ Done — see commit(s): `660e5c5`, `28183b1`

---

### Task 3: `herdr-review exclusive` takes turns

✅ Done — see commit(s): `cb7e2e0`, `04b5c32`

---

### Task 4: `exclusive` stops a command past its timeout or on a signal, with everything it started

✅ Done — see commit(s): `f5b60f0`, `0d3e1f8`, `225ad66`

---

### Task 5: `status` names who holds the build queue

✅ Done — see commit(s): `a66d1b8`

---

### Task 6: `run fail`, `run finish` and `close` stop their run's command in the build queue

✅ Done — see commit(s): `ddb0aba`

---

### Task 7: The reviewer and the fixer run heavy commands through `exclusive`; the orchestrator judges them

✅ Done — see commit(s): `e0a24f5`

---

### Task 8: Documentation

✅ Done — see commit(s): `bc0f465`

---

## Finishing the branch

Per the owner's rule: before the pull request, `git rm` the spec and this plan (`docs/superpowers/specs/2026-09-25-build-queue-design.md`, `docs/superpowers/plans/2026-09-25-build-queue.md`) and commit, so that neither appears in the PR diff; they stay in the branch history. Leave the owner's other untracked files under `docs/` alone.
