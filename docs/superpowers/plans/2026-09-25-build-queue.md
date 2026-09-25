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

**Files:**
- Create: `herdr_review/exclusive.py`
- Test: `tests/unit/test_exclusive.py`

**Interfaces:**
- Consumes: `herdr_review.config.load_config(path=None, environ=os.environ) -> Config` and `ConfigError`; `herdr_review.status.now_iso() -> str`.
- Produces (module `herdr_review.exclusive`):
  - constants `LOCK_NAME = "exclusive.lock"`, `HOLDER_NAME = "exclusive.json"`, `RUN_ENV`, `AGENT_ENV = "HERDR_REVIEW_AGENT"`, `NESTED_ENV = "HERDR_REVIEW_EXCLUSIVE"`, `PREFIX = "herdr-review exclusive:"`, `DEFAULT_WAIT_SEC = 60.0`, `DEFAULT_TIMEOUT_SEC = 1800.0`, `POLL_SEC = 1.0`, `NOTICE_SEC = 15.0`, `GRACE_SEC = 10.0`, `STOP_WAIT_SEC = 15.0`, `EXIT_BUSY = 75`, `EXIT_TIMEOUT = 124`, `EXIT_NOT_EXECUTABLE = 126`, `EXIT_NOT_FOUND = 127`, `COMMAND_CHARS = 200`;
  - `class ExclusiveError(Exception)`; `@dataclass class Where(runs_dir: Path, run_dir: Path | None = None, run_id: str | None = None)`;
  - `runs_dir_of(run_dir: Path) -> Path`; `locate(environ: Mapping[str, str], cwd: Path) -> Where`;
  - `command_text(command: list[str]) -> str`; `format_duration(sec: float) -> str`; `since_sec(started_at: object) -> int | None`;
  - `read_holder(runs_dir: Path) -> dict | None`; `write_holder(runs_dir: Path, holder: dict) -> None`; `remove_holder(runs_dir: Path, pid: int) -> None`;
  - `holder_state(holder: dict | None) -> dict`; `queue_state(runs_dir: Path) -> dict`; `holder_text(state: Mapping) -> str`.
  - Test helpers in `tests/unit/test_exclusive.py`, used by later tasks: `iso_ago(seconds) -> str`, `make_run_dir(runs, run_id="hrtest", record_runs_dir=True) -> Path`, `class Held(runs, **holder)` with `.release()`, `class ExclusiveBase(unittest.TestCase)` with `self.root`, `self.runs`, `self.run_dir`, `self.hold(**holder) -> Held`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_exclusive.py`:

```python
import fcntl
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from herdr_review import PACKAGE_ROOT
from herdr_review.exclusive import (
    ExclusiveError, Where, format_duration, holder_text, locate, queue_state, read_holder, remove_holder,
    runs_dir_of, write_holder,
)

BIN = PACKAGE_ROOT / "bin" / "herdr-review"


def iso_ago(seconds: float) -> str:
    """A `started_at` of <seconds> ago, in the format of status.now_iso()."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def make_run_dir(runs: Path, run_id: str = "hrtest", record_runs_dir: bool = True) -> Path:
    """<runs>/<project>/<run> with a run.json, as launch writes one; <record_runs_dir>: False for a run launched
    before run.json recorded its runs_dir."""
    run_dir = runs / "proj-abc123" / f"20260925-100000-{run_id}"
    run_dir.mkdir(parents=True)
    data = {"run_id": run_id, "run_dir": str(run_dir)}
    if record_runs_dir:
        data["runs_dir"] = str(runs)
    (run_dir / "run.json").write_text(json.dumps(data))
    return run_dir


class Held:
    """The queue under <runs> held by this test process, the way a wrapper holds it; with <holder> fields the
    holder file too. Another open file description of the lock file conflicts with it, in this process as well."""

    def __init__(self, runs: Path, **holder):
        runs.mkdir(parents=True, exist_ok=True)
        self.fd: int | None = os.open(runs / "exclusive.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        if holder:
            (runs / "exclusive.json").write_text(json.dumps(holder, ensure_ascii=False), encoding="utf-8")

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ExclusiveBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / "runs"
        self.run_dir = make_run_dir(self.runs)

    def hold(self, **holder) -> Held:
        held = Held(self.runs, **holder)
        self.addCleanup(held.release)
        return held


class QueueFilesTest(ExclusiveBase):
    def test_a_run_records_its_runs_dir(self):
        self.assertEqual(runs_dir_of(self.run_dir), self.runs)

    def test_an_older_run_uses_the_grandparent_of_its_directory(self):
        old = make_run_dir(self.root / "elsewhere", run_id="hrold", record_runs_dir=False)
        self.assertEqual(runs_dir_of(old), self.root / "elsewhere")

    def test_an_unreadable_run_json_falls_back_to_the_grandparent(self):
        (self.run_dir / "run.json").write_text("{not json")
        self.assertEqual(runs_dir_of(self.run_dir), self.runs)

    def test_inside_a_review_the_run_names_the_queue(self):
        self.assertEqual(locate({"HERDR_REVIEW_RUN": str(self.run_dir)}, self.root), Where(self.runs, self.run_dir, "hrtest"))

    def test_a_run_variable_without_a_run_is_an_error(self):
        with self.assertRaises(ExclusiveError) as ctx:
            locate({"HERDR_REVIEW_RUN": str(self.root / "gone")}, self.root)
        self.assertIn("names no run directory", str(ctx.exception))

    def test_outside_a_review_the_config_names_the_queue_relative_to_the_cwd(self):
        cfg = self.root / "config.yaml"
        cfg.write_text("profiles:\n  codex: {kind: codex}\nsettings: {runs_dir: rel-runs}\n")
        cfg.chmod(0o600)
        where = locate({"HERDR_REVIEW_CONFIG": str(cfg)}, self.root / "here")
        self.assertEqual(where, Where((self.root / "here" / "rel-runs").resolve()))

    def test_outside_a_review_a_missing_config_is_an_error(self):
        with self.assertRaises(ExclusiveError) as ctx:
            locate({"HERDR_REVIEW_CONFIG": str(self.root / "none.yaml")}, self.root)
        self.assertIn("config not found", str(ctx.exception))

    def test_the_holder_file_round_trips_and_only_its_own_pid_removes_it(self):
        write_holder(self.runs, {"pid": 42, "command": "echo 'привет мир'"})
        self.assertEqual(read_holder(self.runs), {"pid": 42, "command": "echo 'привет мир'"})
        remove_holder(self.runs, 43)
        self.assertIsNotNone(read_holder(self.runs))
        remove_holder(self.runs, 42)
        self.assertIsNone(read_holder(self.runs))
        self.assertEqual(sorted(p.name for p in self.runs.iterdir()), ["proj-abc123"])   # no temporary file left

    def test_durations(self):
        for sec, text in ((0, "0s"), (59, "59s"), (60, "1m00s"), (185, "3m05s"), (3600, "1h00m"), (3725, "1h02m")):
            with self.subTest(sec=sec):
                self.assertEqual(format_duration(sec), text)


class QueueStateTest(ExclusiveBase):
    def test_no_queue_file_is_a_free_queue(self):
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_queue_file_nobody_holds_is_free_whatever_the_holder_file_says(self):
        self.hold(pid=42, agent="hrtest-codex", run_id="hrtest", command="mvn test", started_at=iso_ago(5)).release()
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_held_queue_names_its_holder(self):
        self.hold(pid=42, agent="hrtest-codex", run_id="hrtest", command="mvn test", started_at=iso_ago(185))
        state = queue_state(self.runs)
        self.assertEqual({k: state[k] for k in ("held", "agent", "run_id", "pid", "command")},
                         {"held": True, "agent": "hrtest-codex", "run_id": "hrtest", "pid": 42, "command": "mvn test"})
        self.assertGreaterEqual(state["since_sec"], 185)
        self.assertEqual(holder_text({**state, "since_sec": 185}), 'hrtest-codex (run hrtest) has run "mvn test" for 3m05s')

    def test_a_holder_outside_a_review(self):
        state = {"held": True, "agent": None, "run_id": None, "pid": 42, "command": "make", "since_sec": 5}
        self.assertEqual(holder_text(state), 'pid 42 outside a review has run "make" for 5s')

    def test_a_held_queue_without_a_holder_file_has_an_unnamed_holder(self):   # Review Focus 3
        self.hold()                                   # a holder killed before it wrote the file, or while it left
        state = queue_state(self.runs)
        self.assertEqual(state, {"held": True, "agent": None, "run_id": None, "pid": None, "command": None, "since_sec": None})
        self.assertEqual(holder_text(state), "another process holds the queue")

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_an_unreadable_queue_file_cannot_be_probed(self):
        self.hold().release()
        (self.runs / "exclusive.lock").chmod(0)
        state = queue_state(self.runs)
        self.assertIsNone(state["held"])
        self.assertIn("Permission denied", state["error"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_exclusive -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'herdr_review.exclusive'`.

- [ ] **Step 3: Write the module**

Create `herdr_review/exclusive.py`:

```python
"""herdr-review exclusive: heavy commands take turns, one at a time on this machine.

A heavy command — a build, tests, a dependency install, a server — runs only while its wrapper holds an
exclusive flock on <runs_dir>/exclusive.lock. The lock lives exactly as long as the wrapper: the kernel drops it
when the wrapper exits or dies, and the command never inherits its descriptor. <runs_dir>/exclusive.json names
the holder for messages and `status`; it is a hint, never proof.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .config import ConfigError, load_config

LOCK_NAME = "exclusive.lock"
HOLDER_NAME = "exclusive.json"
RUN_ENV = "HERDR_REVIEW_RUN"
AGENT_ENV = "HERDR_REVIEW_AGENT"
NESTED_ENV = "HERDR_REVIEW_EXCLUSIVE"
PREFIX = "herdr-review exclusive:"
DEFAULT_WAIT_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 1800.0
POLL_SEC = 1.0
NOTICE_SEC = 15.0
GRACE_SEC = 10.0
STOP_WAIT_SEC = 15.0
EXIT_BUSY = 75
EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127
COMMAND_CHARS = 200


class ExclusiveError(Exception):
    """The wrapper itself cannot work: exit 1."""


@dataclass
class Where:
    """The queue a wrapper takes its turn in, and the run it belongs to (None outside a review)."""
    runs_dir: Path
    run_dir: Path | None = None
    run_id: str | None = None


def _run_json(run_dir: Path) -> dict:
    try:
        data = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runs_dir_of(run_dir: Path) -> Path:
    """A run's runs directory: as its run.json records it; for a run launched before run.json recorded it, the
    run directory's grandparent (<runs_dir>/<project>/<run>)."""
    recorded = _run_json(run_dir).get("runs_dir")
    return Path(recorded) if isinstance(recorded, str) and recorded else Path(run_dir).parent.parent


def locate(environ: Mapping[str, str], cwd: Path) -> Where:
    """The queue of the run HERDR_REVIEW_RUN names; outside a review, the queue in the config's runs_dir, resolved
    against <cwd> as launch resolves it."""
    run = environ.get(RUN_ENV)
    if run:
        run_dir = Path(run).expanduser()
        if not (run_dir / "run.json").is_file():
            raise ExclusiveError(f"{RUN_ENV}={run} names no run directory: it has no run.json")
        run_id = _run_json(run_dir).get("run_id")
        return Where(runs_dir_of(run_dir), run_dir, run_id if isinstance(run_id, str) and run_id else None)
    try:
        cfg = load_config(environ=environ)
    except ConfigError as e:
        raise ExclusiveError(str(e)) from e
    return Where((Path(cwd) / cfg.settings.runs_dir).resolve())


def command_text(command: list[str]) -> str:
    """The command as a shell would read it, cut to COMMAND_CHARS."""
    text = shlex.join(command)
    return text if len(text) <= COMMAND_CHARS else text[:COMMAND_CHARS - 1] + "…"


def format_duration(sec: float) -> str:
    s = max(0, int(sec))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def since_sec(started_at: object) -> int | None:
    """Whole seconds since a timestamp in the format of status.now_iso(); None without a usable one."""
    if not isinstance(started_at, str):
        return None
    try:
        start = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if start.tzinfo is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - start).total_seconds()))


def read_holder(runs_dir: Path) -> dict | None:
    try:
        data = json.loads((Path(runs_dir) / HOLDER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_holder(runs_dir: Path, holder: dict) -> None:
    """Atomically: a reader sees the old holder or the new one, never half of it."""
    path = Path(runs_dir) / HOLDER_NAME
    tmp = path.with_name(f"{HOLDER_NAME}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(holder, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def remove_holder(runs_dir: Path, pid: int) -> None:
    """Remove the holder file while it names <pid>: never another holder's."""
    holder = read_holder(runs_dir)
    if holder is not None and holder.get("pid") == pid:
        (Path(runs_dir) / HOLDER_NAME).unlink(missing_ok=True)


def holder_state(holder: dict | None) -> dict:
    """The queue as held by <holder>, the holder file's content; None: there is no readable holder file."""
    h = holder or {}
    return {
        "held": True, "agent": h.get("agent"), "run_id": h.get("run_id"), "pid": h.get("pid"),
        "command": h.get("command"), "since_sec": since_sec(h.get("started_at")),
    }


def queue_state(runs_dir: Path) -> dict:
    """The build queue in <runs_dir>: holder_state() while a wrapper holds it, {"held": False} when nobody does,
    {"held": None, "error": …} when the queue file cannot be probed. The probe is a shared lock, taken and
    dropped at once: a waiter polling meanwhile just tries again."""
    try:
        fd = os.open(Path(runs_dir) / LOCK_NAME, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"held": False}
    except OSError as e:
        return {"held": None, "error": str(e)}
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return holder_state(read_holder(runs_dir))
    except OSError as e:
        return {"held": None, "error": str(e)}
    finally:
        os.close(fd)
    return {"held": False}


def holder_text(state: Mapping) -> str:
    """`<agent> (run <run_id>) has run "<command>" for <duration>`, or `pid <pid> outside a review …`."""
    pid, command = state.get("pid"), state.get("command")
    if pid is None or command is None:
        return "another process holds the queue"
    if state.get("run_id"):
        name = state.get("agent") or f"pid {pid}"
        who = f"{name} (run {state['run_id']})"
    else:
        who = f"pid {pid} outside a review"
    took = state.get("since_sec")
    return f'{who} has run "{command}"' + ("" if took is None else f" for {format_duration(took)}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_exclusive -v`
Expected: every test passes (`test_an_unreadable_queue_file_cannot_be_probed` is skipped under root).
Then run `tests/run.sh unit`: every test passes.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/exclusive.py tests/unit/test_exclusive.py
git commit -m "feat: the build queue's files and state"
```

---

### Task 2: Every agent names itself and leaves the index lock alone; run.json records runs_dir

**Files:**
- Modify: `herdr_review/runner.py` (imports; `_base_env`; new `_agent_env`; `_place_tabs`, `_place_grid`, `start_fixer`)
- Modify: `herdr_review/launch.py` (imports; `run_json`; `env_all`)
- Test: `tests/unit/test_runner_start.py`, `tests/unit/test_launch.py`

**Interfaces:**
- Consumes: `exclusive.AGENT_ENV` (Task 1).
- Produces: `Runner._agent_env(name: str, profile: str) -> dict[str, str]`; every reviewer, the fixer and the orchestrator start with `GIT_OPTIONAL_LOCKS=0` and `HERDR_REVIEW_AGENT=<agent name>`; `run.json` has `"runs_dir": "<resolved absolute path>"`, which `exclusive.runs_dir_of` reads (Task 1).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_runner_start.py`, `StartReviewersTest.test_tabs_layout_starts_every_reviewer`, replace the two env assertions:

```python
        self.assertEqual(tabs[0][4], {"HERDR_REVIEW_RUN": str(run_dir), "TOKEN": "s3cret"})
        self.assertEqual(tabs[1][4], {"HERDR_REVIEW_RUN": str(run_dir)})
```

with:

```python
        self.assertEqual(tabs[0][4], {"HERDR_REVIEW_RUN": str(run_dir), "GIT_OPTIONAL_LOCKS": "0", "TOKEN": "s3cret",
                                      "HERDR_REVIEW_AGENT": "hrtest-claude-opus"})
        self.assertEqual(tabs[1][4], {"HERDR_REVIEW_RUN": str(run_dir), "GIT_OPTIONAL_LOCKS": "0",
                                      "HERDR_REVIEW_AGENT": "hrtest-codex"})
```

Add to `StartReviewersTest`:

```python
    def test_a_grid_pane_names_its_agent_and_leaves_the_index_lock_alone(self):
        run_dir = make_run(self.root, self.repo, layout="grid")
        self.runner(run_dir).start_reviewers()
        envs = [s[5] for s in self.herdr.calls_named("pane_split")]
        self.assertEqual(sorted(e["HERDR_REVIEW_AGENT"] for e in envs), ["hrtest-claude-opus", "hrtest-codex", "hrtest-gemini"])
        self.assertEqual({e["GIT_OPTIONAL_LOCKS"] for e in envs}, {"0"})

    def test_a_profile_may_set_git_optional_locks_but_never_the_agent_name(self):
        self.cfg.profiles["codex"].env = {"GIT_OPTIONAL_LOCKS": "1", "HERDR_REVIEW_AGENT": "someone-else"}
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.runner(run_dir).start_reviewers()
        env = self.herdr.calls_named("tab_create")[0][4]
        self.assertEqual((env["GIT_OPTIONAL_LOCKS"], env["HERDR_REVIEW_AGENT"]), ("1", "hrtest-codex"))

    def test_the_fixer_names_itself_and_leaves_the_index_lock_alone(self):
        run_dir = make_run(self.root, self.repo)
        self.runner(run_dir).start_fixer()
        env = self.herdr.calls_named("tab_create")[-1][4]
        self.assertEqual((env["GIT_OPTIONAL_LOCKS"], env["HERDR_REVIEW_AGENT"]), ("0", "hrtest-fixer"))
```

In `tests/unit/test_launch.py`, add to `LaunchTest`:

```python
    def test_the_run_records_its_runs_dir_and_the_orchestrator_names_itself(self):
        res = self.do_launch()
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual(run_json["runs_dir"], str((self.root / "runs").resolve()))
        env = self.herdr.calls_named("tab_create")[0][4]
        self.assertEqual((env["GIT_OPTIONAL_LOCKS"], env["HERDR_REVIEW_AGENT"]), ("0", "hrtest-orch"))
```

and at the end of `test_relative_runs_dir_is_resolved_against_the_launch_cwd` (after its last assertion):

```python
        self.assertEqual(run_json["runs_dir"], str((sub / ".review-runs").resolve()))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_start tests.unit.test_launch -v`
Expected: FAIL — the env dicts lack `GIT_OPTIONAL_LOCKS` and `HERDR_REVIEW_AGENT` (`KeyError: 'HERDR_REVIEW_AGENT'` in the new tests); `KeyError: 'runs_dir'` in the launch tests.

- [ ] **Step 3: Implement**

`herdr_review/runner.py` — the package import becomes:

```python
from . import PROMPTS_DIR, exclusive, gitutil
```

Replace `_base_env` with:

```python
    def _base_env(self) -> dict[str, str]:
        # GIT_OPTIONAL_LOCKS=0: an agent's `git status` or `git diff` never takes .git/index.lock from under the
        # owner's own `git commit`; the locks that `git add` and `git commit` need are not optional and stay.
        return {"HERDR_REVIEW_RUN": str(self.run_dir), "GIT_OPTIONAL_LOCKS": "0"}

    def _agent_env(self, name: str, profile: str) -> dict[str, str]:
        """An agent's environment: the run's, then its profile's env, which may override the run's, then its own
        name, which `herdr-review exclusive` records as the holder of the build queue."""
        return {**self._base_env(), **self._profile_env(profile), exclusive.AGENT_ENV: name}
```

In `_place_tabs`, replace

```python
                env = {**self._base_env(), **self._profile_env(s["profile"])}
```

with

```python
                env = self._agent_env(s["name"], s["profile"])
```

In `_place_grid`, replace

```python
                env = {**self._base_env(), **(self._profile_env(spec["profile"]) if spec else {})}
```

with

```python
                env = self._agent_env(spec["name"], spec["profile"]) if spec else self._base_env()
```

In `start_fixer`, replace

```python
        env = {**self._base_env(), **self._profile_env(fx["profile"])}
```

with

```python
        env = self._agent_env(name, fx["profile"])
```

`herdr_review/launch.py` — the package import becomes:

```python
from . import PROMPTS_DIR, __version__, exclusive, gitutil
```

In `run_json`, after `"run_dir": str(run_dir),` add:

```python
            "runs_dir": str(runs_dir),
```

Replace `env_all = {"HERDR_REVIEW_RUN": str(run_dir)}` with:

```python
    env_all = {"HERDR_REVIEW_RUN": str(run_dir), "GIT_OPTIONAL_LOCKS": "0"}
```

and right after `env_all.update(cfg.profiles[orch_profile].env)` add:

```python
    env_all[exclusive.AGENT_ENV] = orch["name"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_runner_start tests.unit.test_launch -v`
Expected: PASS. Then `tests/run.sh`: every unit and bats test passes.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/runner.py herdr_review/launch.py tests/unit/test_runner_start.py tests/unit/test_launch.py
git commit -m "feat: every agent names itself and leaves the index lock alone"
```

---

### Task 3: `herdr-review exclusive` takes turns

**Files:**
- Modify: `herdr_review/exclusive.py` (the wrapper: `Turn`, `_open_queue`, `_say`, `_log`, `_take_turn`, `_exec_in_turn`, `_run_command`, `run`)
- Modify: `herdr_review/cli.py` (imports; `seconds`; `poll_sec_from`; the `exclusive` parser; `cmd_exclusive`; `cmd_run`; `dispatch`)
- Test: `tests/unit/test_exclusive.py`, `tests/unit/test_cli.py`
- Create: `tests/bats/exclusive.bats`

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces:
  - `exclusive.run(command: list[str], *, wait_sec: float = DEFAULT_WAIT_SEC, environ: Mapping[str, str] = os.environ, cwd: Path | None = None, poll_sec: float = POLL_SEC, notice_sec: float = NOTICE_SEC) -> int` (Task 4 adds `timeout_sec` and `grace_sec`);
  - `exclusive.Turn(outcome, waited, holder, signum=0)`; `_take_turn(fd, runs_dir, wait_sec, poll_sec, notice_sec, received: list[int]) -> Turn` (Task 4 passes the real `received`);
  - CLI `herdr-review exclusive [--wait SEC] -- COMMAND [ARG...]`; `cli.seconds(zero_ok: bool) -> Callable[[str], float]`; `cli.poll_sec_from(environ, default: float) -> float`; `cli.cmd_exclusive(args, environ) -> int`;
  - test helpers in `tests/unit/test_exclusive.py`: `clean_env(**extra) -> dict`, `exclusive_cmd(*args) -> list[str]`, `wait_until(predicate, timeout=10.0) -> bool`, `alive(pid) -> bool`, `kill_quietly(pid)`, `stop_quietly(proc)`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_exclusive.py`, extend the imports:

```python
import fcntl
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
```

(keep the existing `from herdr_review …` imports), add these helpers after `make_run_dir`:

```python
def clean_env(**extra: str) -> dict[str, str]:
    """The test's environment without anything that would put a wrapper inside a run or inside another turn."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_REVIEW_")}
    env["HERDR_REVIEW_POLL_SEC"] = "0.05"
    env.update(extra)
    return env


def exclusive_cmd(*args: str) -> list[str]:
    return [sys.executable, str(BIN), "exclusive", *args]


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def alive(pid: int) -> bool:
    """Whether <pid> runs; a zombie counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_quietly(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
```

In `ExclusiveBase.setUp`, after `self.run_dir = make_run_dir(self.runs)`, add:

```python
        self.env = clean_env(HERDR_REVIEW_RUN=str(self.run_dir), HERDR_REVIEW_AGENT="hrtest-codex")
```

and append this class at the end of the file:

```python
class TurnTest(ExclusiveBase):
    def exclusive(self, *args: str, env: dict | None = None, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(exclusive_cmd(*args), env=self.env if env is None else env, capture_output=True,
                              text=True, timeout=60, **kw)

    def test_a_free_queue_runs_the_command_and_passes_its_code_through(self):
        self.assertEqual(self.exclusive("--", "true").returncode, 0)
        self.assertEqual(self.exclusive("--", "sh", "-c", "exit 3").returncode, 3)
        self.assertEqual(queue_state(self.runs), {"held": False})
        self.assertIsNone(read_holder(self.runs))

    def test_the_holder_file_names_the_command_while_it_runs(self):
        seen = self.root / "seen.json"
        args = ["sh", "-c", 'cp "$1" "$2"', "copy", str(self.runs / "exclusive.json"), str(seen)]
        p = self.exclusive("--", *args)
        self.assertEqual(p.returncode, 0, p.stderr)
        holder = json.loads(seen.read_text())
        self.assertEqual((holder["agent"], holder["run_id"], holder["run_dir"]), ("hrtest-codex", "hrtest", str(self.run_dir)))
        self.assertEqual(holder["command"], shlex.join(args))
        self.assertIsInstance(holder["pid"], int)
        self.assertIsNone(read_holder(self.runs))                  # gone once the command is done

    def test_a_busy_queue_exits_75_and_runs_nothing(self):
        marker = self.root / "ran"
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(65))
        p = self.exclusive("--wait", "0.3", "--", "touch", str(marker))
        self.assertEqual(p.returncode, 75)
        self.assertFalse(marker.exists())
        self.assertIn('herdr-review exclusive: waiting — hrtest-gemini (run hrtest) has run "mvn test" for 1m0', p.stderr)
        self.assertIn('herdr-review exclusive: busy — hrtest-gemini (run hrtest) has run "mvn test" for 1m0', p.stderr)
        self.assertIn("; nothing was run. Do other work and run the same command again later.", p.stderr)

    def test_wait_zero_tries_once(self):
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        p = self.exclusive("--wait", "0", "--", "true")
        self.assertEqual(p.returncode, 75)
        self.assertNotIn("waiting —", p.stderr)
        self.assertIn("busy —", p.stderr)

    def test_the_turn_comes_when_the_holder_lets_go(self):
        marker = self.root / "ran"
        held = self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        p = subprocess.Popen(exclusive_cmd("--wait", "30", "--", "touch", str(marker)), env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(stop_quietly, p)
        time.sleep(0.5)
        self.assertIsNone(p.poll())
        self.assertFalse(marker.exists())
        held.release()
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertTrue(marker.exists())
        self.assertIn("herdr-review exclusive: your turn after", err)

    def test_the_command_gets_no_input(self):
        p = self.exclusive("--", "cat", input="typed by someone\n")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_every_argument_after_the_separator_reaches_the_command(self):
        p = self.exclusive("--", "printf", "%s\n", "--", "--wait", "b c", "привет")
        self.assertEqual(p.stdout, "--\n--wait\nb c\nпривет\n")

    def test_without_a_separator_the_first_non_option_starts_the_command(self):
        self.assertEqual(self.exclusive("printf", "%s\n", "--wait").stdout, "--wait\n")

    def test_no_command_is_a_usage_error(self):
        for args in ((), ("--",), ("--wait", "5", "--")):
            with self.subTest(args=args):
                p = self.exclusive(*args)
                self.assertEqual(p.returncode, 2)
                self.assertIn("herdr-review exclusive: no command given", p.stderr)

    def test_a_bad_wait_is_a_usage_error(self):
        for value in ("-1", "soon", "nan", "inf"):
            with self.subTest(value=value):
                self.assertEqual(self.exclusive("--wait", value, "--", "true").returncode, 2)

    def test_a_missing_program_exits_127_and_frees_the_queue(self):
        p = self.exclusive("--", "no-such-program-for-herdr-review")
        self.assertEqual(p.returncode, 127)
        self.assertIn("herdr-review exclusive: command not found: no-such-program-for-herdr-review", p.stderr)
        self.assertEqual(queue_state(self.runs), {"held": False})
        self.assertIsNone(read_holder(self.runs))

    def test_a_file_that_is_not_executable_exits_126(self):
        script = self.root / "script.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        p = self.exclusive("--", str(script))
        self.assertEqual(p.returncode, 126)
        self.assertIn(f"herdr-review exclusive: cannot execute {script}", p.stderr)

    def test_a_queue_it_cannot_open_exits_1(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file where the runs directory should be\n")
        run_dir = make_run_dir(self.root / "other")
        run_json = json.loads((run_dir / "run.json").read_text())
        run_json["runs_dir"] = str(blocker / "runs")
        (run_dir / "run.json").write_text(json.dumps(run_json))
        p = self.exclusive("--", "true", env=clean_env(HERDR_REVIEW_RUN=str(run_dir)))
        self.assertEqual(p.returncode, 1)
        self.assertIn("herdr-review exclusive: cannot open the build queue", p.stderr)

    def test_a_run_variable_without_a_run_exits_1(self):
        p = self.exclusive("--", "true", env=clean_env(HERDR_REVIEW_RUN=str(self.root / "gone")))
        self.assertEqual(p.returncode, 1)
        self.assertIn("herdr-review exclusive: HERDR_REVIEW_RUN=", p.stderr)

    def test_inside_a_turn_a_nested_call_runs_at_once(self):
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="make", started_at=iso_ago(5))
        p = self.exclusive("--wait", "0", "--", "true", env={**self.env, "HERDR_REVIEW_EXCLUSIVE": "1"})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_script_under_the_wrapper_may_call_the_wrapper_again(self):
        p = self.exclusive("--", *exclusive_cmd("--wait", "0", "--", "sh", "-c", "exit 4"))
        self.assertEqual(p.returncode, 4, p.stderr)

    def test_the_run_log_records_the_turn(self):
        self.exclusive("--", "true")
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        self.exclusive("--wait", "0", "--", "true")
        log = (self.run_dir / "runner.log").read_text()
        self.assertRegex(log, r'exclusive: hrtest-codex running "true" after 0s of waiting\n')
        self.assertRegex(log, r"exclusive: hrtest-codex done after \d+s, exit 0\n")
        self.assertRegex(log, r'exclusive: hrtest-codex busy after 0s: hrtest-gemini \(run hrtest\) has run "mvn test"')

    def test_outside_a_review_the_config_names_the_queue_and_creates_it(self):   # Review Focus 4
        fresh = self.root / "fresh" / "runs"
        cfg = self.root / "config.yaml"
        cfg.write_text(f"profiles:\n  codex: {{kind: codex}}\nsettings: {{runs_dir: {fresh}}}\n")
        cfg.chmod(0o600)
        p = self.exclusive("--", "true", env=clean_env(HERDR_REVIEW_CONFIG=str(cfg)))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((fresh / "exclusive.lock").stat().st_mode), 0o600)

    def test_a_command_that_leaves_a_background_process_returns_at_once_and_frees_the_queue(self):   # Review Focus 2
        pidfile = self.root / "bg.pid"
        started = time.monotonic()
        p = subprocess.run(exclusive_cmd("--", "sh", "-c", 'sleep 30 >/dev/null 2>&1 & echo $! > "$1"', "sh", str(pidfile)),
                           env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
        background = int(pidfile.read_text())
        self.addCleanup(kill_quietly, background)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(queue_state(self.runs), {"held": False})
        self.assertTrue(alive(background))                     # the prompt asks agents to stop it; the wrapper does not

    def test_spaces_quotes_and_non_ascii_reach_the_command_and_the_holder_file(self):   # Review Focus 5
        seen = self.root / "seen.json"
        args = ["sh", "-c", 'cp "$1" "$2"; printf "%s\\n" "$3"', "copy", str(self.runs / "exclusive.json"), str(seen),
                "echo \"привет мир\" 'x'"]
        p = self.exclusive("--", *args)
        self.assertEqual(p.stdout, "echo \"привет мир\" 'x'\n")
        self.assertEqual(json.loads(seen.read_text(encoding="utf-8"))["command"], shlex.join(args))
        self.assertIn(shlex.join(args), (self.run_dir / "runner.log").read_text(encoding="utf-8"))
```

In `tests/unit/test_cli.py`, add to `CliParsingTest`:

```python
    def test_exclusive_parses(self):
        args = build_parser().parse_args(["exclusive", "--wait", "5", "--", "git", "commit", "--", "f"])
        self.assertEqual((args.cmd, args.wait, args.command), ("exclusive", 5.0, ["--", "git", "commit", "--", "f"]))
```

Create `tests/bats/exclusive.bats`:

```bash
#!/usr/bin/env bats
load helpers

setup() { setup_env; }
teardown() { teardown_env; }

@test "exclusive: two calls started together take turns" {
  cat > "$TMP/job.sh" <<'EOF'
#!/bin/sh
python3 -c 'import time; print(time.time())' > "$1.start"
sleep 1
python3 -c 'import time; print(time.time())' > "$1.end"
EOF
  chmod +x "$TMP/job.sh"
  "$HR" exclusive -- "$TMP/job.sh" "$TMP/a" &
  first=$!
  "$HR" exclusive -- "$TMP/job.sh" "$TMP/b" &
  second=$!
  wait "$first"
  wait "$second"
  python3 - "$TMP" <<'PY'
import sys
t = sys.argv[1]
read = lambda name: float(open(f"{t}/{name}").read())
a, b = (read("a.start"), read("a.end")), (read("b.start"), read("b.end"))
assert a[1] <= b[0] or b[1] <= a[0], (a, b)
PY
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_exclusive tests.unit.test_cli -v`
Expected: the `TurnTest` tests fail — `herdr-review` has no `exclusive` command (`invalid choice: 'exclusive'`, exit 2); `test_exclusive_parses` fails the same way.
Run: `bats tests/bats/exclusive.bats`
Expected: FAIL.

- [ ] **Step 3: Implement the wrapper**

In `herdr_review/exclusive.py`, extend the imports to:

```python
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple

from .config import ConfigError, load_config
from .status import now_iso
```

and append at the end of the module:

```python
class Turn(NamedTuple):
    outcome: str                # "turn", "busy" or "signal"
    waited: float | None        # seconds spent waiting; None when the queue was free at once
    holder: dict                # the queue's holder_state() when busy
    signum: int = 0             # the stop signal that ended the wait


def _open_queue(runs_dir: Path) -> int:
    try:
        runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(runs_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise ExclusiveError(f"cannot open the build queue {runs_dir / LOCK_NAME}: {e}") from e


def _say(line: str) -> None:
    print(f"{PREFIX} {line}", file=sys.stderr, flush=True)


def _log(where: Where, line: str) -> None:
    """A line in the run's runner.log, in the runner's format; nothing outside a review."""
    if where.run_dir is None:
        return
    try:
        with open(where.run_dir / "runner.log", "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} exclusive: {line}\n")
    except OSError:
        pass


def _take_turn(fd: int, runs_dir: Path, wait_sec: float, poll_sec: float, notice_sec: float, received: list[int]) -> Turn:
    """Poll the lock until it is ours, <wait_sec> passes, or a stop signal arrives in <received>. Say who holds the
    queue at once, then every <notice_sec>."""
    start = time.monotonic()
    next_notice = start
    waited: float | None = None
    while True:
        if received:
            return Turn("signal", waited, {}, received[0])
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return Turn("turn", None if waited is None else time.monotonic() - start, {})
        except BlockingIOError:
            pass
        now = time.monotonic()
        waited = now - start
        holder = holder_state(read_holder(runs_dir))
        if waited >= wait_sec:
            return Turn("busy", waited, holder)
        if now >= next_notice:
            _say(f"waiting — {holder_text(holder)}")
            next_notice = now + notice_sec
        time.sleep(min(poll_sec, wait_sec - waited))


def _exec_in_turn(command: list[str], environ: Mapping[str, str]) -> int:
    """Inside another wrapper's turn: become the command, under the outer turn's lock and timeout."""
    try:
        os.execvpe(command[0], command, dict(environ))
    except FileNotFoundError:
        _say(f"command not found: {command[0]}")
        return EXIT_NOT_FOUND
    except OSError as e:
        _say(f"cannot execute {command[0]}: {e.strerror or e}")
        return EXIT_NOT_EXECUTABLE


def _run_command(command: list[str], environ: Mapping[str, str]) -> tuple[int, float]:
    """Run the command with an empty stdin: (its exit code, 128+N for signal N; the seconds it ran)."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(command, env={**environ, NESTED_ENV: "1"}, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        _say(f"command not found: {command[0]}")
        return EXIT_NOT_FOUND, 0.0
    except OSError as e:
        _say(f"cannot execute {command[0]}: {e.strerror or e}")
        return EXIT_NOT_EXECUTABLE, 0.0
    code = proc.wait()
    return (code if code >= 0 else 128 - code), time.monotonic() - started


def run(command: list[str], *, wait_sec: float = DEFAULT_WAIT_SEC, environ: Mapping[str, str] = os.environ,
        cwd: Path | None = None, poll_sec: float = POLL_SEC, notice_sec: float = NOTICE_SEC) -> int:
    """Run <command> once this process holds the machine's build queue. The exit code for the CLI."""
    if environ.get(NESTED_ENV):
        return _exec_in_turn(command, environ)
    where = locate(environ, Path.cwd() if cwd is None else Path(cwd))
    who = environ.get(AGENT_ENV) or f"pid {os.getpid()}"
    shown = command_text(command)
    fd = _open_queue(where.runs_dir)
    try:
        turn = _take_turn(fd, where.runs_dir, wait_sec, poll_sec, notice_sec, [])
        if turn.outcome == "busy":
            text = holder_text(turn.holder)
            _say(f"busy — {text}; nothing was run. Do other work and run the same command again later.")
            _log(where, f"{who} busy after {format_duration(turn.waited or 0)}: {text}")
            return EXIT_BUSY
        pid = os.getpid()
        try:
            write_holder(where.runs_dir, {
                "pid": pid, "agent": environ.get(AGENT_ENV) or None, "run_id": where.run_id,
                "run_dir": None if where.run_dir is None else str(where.run_dir), "command": shown,
                "cwd": os.getcwd(), "started_at": now_iso(),
            })
        except OSError as e:
            raise ExclusiveError(f"cannot write {where.runs_dir / HOLDER_NAME}: {e}") from e
        try:
            if turn.waited is not None:
                _say(f"your turn after {format_duration(turn.waited)}")
            _log(where, f'{who} running "{shown}" after {format_duration(turn.waited or 0)} of waiting')
            code, ran = _run_command(command, environ)
            _log(where, f"{who} done after {format_duration(ran)}, exit {code}")
            return code
        finally:
            remove_holder(where.runs_dir, pid)
    finally:
        os.close(fd)
```

- [ ] **Step 4: Wire the CLI**

In `herdr_review/cli.py`:

Imports — add `import math`, change `from typing import Mapping` to `from typing import Callable, Mapping`, and change the package import to:

```python
from . import PACKAGE_ROOT, __version__, exclusive, gitutil
```

Right after `RUNNER_PATH = …`, add:

```python
EXCLUSIVE_USAGE = "herdr-review exclusive [--wait SEC] -- COMMAND [ARG...]"


def seconds(zero_ok: bool) -> Callable[[str], float]:
    """An argparse type: a finite number of seconds, 0 allowed only when <zero_ok>."""
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
        if not math.isfinite(value) or value < 0 or (value == 0 and not zero_ok):
            raise argparse.ArgumentTypeError(f"must be {'0 or more' if zero_ok else 'more than 0'} seconds, not {text!r}")
        return value
    return parse


def poll_sec_from(environ: Mapping[str, str], default: float) -> float:
    """HERDR_REVIEW_POLL_SEC, the tests' knob for every polling loop; <default> without it."""
    raw = environ.get("HERDR_REVIEW_POLL_SEC")
    if raw is None:
        return default
    try:
        poll = float(raw)
    except ValueError:
        raise RunnerError(f"HERDR_REVIEW_POLL_SEC={raw!r} is not a number") from None
    if not poll > 0:
        raise RunnerError(f"HERDR_REVIEW_POLL_SEC={raw!r} must be a positive number")
    return poll
```

In `build_parser`, right before `r = sub.add_parser("run", …)`, add:

```python
    p = sub.add_parser("exclusive", help="run a heavy command (a build, tests, an install, a server) only while no other runs on this machine")
    p.add_argument("--wait", type=seconds(zero_ok=True), default=exclusive.DEFAULT_WAIT_SEC, metavar="SEC",
                   help="how long to wait for a turn before exiting 75 (default 60; 0 tries once)")
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --")
```

After `cmd_close`, add:

```python
def cmd_exclusive(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    command = list(args.command)
    if command[:1] == ["--"]:           # argparse keeps the separator in front of a REMAINDER
        command = command[1:]
    if not command:
        print(f"{exclusive.PREFIX} no command given; usage: {EXCLUSIVE_USAGE}", file=sys.stderr)
        return 2
    try:
        return exclusive.run(command, wait_sec=args.wait, environ=environ, cwd=Path.cwd(),
                             poll_sec=poll_sec_from(environ, exclusive.POLL_SEC))
    except exclusive.ExclusiveError as e:
        print(f"{exclusive.PREFIX} {e}", file=sys.stderr)
        return 1
```

In `cmd_run`, replace its first seven lines (from `raw_poll = …` through the second `raise RunnerError(…)`) with:

```python
    poll = poll_sec_from(environ, 5.0)
```

In `dispatch`, before `if args.cmd == "run":`, add:

```python
    if args.cmd == "exclusive":
        return cmd_exclusive(args, environ)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_exclusive tests.unit.test_cli -v`
Expected: PASS.
Run: `bats tests/bats/exclusive.bats`
Expected: PASS.
Then `tests/run.sh`: every unit and bats test passes.

- [ ] **Step 6: Commit**

```bash
git add herdr_review/exclusive.py herdr_review/cli.py tests/unit/test_exclusive.py tests/unit/test_cli.py tests/bats/exclusive.bats
git commit -m "feat: herdr-review exclusive runs heavy commands one at a time"
```

---

### Task 4: `exclusive` stops a command past its timeout or on a signal, with everything it started

**Files:**
- Modify: `herdr_review/exclusive.py` (imports; constants; `_catch_stop_signals`, `_die_with_wrapper`, `_children`, `descendants`, `_signal_all`, `_alive`, `_finish_off`; new `_run_command` and `run`)
- Modify: `herdr_review/cli.py` (`EXCLUSIVE_USAGE`; the `--timeout` option; `cmd_exclusive`)
- Test: `tests/unit/test_exclusive.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 3's `Turn`, `_take_turn`, `_open_queue`, `_say`, `_log`, `_exec_in_turn`.
- Produces: `exclusive.run(command, *, wait_sec=DEFAULT_WAIT_SEC, timeout_sec=DEFAULT_TIMEOUT_SEC, environ=os.environ, cwd=None, poll_sec=POLL_SEC, notice_sec=NOTICE_SEC, grace_sec=GRACE_SEC) -> int`; `exclusive.descendants(pid: int) -> list[int]`; CLI `herdr-review exclusive [--wait SEC] [--timeout SEC] -- COMMAND [ARG...]`. A wrapper that receives SIGTERM passes it to its command and releases the queue — Task 6's stop relies on that.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_exclusive.py`:

```python
HARNESS = (
    "import os, sys\n"
    "from herdr_review import exclusive\n"
    "sys.exit(exclusive.run(sys.argv[1:], timeout_sec=0.5, grace_sec=0.5, poll_sec=0.05, environ=os.environ))\n"
)


class StopTest(ExclusiveBase):
    def start(self, *args: str, **kw) -> subprocess.Popen:
        p = subprocess.Popen(exclusive_cmd(*args), env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, **kw)
        self.addCleanup(stop_quietly, p)
        return p

    def holder_written(self) -> bool:
        return wait_until(lambda: (self.runs / "exclusive.json").exists())

    def test_a_command_past_its_timeout_is_stopped_with_everything_it_started(self):
        pidfile = self.root / "bg.pid"
        started = time.monotonic()
        p = subprocess.run(exclusive_cmd("--timeout", "0.5", "--", "sh", "-c", 'sleep 30 & echo $! > "$1"; sleep 30', "sh", str(pidfile)),
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertLess(time.monotonic() - started, 10)
        self.assertRegex(p.stderr, r'herdr-review exclusive: timed out after \ds; stopped "sh -c')
        background = int(pidfile.read_text())
        self.addCleanup(kill_quietly, background)
        self.assertTrue(wait_until(lambda: not alive(background)))
        self.assertEqual(queue_state(self.runs), {"held": False})
        self.assertRegex((self.run_dir / "runner.log").read_text(), r"exclusive: hrtest-codex timed out after \d+s\n")

    def test_sigterm_stops_the_command_and_frees_the_queue(self):
        p = self.start("--", "sleep", "30")
        self.assertTrue(self.holder_written())
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 143, err)
        self.assertIn('herdr-review exclusive: SIGTERM received; stopped "sleep 30"', err)
        self.assertEqual(queue_state(self.runs), {"held": False})
        self.assertIsNone(read_holder(self.runs))

    def test_a_signal_while_waiting_runs_nothing(self):
        marker = self.root / "ran"
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        p = self.start("--wait", "30", "--", "touch", str(marker))
        first = p.stderr.readline()                  # the waiting notice: the signal handlers are in place
        self.assertIn("waiting —", first)
        p.send_signal(signal.SIGTERM)
        _, rest = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 143, first + rest)
        self.assertIn("herdr-review exclusive: SIGTERM received while waiting for a turn; nothing was run", rest)
        self.assertFalse(marker.exists())

    def test_ctrl_c_to_the_whole_group_stops_the_command_and_frees_the_queue(self):   # Review Focus 1
        p = self.start("--", "sleep", "30", start_new_session=True)
        self.assertTrue(self.holder_written())
        os.killpg(p.pid, signal.SIGINT)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertIn('herdr-review exclusive: SIGINT received; stopped "sleep 30"', err)
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(self):
        started = time.monotonic()
        p = subprocess.run([sys.executable, "-c", HARNESS, "sh", "-c", "trap '' TERM; sleep 30"], cwd=PACKAGE_ROOT,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(queue_state(self.runs), {"held": False})

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux-only")
    def test_on_linux_the_command_dies_with_a_wrapper_killed_by_sigkill(self):
        pidfile = self.root / "cmd.pid"
        p = self.start("--", "sh", "-c", 'echo $$ > "$1"; exec sleep 30', "sh", str(pidfile))
        self.assertTrue(wait_until(lambda: pidfile.exists() and pidfile.read_text().strip() != ""))
        command = int(pidfile.read_text())
        self.addCleanup(kill_quietly, command)
        p.kill()
        p.wait(timeout=30)
        self.assertTrue(wait_until(lambda: not alive(command)))
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_bad_timeout_is_a_usage_error(self):
        for value in ("0", "-5", "never"):
            with self.subTest(value=value):
                p = subprocess.run(exclusive_cmd("--timeout", value, "--", "true"), env=self.env, capture_output=True,
                                   text=True, timeout=30)
                self.assertEqual(p.returncode, 2)
```

In `tests/unit/test_cli.py`, add to `CliParsingTest`:

```python
    def test_exclusive_timeout_parses(self):
        self.assertEqual(build_parser().parse_args(["exclusive", "--", "true"]).timeout, 1800.0)
        self.assertEqual(build_parser().parse_args(["exclusive", "--timeout", "90", "--", "true"]).timeout, 90.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_exclusive tests.unit.test_cli -v`
Expected: `StopTest` fails — `--timeout` is an unknown option (exit 2), SIGTERM kills the wrapper by Python's default (`returncode -15`), the harness's `run()` rejects `timeout_sec`; `test_exclusive_timeout_parses` fails with `AttributeError: 'Namespace' object has no attribute 'timeout'`.

- [ ] **Step 3: Implement**

In `herdr_review/exclusive.py`, extend the imports:

```python
import ctypes
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, NamedTuple
```

add after `COMMAND_CHARS = 200`:

```python
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
PR_SET_PDEATHSIG = 1
```

add before `_run_command`:

```python
def _catch_stop_signals() -> list[int]:
    """Record SIGTERM, SIGINT and SIGHUP instead of dying of them: the wrapper stops its command and releases the
    queue itself. A signal the caller ignores stays ignored."""
    received: list[int] = []

    def record(signum: int, frame: object) -> None:
        received.append(signum)

    for sig in STOP_SIGNALS:
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, record)
    return received


def _die_with_wrapper() -> Callable[[], None] | None:
    """Linux: the command gets SIGKILL when the wrapper dies, even of SIGKILL (PR_SET_PDEATHSIG). Elsewhere
    nothing: there is no such call. prctl is looked up before the fork: preexec_fn must not import."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    wrapper = os.getpid()

    def preexec() -> None:
        prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
        if os.getppid() != wrapper:          # the wrapper died before the call took effect
            os._exit(EXIT_NOT_EXECUTABLE)

    return preexec


def _children() -> dict[int, list[int]]:
    """Parent pid → child pids: from /proc on Linux, from `ps` elsewhere; empty when neither answers."""
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if (proc / "self" / "stat").exists():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry.name))
        return children
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return children
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def descendants(pid: int) -> list[int]:
    """Every process under <pid>, as the process table shows it now."""
    tree = _children()
    found: list[int] = []
    todo = [pid]
    while todo:
        for child in tree.get(todo.pop(), []):
            if child not in found:
                found.append(child)
                todo.append(child)
    return found


def _signal_all(pids, sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid: int) -> bool:
    """Whether <pid> runs; a zombie counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _finish_off(pids: set[int], grace_sec: float, poll_sec: float) -> None:
    """The command's first process has ended: whatever of <pids> still lives after <grace_sec> gets SIGKILL."""
    deadline = time.monotonic() + max(0.0, grace_sec)
    alive = {p for p in pids if _alive(p)}
    while alive and time.monotonic() < deadline:
        time.sleep(min(poll_sec, 0.2))
        alive = {p for p in alive if _alive(p)}
    _signal_all(alive, signal.SIGKILL)
```

Replace `_run_command` with:

```python
def _run_command(command: list[str], environ: Mapping[str, str], timeout_sec: float, poll_sec: float,
                 grace_sec: float, received: list[int], shown: str) -> tuple[int, float, bool]:
    """Run the command with an empty stdin, in the wrapper's process group. On a stop signal in <received>, or
    after <timeout_sec>, signal it and every process under it; SIGKILL whatever is left <grace_sec> later.
    (the exit code, the seconds it ran, whether it timed out)."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(command, env={**environ, NESTED_ENV: "1"}, stdin=subprocess.DEVNULL,
                                preexec_fn=_die_with_wrapper())
    except FileNotFoundError:
        _say(f"command not found: {command[0]}")
        return EXIT_NOT_FOUND, 0.0, False
    except OSError as e:
        _say(f"cannot execute {command[0]}: {e.strerror or e}")
        return EXIT_NOT_EXECUTABLE, 0.0, False
    stopping = 0                 # the signal the command was stopped with
    timed_out = False
    killed = False
    targets: set[int] = set()
    stopped_at = 0.0
    while True:
        try:
            code = proc.wait(timeout=poll_sec)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if not stopping:
            if received:
                stopping = received[0]
            elif now - started >= timeout_sec:
                stopping, timed_out = signal.SIGTERM, True
            if stopping:
                targets = {proc.pid, *descendants(proc.pid)}
                _signal_all(targets, stopping)
                stopped_at = now
        elif not killed and now - stopped_at >= grace_sec:
            targets |= {proc.pid, *descendants(proc.pid)}
            _signal_all(targets, signal.SIGKILL)
            killed = True
    ran = time.monotonic() - started
    if not stopping and received:          # Ctrl-C to the whole group reached the command too
        stopping = received[0]
    if targets and not killed:
        _finish_off(targets - {proc.pid}, grace_sec - (time.monotonic() - stopped_at), poll_sec)
    if timed_out:
        _say(f'timed out after {format_duration(ran)}; stopped "{shown}"')
        return EXIT_TIMEOUT, ran, True
    if stopping:
        _say(f'{signal.Signals(stopping).name} received; stopped "{shown}"')
        return 128 + stopping, ran, False
    return (code if code >= 0 else 128 - code), ran, False
```

Replace `run` with:

```python
def run(command: list[str], *, wait_sec: float = DEFAULT_WAIT_SEC, timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        environ: Mapping[str, str] = os.environ, cwd: Path | None = None, poll_sec: float = POLL_SEC,
        notice_sec: float = NOTICE_SEC, grace_sec: float = GRACE_SEC) -> int:
    """Run <command> once this process holds the machine's build queue. The exit code for the CLI."""
    if environ.get(NESTED_ENV):
        return _exec_in_turn(command, environ)
    where = locate(environ, Path.cwd() if cwd is None else Path(cwd))
    who = environ.get(AGENT_ENV) or f"pid {os.getpid()}"
    shown = command_text(command)
    received = _catch_stop_signals()
    fd = _open_queue(where.runs_dir)
    try:
        turn = _take_turn(fd, where.runs_dir, wait_sec, poll_sec, notice_sec, received)
        if turn.outcome == "signal":
            name = signal.Signals(turn.signum).name
            _say(f"{name} received while waiting for a turn; nothing was run")
            _log(where, f"{who} stopped by {name} while waiting")
            return 128 + turn.signum
        if turn.outcome == "busy":
            text = holder_text(turn.holder)
            _say(f"busy — {text}; nothing was run. Do other work and run the same command again later.")
            _log(where, f"{who} busy after {format_duration(turn.waited or 0)}: {text}")
            return EXIT_BUSY
        pid = os.getpid()
        try:
            write_holder(where.runs_dir, {
                "pid": pid, "agent": environ.get(AGENT_ENV) or None, "run_id": where.run_id,
                "run_dir": None if where.run_dir is None else str(where.run_dir), "command": shown,
                "cwd": os.getcwd(), "started_at": now_iso(),
            })
        except OSError as e:
            raise ExclusiveError(f"cannot write {where.runs_dir / HOLDER_NAME}: {e}") from e
        try:
            if turn.waited is not None:
                _say(f"your turn after {format_duration(turn.waited)}")
            _log(where, f'{who} running "{shown}" after {format_duration(turn.waited or 0)} of waiting')
            code, ran, timed_out = _run_command(command, environ, timeout_sec, poll_sec, grace_sec, received, shown)
            _log(where, f"{who} timed out after {format_duration(ran)}" if timed_out
                 else f"{who} done after {format_duration(ran)}, exit {code}")
            return code
        finally:
            remove_holder(where.runs_dir, pid)
    finally:
        os.close(fd)
```

In `herdr_review/cli.py`, change `EXCLUSIVE_USAGE` to:

```python
EXCLUSIVE_USAGE = "herdr-review exclusive [--wait SEC] [--timeout SEC] -- COMMAND [ARG...]"
```

In the `exclusive` parser, after the `--wait` option, add:

```python
    p.add_argument("--timeout", type=seconds(zero_ok=False), default=exclusive.DEFAULT_TIMEOUT_SEC, metavar="SEC",
                   help="stop the command, with everything it started, after this many seconds and exit 124 (default 1800)")
```

and in `cmd_exclusive` the call becomes:

```python
        return exclusive.run(command, wait_sec=args.wait, timeout_sec=args.timeout, environ=environ, cwd=Path.cwd(),
                             poll_sec=poll_sec_from(environ, exclusive.POLL_SEC))
```

Do not start the command in a session or process group of its own (`start_new_session`, `process_group`): whatever the agent's CLI signals to cancel a command must reach both the wrapper and the command. Do not pass the queue file's descriptor to the command: `O_CLOEXEC` and `Popen`'s default `close_fds=True` keep it out.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_exclusive tests.unit.test_cli -v`
Expected: PASS (the SIGKILL test is skipped outside Linux).
Then `tests/run.sh`: every unit and bats test passes.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/exclusive.py herdr_review/cli.py tests/unit/test_exclusive.py tests/unit/test_cli.py
git commit -m "feat: exclusive stops a command past its timeout or on a signal, with everything it started"
```

---

### Task 5: `status` names who holds the build queue

**Files:**
- Modify: `herdr_review/cli.py` (`queue_line`; `cmd_status`)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `exclusive.queue_state(runs_dir) -> dict`, `exclusive.runs_dir_of(run_dir) -> Path` (Task 1); test helpers `Held`, `iso_ago`, `make_run_dir` from `tests.unit.test_exclusive`.
- Produces: `cli.queue_line(state: Mapping) -> str`; `status --json` carries `"exclusive"`: `queue_state()`'s dict.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_cli.py`, add `import os` to the imports and

```python
from tests.unit.test_exclusive import Held, iso_ago, make_run_dir
```

then append:

```python
class StatusQueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / "runs"
        self.run_dir = make_run_dir(self.runs)
        RunStatus.create(self.run_dir, run_id="hrtest", repo=str(self.root), layout="tabs")

    def status(self, *flags: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["status", "--run", str(self.run_dir), *flags]), 0)
        return out.getvalue()

    def hold(self, **holder) -> Held:
        held = Held(self.runs, **holder)
        self.addCleanup(held.release)
        return held

    def test_a_held_queue_names_the_agent_its_run_and_the_command(self):
        self.hold(pid=42, agent="hrtest-codex", run_id="hrtest", command="mvn test", started_at=iso_ago(185))
        self.assertIn("очередь сборок: занята — hrtest-codex (прогон hrtest): mvn test, 3 мин", self.status())
        queue = json.loads(self.status("--json"))["exclusive"]
        self.assertEqual({k: queue[k] for k in ("held", "agent", "run_id", "pid", "command")},
                         {"held": True, "agent": "hrtest-codex", "run_id": "hrtest", "pid": 42, "command": "mvn test"})

    def test_a_holder_outside_a_review(self):
        self.hold(pid=42, agent=None, run_id=None, command="make", started_at=iso_ago(5))
        self.assertRegex(self.status(), r"очередь сборок: занята — pid 42 вне ревью: make, [5-9] с")

    def test_a_free_queue(self):
        self.assertIn("очередь сборок: свободна", self.status())
        self.assertEqual(json.loads(self.status("--json"))["exclusive"], {"held": False})

    def test_a_holder_file_left_by_a_dead_wrapper_is_not_believed(self):
        self.hold(pid=42, agent="hrtest-codex", run_id="hrtest", command="mvn test", started_at=iso_ago(5)).release()
        self.assertIn("очередь сборок: свободна", self.status())

    def test_a_held_queue_without_a_holder_file(self):   # Review Focus 3
        self.hold()
        self.assertIn("очередь сборок: занята — процессом, который себя не назвал", self.status())

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_a_queue_file_it_cannot_read(self):
        self.hold().release()
        (self.runs / "exclusive.lock").chmod(0)
        self.assertIn("очередь сборок: не удалось проверить (", self.status())
        self.assertIsNone(json.loads(self.status("--json"))["exclusive"]["held"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_cli -v`
Expected: `StatusQueueTest` fails — no «очередь сборок» line, `KeyError: 'exclusive'` in the JSON.

- [ ] **Step 3: Implement**

In `herdr_review/cli.py`, before `cmd_status`, add:

```python
def queue_line(state: Mapping) -> str:
    """The machine's build queue, in `status` text."""
    held = state.get("held")
    if held is None:
        return f"очередь сборок: не удалось проверить ({state.get('error')})"
    if not held:
        return "очередь сборок: свободна"
    pid = state.get("pid")
    if pid is None:
        return "очередь сборок: занята — процессом, который себя не назвал"
    if state.get("run_id"):
        name = state.get("agent") or f"pid {pid}"
        who = f"{name} (прогон {state['run_id']})"
    else:
        who = f"pid {pid} вне ревью"
    line = f"очередь сборок: занята — {who}: {state.get('command')}"
    since = state.get("since_sec")
    if since is not None:
        line += f", {since} с" if since < 60 else f", {since // 60} мин"
    return line
```

In `cmd_status`, right after `data = st.data`, add:

```python
    queue = exclusive.queue_state(exclusive.runs_dir_of(run_dir))
```

in the `--json` branch, after `out["run_dir"] = str(run_dir)`, add:

```python
        out["exclusive"] = queue
```

and right after the `drift:` print (the `if data.get("drift"):` block), add:

```python
    print(queue_line(queue))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_cli -v`
Expected: PASS. Then `tests/run.sh`: every unit and bats test passes.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/cli.py tests/unit/test_cli.py
git commit -m "feat: status names who holds the build queue"
```

---

### Task 6: `run fail`, `run finish` and `close` stop their run's command in the build queue

**Files:**
- Modify: `herdr_review/exclusive.py` (`is_wrapper`, `stop_holder`)
- Modify: `herdr_review/runner.py` (`_stop_queue_holder`; `fail`; `finish`; `close`)
- Modify: `herdr_review/cli.py` (`cmd_close` text)
- Create: `tests/unit/test_runner_queue.py`

**Interfaces:**
- Consumes: `queue_state`, `format_duration`, `STOP_WAIT_SEC` (Task 1); a wrapper passes SIGTERM to its command and releases the queue (Task 4); `RunnerBase`, `make_run` from `tests.unit.test_runner_start`; `Held`, `clean_env`, `exclusive_cmd`, `iso_ago`, `stop_quietly`, `wait_until` from `tests.unit.test_exclusive`.
- Produces: `exclusive.is_wrapper(pid: int) -> bool`; `exclusive.stop_holder(runs_dir: Path, run_id: str, agent: str | None = None, *, wait_sec: float = STOP_WAIT_SEC, clock=time.monotonic, sleep=time.sleep, log=lambda line: None) -> str | None`; `Runner._stop_queue_holder(agent: str | None = None) -> str | None`; `"exclusive_stopped": "<agent>: <command>"` in the JSON of `run fail`, `run finish` and `close` when a stop happened.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_runner_queue.py`:

```python
import os
import signal
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from herdr_review.exclusive import queue_state, stop_holder
from tests.unit.test_exclusive import Held, clean_env, exclusive_cmd, iso_ago, stop_quietly, wait_until
from tests.unit.test_runner_start import RunnerBase, make_run


class RunnerStopsItsQueueHolderTest(RunnerBase):
    def setUp(self):
        super().setUp()
        self.runs = self.root / "runs"          # make_run puts a run in <root>/runs/repo/<run>

    def holding(self, run_dir: Path, agent: str) -> subprocess.Popen:
        """A real wrapper of <agent> in the run of <run_dir>, holding the queue with `sleep 30`."""
        p = subprocess.Popen(exclusive_cmd("--", "sleep", "30"), env=clean_env(HERDR_REVIEW_RUN=str(run_dir), HERDR_REVIEW_AGENT=agent),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(stop_quietly, p)
        self.assertTrue(wait_until(lambda: queue_state(self.runs).get("pid") == p.pid))
        return p

    def test_run_fail_stops_the_failed_agents_command(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = self.runner(run_dir)
        r.start_reviewers()
        p = self.holding(run_dir, "hrtest-codex")
        out = r.fail("hrtest-codex", "stuck")
        self.assertEqual(out["exclusive_stopped"], "hrtest-codex: sleep 30")
        self.assertEqual(p.wait(timeout=30), 143)
        self.assertEqual(queue_state(self.runs), {"held": False})
        self.assertIn("exclusive: stopped hrtest-codex: sleep 30", (run_dir / "runner.log").read_text())

    def test_run_fail_leaves_another_agents_command_alone(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex", "gemini"))
        r = self.runner(run_dir)
        r.start_reviewers()
        p = self.holding(run_dir, "hrtest-gemini")
        self.assertNotIn("exclusive_stopped", r.fail("hrtest-codex", "stuck"))
        self.assertIsNone(p.poll())

    def test_finish_stops_a_command_of_its_run(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = self.runner(run_dir)
        r.start_reviewers()
        p = self.holding(run_dir, "hrtest-codex")
        self.assertEqual(r.finish([])["exclusive_stopped"], "hrtest-codex: sleep 30")
        self.assertEqual(p.wait(timeout=30), 143)

    def test_close_stops_a_command_of_its_run(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = self.runner(run_dir)
        r.start_reviewers()
        r.start_fixer()
        r.finish([])
        p = self.holding(run_dir, "hrtest-fixer")
        self.assertEqual(self.runner(run_dir).close()["exclusive_stopped"], "hrtest-fixer: sleep 30")
        self.assertEqual(p.wait(timeout=30), 143)

    def test_a_command_of_another_run_is_left_alone(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        other = self.runs / "other" / "20260908-110000-hrother"
        other.mkdir(parents=True)
        (other / "run.json").write_text('{"run_id": "hrother"}')
        p = self.holding(other, "hrother-codex")
        self.assertNotIn("exclusive_stopped", self.runner(run_dir).finish([]))
        self.assertIsNone(p.poll())

    def test_a_holder_file_that_names_no_wrapper_is_left_alone(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        held = Held(self.runs, pid=os.getpid(), agent="hrtest-codex", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        self.addCleanup(held.release)
        self.assertNotIn("exclusive_stopped", self.runner(run_dir).finish([]))     # SIGTERM would hit this very test
        self.assertIn("which does not run herdr-review exclusive; left alone", (run_dir / "runner.log").read_text())

    def test_a_free_queue_stops_nothing(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.assertNotIn("exclusive_stopped", self.runner(run_dir).finish([]))


class StopHolderTest(unittest.TestCase):
    def test_a_holder_that_outlives_the_wait_is_reported_and_left_to_its_timeout(self):
        state = {"held": True, "agent": "hrtest-codex", "run_id": "hrtest", "pid": 99999, "command": "mvn test", "since_sec": 5}
        now = [0.0]
        lines: list[str] = []
        with mock.patch("herdr_review.exclusive.queue_state", return_value=state), \
                mock.patch("herdr_review.exclusive.is_wrapper", return_value=True), \
                mock.patch("herdr_review.exclusive.os.kill") as kill:
            out = stop_holder(Path("/nowhere"), "hrtest", clock=lambda: now[0],
                              sleep=lambda s: now.__setitem__(0, now[0] + s), log=lines.append)
        kill.assert_called_once_with(99999, signal.SIGTERM)
        self.assertEqual(out, "hrtest-codex: mvn test (still running after 15s)")
        self.assertIn("left to its --timeout", lines[-1])

    def test_a_free_queue_or_another_run_is_left_alone(self):
        for state in ({"held": False}, {"held": True, "run_id": "hrother", "agent": "x", "pid": 1, "command": "c"}):
            with self.subTest(state=state), mock.patch("herdr_review.exclusive.queue_state", return_value=state), \
                    mock.patch("herdr_review.exclusive.os.kill") as kill:
                self.assertIsNone(stop_holder(Path("/nowhere"), "hrtest"))
                kill.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_queue -v`
Expected: ERROR — `ImportError: cannot import name 'stop_holder' from 'herdr_review.exclusive'`.

- [ ] **Step 3: Implement the stop**

Append to `herdr_review/exclusive.py`:

```python
def is_wrapper(pid: int) -> bool:
    """Whether <pid> runs `herdr-review exclusive`: the holder file is a hint, and a pid is reused."""
    if Path("/proc/self/cmdline").exists():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        words = [w.decode("utf-8", "replace") for w in raw.split(b"\0") if w]
    else:
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        words = out.stdout.split()
    return "exclusive" in words and any("herdr-review" in w or "herdr_review" in w for w in words)


def stop_holder(runs_dir: Path, run_id: str, agent: str | None = None, *, wait_sec: float = STOP_WAIT_SEC,
                clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                log: Callable[[str], None] = lambda line: None) -> str | None:
    """Stop the wrapper that holds the queue for run <run_id> (for <agent>, when given) with SIGTERM, which it passes
    to its command before it releases the queue. "<agent>: <command>" when one was stopped, with a note when the
    queue is still held <wait_sec> later; None when the queue is free or held by someone else. Never SIGKILL: that
    would orphan the command without the lock, and the wrapper's --timeout still bounds it."""
    state = queue_state(runs_dir)
    if state.get("held") is not True or state.get("run_id") != run_id:
        return None
    if agent is not None and state.get("agent") != agent:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or not is_wrapper(pid):
        log(f"exclusive: the holder file names pid {pid}, which does not run herdr-review exclusive; left alone")
        return None
    name = state.get("agent") or f"pid {pid}"
    what = f"{name}: {state.get('command')}"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError as e:
        log(f"exclusive: cannot stop {what}: {e}")
        return None
    deadline = clock() + wait_sec
    while clock() < deadline:
        now_state = queue_state(runs_dir)
        if now_state.get("held") is not True or now_state.get("pid") != pid:
            log(f"exclusive: stopped {what}")
            return what
        sleep(0.2)
    log(f"exclusive: {what} still holds the queue {format_duration(wait_sec)} after SIGTERM; left to its --timeout")
    return f"{what} (still running after {format_duration(wait_sec)})"
```

- [ ] **Step 4: Call it from the runner and print it in `close`**

In `herdr_review/runner.py`, add before the `# ----- finish` section:

```python
    # ----- the build queue
    def _stop_queue_holder(self, agent: str | None = None) -> str | None:
        """Stop a heavy command of this run (of <agent>, when given) that still holds the machine's build queue: a
        failed reviewer's build, or a background command a CLI kept alive, would hold it up to its --timeout."""
        return exclusive.stop_holder(exclusive.runs_dir_of(self.run_dir), self.run_id, agent,
                                     clock=self.clock, sleep=self.sleep, log=self.log)
```

Replace `fail` with:

```python
    def fail(self, name: str, reason: str) -> dict:
        self._agent(name)
        self._set_state(name, "failed", reason=reason, last_screen=self._last_screen(name))
        out = {"name": name, "state": "failed"}
        stopped = self._stop_queue_holder(agent=name)
        if stopped:
            out["exclusive_stopped"] = stopped
        return out
```

In `finish`, right before `closed: list[str] = []`, add:

```python
        stopped = self._stop_queue_holder()          # before scratch/ goes: the command may run there
```

and replace its last line, `return {"phase": "finished", "commits": data["commits"], "closed": closed}`, with:

```python
        result = {"phase": "finished", "commits": data["commits"], "closed": closed}
        if stopped:
            result["exclusive_stopped"] = stopped
        return result
```

In `close`, right after the `raise RunnerError(f"run {self.run_id} is still in phase {phase}; …")` check, add:

```python
        stopped = self._stop_queue_holder()          # a CLI's background command can outlive its tab
```

and replace its last line, `return {"closed": closed, "already_closed": gone, "left_open": left_open, "failed": failed}`, with:

```python
        result = {"closed": closed, "already_closed": gone, "left_open": left_open, "failed": failed}
        if stopped:
            result["exclusive_stopped"] = stopped
        return result
```

In `herdr_review/cli.py`, `cmd_close`, after the `оставлены открытыми` print, add:

```python
        if result.get("exclusive_stopped"):
            print(f"остановлена команда из очереди сборок: {result['exclusive_stopped']}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_runner_queue tests.unit.test_runner_close tests.unit.test_runner_collect -v`
Expected: PASS — the existing `close` and `finish` tests keep their exact dicts, since `exclusive_stopped` appears only after a stop.
Then `tests/run.sh`: every unit and bats test passes.

- [ ] **Step 6: Commit**

```bash
git add herdr_review/exclusive.py herdr_review/runner.py herdr_review/cli.py tests/unit/test_runner_queue.py
git commit -m "feat: run fail, finish and close stop their run's command in the build queue"
```

---

### Task 7: The reviewer and the fixer run heavy commands through `exclusive`; the orchestrator judges them

**Files:**
- Modify: `herdr_review/__init__.py` (`RUNNER_PATH`)
- Modify: `herdr_review/cli.py` (imports `RUNNER_PATH` instead of defining it)
- Modify: `herdr_review/scope.py` (`exclusive_rules`; `fixer_skeleton` takes the runner)
- Modify: `herdr_review/launch.py` (renders the rules into the reviewer prompts and the fixer skeletons)
- Create: `prompts/exclusive.md`
- Modify: `prompts/reviewer.md`, `prompts/fixer-auto.md`, `prompts/fixer-decision.md`, `prompts/orchestrator.md`
- Test: `tests/unit/test_prompts.py`, `tests/unit/test_launch.py`

**Interfaces:**
- Consumes: the CLI `herdr-review exclusive` (Tasks 3–4), its exit codes 75, 124 and 1, `status` naming the holder (Task 5).
- Produces: `herdr_review.RUNNER_PATH`; `scope.exclusive_rules(runner: Path | str) -> str`; `scope.fixer_skeleton(kind: str, scope: str, run_dir: Path | str, runner: Path | str = RUNNER_PATH) -> str`; placeholder `{EXCLUSIVE_RULES}` in `reviewer.md`, `fixer-auto.md` and `fixer-decision.md`; `{RUNNER}` in `exclusive.md`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_prompts.py`, change the scope import to

```python
from herdr_review.scope import exclusive_rules, fixer_skeleton, reviewer_steps
```

and in `EXPECTED` replace the three entries and add the new template:

```python
    "reviewer.md": {"DESCRIPTION", "PLAN_REFERENCE", "REPO", "BASE_REF", "MERGE_BASE", "RESULT_PATH", "REVIEWER", "SCOPE_STEPS", "SCRATCH_DIR", "EXCLUSIVE_RULES"},
    "fixer-auto.md": {"RUN_DIR", "COMMIT_RULES", "EXCLUSIVE_RULES"},
    "fixer-decision.md": {"RUN_DIR", "COMMIT_RULES", "EXCLUSIVE_RULES"},
    "exclusive.md": {"RUNNER"},
```

Add to `PromptTemplatesTest`:

```python
    def test_the_heavy_command_rules_reach_the_reviewer_and_the_fixer(self):
        runner = "/opt/hr/bin/herdr-review"
        values = {k: "v" for k in EXPECTED["reviewer.md"]}
        values["EXCLUSIVE_RULES"] = exclusive_rules(runner)
        reviewer = render_file(PROMPTS_DIR / "reviewer.md", values)
        texts = {"reviewer": reviewer}
        for kind in ("auto", "decision"):
            for scope in ("commits", "worktree"):
                texts[f"fixer-{kind} ({scope})"] = fixer_skeleton(kind, scope, "/run", runner)
        for name, text in texts.items():
            with self.subTest(name=name):
                self.assertIn('Run each one through `"/opt/hr/bin/herdr-review" exclusive -- <command> [args…]`', text)
                self.assertIn("`\"/opt/hr/bin/herdr-review\" exclusive -- sh -c 'npm ci && npm test'`", text)
                self.assertIn("a commit whose hooks build or test", text)
                self.assertIn("Exit code 75 with a `herdr-review exclusive: busy` line means the command did not run.", text)
                self.assertIn("never in a shell loop", text)
                self.assertIn("pass `--timeout <seconds>` before the `--`", text)
                self.assertIn("No server, container or watcher may outlive the call.", text)
                self.assertIn("Never run the command without it.", text)
                self.assertNotIn("{", text)
        self.assertIn("## Heavy Commands", reviewer)
        self.assertIn("running the project's own tests — through the wrapper that Heavy Commands below describes — are fine", reviewer)
        self.assertIn("Do not give up a check you need because the queue is busy.", reviewer)
        for kind in ("auto", "decision"):
            text = texts[f"fixer-{kind} (commits)"]
            self.assertIn("## Heavy commands", text)
            self.assertIn("run them as the section Heavy commands below says", text)
            self.assertIn("Never skip the tests because the queue is busy", text)

    def test_the_orchestrator_judges_a_wrapped_command_by_the_command_and_refuses_an_unwrapped_heavy_one(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(RUNNER="/opt/hr/bin/herdr-review", RUN_DIR="/run")
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            'a command run through `"/opt/hr/bin/herdr-review" exclusive -- <command>` → judge `<command>` by the rules below',
            "the wrapper itself writes only its queue files in the runs directory and the run's `runner.log`",
            'Run builds, tests, dependency installs, servers and containers through \\"/opt/hr/bin/herdr-review\\" exclusive -- <command>, as your prompt says.',
            "the project's own tests or build run through the wrapper",
            'so is `herdr-review exclusive` waiting for its turn or running its command; `"/opt/hr/bin/herdr-review" status --run "/run"` names who holds the build queue',
            '| Confirming a heavy command that a reviewer or the fixer runs without `"/opt/hr/bin/herdr-review" exclusive` | Refuse; point the agent at the wrapper. |',
        ):
            self.assertIn(phrase, text)
```

In `tests/unit/test_launch.py`, add to `LaunchTest`:

```python
    def test_the_reviewers_and_the_fixer_run_heavy_commands_through_this_runner(self):
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        wrapped = f'"{self.runner}" exclusive -- <command> [args…]'
        self.assertIn(wrapped, (run_dir / "prompts" / "codex.md").read_text())
        self.assertEqual((run_dir / "orchestrator.md").read_text().count(wrapped), 2)    # both fixer skeletons
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_prompts tests.unit.test_launch -v`
Expected: ERROR — `ImportError: cannot import name 'exclusive_rules' from 'herdr_review.scope'`.

- [ ] **Step 3: The runner's path moves into the package**

In `herdr_review/__init__.py`, after `PROMPTS_DIR = …`, add:

```python
RUNNER_PATH = PACKAGE_ROOT / "bin" / "herdr-review"
```

In `herdr_review/cli.py`, delete the line `RUNNER_PATH = PACKAGE_ROOT / "bin" / "herdr-review"` and change the package import to:

```python
from . import RUNNER_PATH, __version__, exclusive, gitutil
```

(`grep -n PACKAGE_ROOT herdr_review/cli.py` must print nothing afterwards; if it prints a use, keep `PACKAGE_ROOT` in that import.)

- [ ] **Step 4: The shared template and the prompts**

Create `prompts/exclusive.md`:

```markdown
Heavy commands share this machine with every agent of every review running on it. Run each one through `"{RUNNER}" exclusive -- <command> [args…]`, which runs one heavy command at a time. A command is heavy when it runs the project's build tool, test runner or package manager (even for one test), installs or updates dependencies, starts a server, a container, a database or anything else that listens on a port, or does any of this indirectly: `make`, a project script, a commit whose hooks build or test. When unsure, treat it as heavy. Reading files, searching, `git diff`, `git log`, `git show`, `git status` and `git clone` are not heavy.

- Put steps that must run back to back into one call: `"{RUNNER}" exclusive -- sh -c 'npm ci && npm test'`. The command gets no input: its stdin is empty.
- The wrapper waits up to 60 s for its turn. Exit code 75 with a `herdr-review exclusive: busy` line means the command did not run. Run the same call again later yourself, never in a shell loop (`until …; do sleep …; done`): such a loop holds one tool call for as long as the queue stays busy, and it never ends when the command itself fails.
- A command still running after 30 minutes is stopped: exit code 124 with a `herdr-review exclusive: timed out` line. For a build you know takes longer, pass `--timeout <seconds>` before the `--`.
- Stop everything you start inside the same call: `sh -c 'docker compose up -d && …; docker compose down'`. No server, container or watcher may outlive the call.
- Exit code 1 with a `herdr-review exclusive:` error line means the wrapper itself failed. Never run the command without it.
```

`prompts/reviewer.md` — in the first Hard Rule, replace

```
Reading, `git diff`, `git log`, and running the project's own tests are fine as long as they write only into gitignored paths.
```

with

```
Reading, `git diff`, `git log`, and running the project's own tests — through the wrapper that Heavy Commands below describes — are fine as long as they write only into gitignored paths.
```

and insert before `## Review Focus`:

```markdown
## Heavy Commands

{EXCLUSIVE_RULES}

While you wait for your turn, do other review work: read code, draft findings. Do not give up a check you need because the queue is busy. If the wrapper failed, or you still could not run a check when the rest of the review is done, say so in the review.

```

`prompts/fixer-auto.md` — replace rule 2 with

```
2. If the project has tests relevant to the changed code, run them as the section Heavy commands below says. Fix a failure only if your change caused it.
```

and insert before `## Committing`:

```markdown
## Heavy commands

{EXCLUSIVE_RULES}

While you wait for your turn, apply your next fix, or wait a minute and run the call again. Never skip the tests because the queue is busy; if the wrapper failed, say in your report that the tests did not run, and why.

```

`prompts/fixer-decision.md` — the same rule 2 replacement, and insert before `## Committing`:

```markdown
## Heavy commands

{EXCLUSIVE_RULES}

While you wait for your turn, wait a minute and run the call again. Never skip the tests because the queue is busy; if the wrapper failed, say in your report that the tests did not run, and why.

```

`prompts/orchestrator.md`, Phase 2, the `blocked` dialog rules — insert this sub-bullet directly above the one that starts `       - a reviewer: a read, a command that only reads,`:

```
       - a command run through `"{RUNNER}" exclusive -- <command>` → judge `<command>` by the rules below; the wrapper itself writes only its queue files in the runs directory and the run's `runner.log`. A heavy command — a build, tests, a dependency install, a server or a container — that a reviewer or the fixer runs without the wrapper → refuse with the dialog's own "no" option, then `herdr agent prompt <name> "Run builds, tests, dependency installs, servers and containers through \"{RUNNER}\" exclusive -- <command>, as your prompt says."`;
```

in the `a reviewer:` sub-bullet, replace `the project's own tests or build, or a write under` with `the project's own tests or build run through the wrapper, or a write under`;

in the stuck-agent rule, replace `A long tool call or a thinking indicator is fine: keep waiting.` with:

```
A long tool call or a thinking indicator is fine: keep waiting — so is `herdr-review exclusive` waiting for its turn or running its command; `"{RUNNER}" status --run "{RUN_DIR}"` names who holds the build queue.
```

and in the Red flags table, after the row that starts `| Confirming a reviewer's write into the repository`, add:

```
| Confirming a heavy command that a reviewer or the fixer runs without `"{RUNNER}" exclusive` | Refuse; point the agent at the wrapper. |
```

- [ ] **Step 5: Render the rules**

In `herdr_review/scope.py`, change the package import to `from . import PROMPTS_DIR, RUNNER_PATH`, add before `fixer_skeleton`:

```python
def exclusive_rules(runner: Path | str) -> str:
    """The rules for heavy commands that the reviewer and the fixer prompts share: run them through <runner>'s
    `exclusive`, one at a time on this machine."""
    return render_file(PROMPTS_DIR / "exclusive.md", {"RUNNER": str(runner)}).strip()
```

and replace `fixer_skeleton` with:

```python
def fixer_skeleton(kind: str, scope: str, run_dir: Path | str, runner: Path | str = RUNNER_PATH) -> str:
    """The fixer's task skeleton, `auto` or `decision`, with the commit rules of <scope> and the heavy-command
    rules of <runner>. In scope `worktree` the change is uncommitted work and the fixer commits nothing: the rule
    reaches it in the skeleton itself, not through the orchestrator's memory."""
    values = {"RUN_DIR": str(run_dir)}
    rules = f"fixer-commit-{kind}.md" if scope == "commits" else "fixer-commit-none.md"
    return render_file(PROMPTS_DIR / f"fixer-{kind}.md", {
        **values, "COMMIT_RULES": render_file(PROMPTS_DIR / rules, values).strip(),
        "EXCLUSIVE_RULES": exclusive_rules(runner),
    })
```

In `herdr_review/launch.py`, change the scope import to:

```python
from .scope import ScopeError, exclusive_rules, fixer_skeleton, orchestrator_scope, resolve_scope, reviewer_steps, untracked_line
```

after `steps = reviewer_steps(…)` add:

```python
        heavy = exclusive_rules(runner_path)
```

add `"EXCLUSIVE_RULES": heavy,` to the values of `render_file(PROMPTS_DIR / "reviewer.md", {…})`, and change the two skeleton lines of the orchestrator's values to:

```python
            "FIXER_AUTO_SKELETON": fixer_skeleton("auto", scope, run_dir, runner_path),
            "FIXER_DECISION_SKELETON": fixer_skeleton("decision", scope, run_dir, runner_path),
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_prompts tests.unit.test_launch -v`
Expected: PASS — including the existing `test_in_the_worktree_scope_the_fixer_commits_nothing` (the rules say "a commit whose hooks", never `git commit`) and the orchestrator's no-leftover-`{` check.
Then `tests/run.sh`: every unit and bats test passes.

- [ ] **Step 7: Commit**

```bash
git add herdr_review/__init__.py herdr_review/cli.py herdr_review/scope.py herdr_review/launch.py prompts/exclusive.md prompts/reviewer.md prompts/fixer-auto.md prompts/fixer-decision.md prompts/orchestrator.md tests/unit/test_prompts.py tests/unit/test_launch.py
git commit -m "feat: the reviewer and the fixer run heavy commands through exclusive"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` ("The CLI", "During the run", "Troubleshooting")
- Modify: `CHANGELOG.md`
- Modify: `config.example.yaml`
- Modify: `tests/SMOKE.md`

**Interfaces:**
- Consumes: the behaviour of Tasks 1–7, as the spec describes it.
- Produces: documentation only.

- [ ] **Step 1: README — "The CLI"**

In the code block of `### The CLI`, after the line `herdr-review profiles               # the validated config, secrets omitted`, add:

```bash
herdr-review exclusive -- ./gradlew build   # your own heavy command, in turn with the reviews on this machine
```

After the paragraph that starts `` `launch --help` lists every flag``, add a paragraph:

```markdown
`herdr-review exclusive -- <command>` runs a heavy command — a build, tests, a dependency install, a server — only while no other heavy command runs on this machine: it holds an exclusive lock on `<runs_dir>/exclusive.lock`, the queue that the reviewers and the fixer of every review share. It waits up to 60 s for its turn (`--wait SEC`; `0` tries once) and exits 75 without running the command when the turn does not come; a command still running after 30 minutes (`--timeout SEC`) is stopped, with everything it started, and the wrapper exits 124. Otherwise it exits with the command's own code. Run your own build through it during a review, and it takes its turn with the agents.
```

- [ ] **Step 2: README — "During the run"**

After the `- **Experiments:** …` bullet, add:

```markdown
- **Builds and tests:** the reviewers share your working tree and your machine, so their heavy commands — builds, tests, dependency installs, servers, containers — take turns: each goes through `herdr-review exclusive`, one at a time among all reviews on this machine, and a reviewer reads code while it waits. The fixer runs its tests the same way. `herdr-review status` shows who holds the queue (`очередь сборок: занята — …`). `run fail` stops a command of the agent it takes out of the run, and `run finish` and `close` stop any command of their run that still holds the queue.
```

- [ ] **Step 3: README — "Troubleshooting"**

After the bullet `- A reviewer or the fixer shows ` ❓` — …`, add:

```markdown
- A reviewer stays ` ⏳` a long time and its tab shows `herdr-review exclusive: waiting` — another heavy command holds the build queue; `herdr-review status` names it and says how long it has run. A command stops by itself after 30 minutes unless its caller asked for more with `--timeout`.
- `herdr-review exclusive` exits 75 — its turn did not come within `--wait`, and the command did not run; run it again later. Exit 124 — the command ran past `--timeout` and was stopped.
```

- [ ] **Step 4: CHANGELOG**

In `CHANGELOG.md`, insert before `## [0.2.0] - 2026-09-24`:

```markdown
## [Unreleased]

### Added
- `herdr-review exclusive -- <command>`: runs a heavy command — a build, tests, a dependency install, a server — only
  while it holds the machine's build queue (`<runs_dir>/exclusive.lock`), one at a time among every review on the
  machine. It waits up to 60 s for its turn (`--wait`) and exits 75 without running the command when the turn does
  not come; a command still running after 30 minutes (`--timeout`) is stopped, with everything it started, and the
  wrapper exits 124. The reviewer and fixer prompts require it for every heavy command, and the orchestrator refuses
  a heavy command run without it when an agent's CLI asks.
- `herdr-review status` names who holds the build queue (`exclusive` in `--json`).

### Changed
- `run fail`, `run finish` and `close` stop a command of their run that still holds the build queue.
- Every agent starts with `GIT_OPTIONAL_LOCKS=0`, so its `git status` and `git diff` no longer take
  `.git/index.lock` from under the owner's `git commit`, and with `HERDR_REVIEW_AGENT` naming it.
- `run.json` records `runs_dir`.

```

- [ ] **Step 5: config.example.yaml**

Directly above the line `  runs_dir: ~/.local/state/herdr-review/runs   # keep the --add-dir values …`, add:

```yaml
  # runs_dir also holds the machine's build queue (exclusive.lock, exclusive.json): every review that uses this
  # runs_dir, and your own `herdr-review exclusive` calls, run their heavy commands there one at a time.
```

- [ ] **Step 6: SMOKE**

Append to `tests/SMOKE.md`:

```markdown
12. Build queue: preset `default` (two reviewers) on a repository whose test suite takes a minute or more. Both reviewers run the tests through `herdr-review exclusive`: while one runs, `bin/herdr-review status --run latest` shows `очередь сборок: занята — <agent> (прогон <id>): …`, and the other reviewer's tab shows `herdr-review exclusive: waiting` or a retry after exit code 75. `runner.log` has their `exclusive:` lines, and no `running` line of one comes between the `running` and `done` lines of the other.
```

- [ ] **Step 7: Check and commit**

Run: `grep -n "exclusive" README.md CHANGELOG.md config.example.yaml tests/SMOKE.md`
Expected: the new lines above, and nothing that contradicts the spec (defaults 60 s and 30 minutes, codes 75 and 124).
Run: `tests/run.sh`
Expected: every unit and bats test passes.

```bash
git add README.md CHANGELOG.md config.example.yaml tests/SMOKE.md
git commit -m "docs: the build queue"
```

---

## Finishing the branch

Per the owner's rule: before the pull request, `git rm` the spec and this plan (`docs/superpowers/specs/2026-09-25-build-queue-design.md`, `docs/superpowers/plans/2026-09-25-build-queue.md`) and commit, so that neither appears in the PR diff; they stay in the branch history. Leave the owner's other untracked files under `docs/` alone.
