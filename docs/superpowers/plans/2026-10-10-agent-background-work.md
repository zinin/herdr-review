# Agents busy with background work — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reviewer or the fixer that herdr reports `idle`/`done` while it waits for its own background work — a
command in the build queue, or a grok background task — stays `working`, and `collect` neither re-prompts nor fails it.

**Architecture:** `herdr-review exclusive` registers every wrapper of a review in `<run_dir>/wrappers/<pid>.json`.
The runner's `_observe` asks `_background_work` whether an agent herdr reports idle or done still waits for such work:
a live wrapper of that agent (every kind), or grok's still-running status line at the bottom of its screen (kind
`grok`, trusted alone for 30 minutes at most), or a sign of either less than 30 s ago. A busy agent stays `working`
and its new `background` field names the work. `collect` looks at an idle or done reviewer again before it prompts or
fails it. The prompts tell the agents to wait for their commands and the orchestrator to leave a busy agent alone.

**Tech Stack:** Python ≥ 3.11 standard library (PyYAML is the only dependency, unchanged); `unittest`; `bats`.

**Spec:** `docs/superpowers/specs/2026-10-10-agent-background-work-design.md` — authoritative on behaviour; this
plan is authoritative on the code. Read both before you start.

## Global Constraints

- Code, comments, prompts, commit messages and docs are in English. The owner is addressed in Russian.
- No new dependency. The entry point (`bin/herdr-review`) supports Python 3.11; use no newer syntax.
- Exact values: `BUSY_GRACE_SEC = 30`, `SCREEN_BUSY_LIMIT_SEC = 1800`, `GROK_STATUS_LINES = 20`.
- Registry: `<run_dir>/wrappers/<pid>.json`, file mode 0600, directory mode 0700, written atomically.
- New agent fields in `status.json`: `background`, `busy_seen_at`, `screen_busy_since`, `screen_busy_logged`; all read
  with `.get` so runs started before this change keep working.
- No new agent state. `STATE_CLASS`, `LABEL_SUFFIX` and the semantics of `collect_retries` stay as they are.
- Stage explicit paths only; never `git add -A` or `git add .`. The owner's untracked files stay untracked:
  `docs/2026-09-23-launcher-without-questions-prompt.md`, `docs/2026-09-24-launcher-without-questions-deferred.md`,
  `docs/2026-10-10-grok-dialogs-and-background-tasks.md` (Task 6 edits it in place, it stays untracked) and the older
  files in `docs/superpowers/plans/`.
- Push, pull request and release only when the owner asks. Before the pull request, `git rm` the spec and this plan
  and commit (the owner's rule: plan documents never appear in a PR diff).
- Do not file the herdr issues of the spec's appendix: the owner decides.
- Tests: `python3 -m unittest discover -s tests/unit -t .` for the unit tests, `bats tests/bats` for the bats tests;
  `tests/run.sh` runs both. At `d373b80` (the design commit) 584 unit tests and 37 bats tests pass; after Task 6,
  619 unit tests and 37 bats tests pass.

## Review Focus

The inputs and conditions the spec implies that a reader of the tests could miss; each has its test in the task that
owns the code.

1. **The registry cannot be written** (a file where `wrappers/` belongs, a read-only run directory): the command still
   runs with its own exit code, and `runner.log` says why. — Task 1,
   `test_a_registry_it_cannot_write_does_not_stop_the_command`.
2. **A secret of the profile's env in the wrapper's command line** (`env TOKEN=… go test`): masked in `background`, in
   `status.json` and in `runner.log`. — Task 3, `test_a_secret_in_the_command_is_masked`.
3. **herdr cannot read a grok agent's screen**: the agent goes by herdr's status; no exception, no agent stuck in
   `working`. — Task 3, `test_an_unreadable_grok_screen_goes_by_herdrs_status`.
4. **ANSI escapes or another leading glyph around grok's status line**: still recognised. — Task 2,
   `test_escapes_and_another_glyph_around_the_line`.
5. **Both signals at once** (a grok agent with a live wrapper and the status line): the wrapper is named, and the
   30-minute clock of the screen signal does not run meanwhile. — Task 3,
   `test_a_live_wrapper_is_named_before_the_grok_line_and_stops_its_clock`.

## File Map

| File | Change | Responsibility |
|---|---|---|
| `herdr_review/exclusive.py` | modify | the registry: `wrapper_entry`, `live_wrappers`, registration in `run()` |
| `herdr_review/dialogs.py` | modify | `grok_background`: grok's still-running status line |
| `herdr_review/runner.py` | modify | `_background_work`, `_observe`, `background` in the outputs, `collect`'s fresh look |
| `herdr_review/cli.py` | modify | `herdr-review status` shows `фон: …` |
| `prompts/exclusive.md`, `prompts/orchestrator.md` | modify | wait for your commands; leave a busy agent alone |
| `README.md`, `CHANGELOG.md` | modify | user-facing docs |
| `tests/unit/test_exclusive.py` | modify | `WrapperRegistryTest` |
| `tests/unit/test_dialogs.py` | modify | grok screen fixtures, `GrokBackgroundTest` |
| `tests/unit/test_runner_start.py` | modify | a `grok` profile in the shared test config `RAW` |
| `tests/unit/test_runner_wait.py` | modify | `register_wrapper`, `LiveWrappers`, `BackgroundWorkTest` |
| `tests/unit/test_runner_collect.py` | modify | `CollectBackgroundTest` |
| `tests/unit/test_cli.py` | modify | the status view test |
| `tests/unit/test_prompts.py` | modify | the new prompt text |

---

### Task 1: The wrapper registry

**Files:**
- Modify: `herdr_review/exclusive.py` (constants at the top; `write_holder` at about line 134; new functions after
  `remove_holder` and after `_log`; `run()` at about line 528)
- Test: `tests/unit/test_exclusive.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `exclusive.WRAPPERS_DIR = "wrappers"`
  - `exclusive.wrapper_entry(run_dir: Path, pid: int) -> Path` — `<run_dir>/wrappers/<pid>.json`
  - `exclusive.live_wrappers(run_dir: Path, alive: Callable[[int], bool] | None = None) -> list[dict]` — the entries
    whose pid passes `alive` (default: `is_wrapper`, looked up at call time, so `mock.patch("herdr_review.exclusive.is_wrapper", …)`
    substitutes it), sorted by `started_at`.
  - Entry keys: `pid` (int), `agent` (str or None), `run_id` (str or None), `command` (str), `phase` (`"waiting"` or
    `"running"`), `started_at` (str), `running_since` (str or None).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_exclusive.py`, extend the import from `herdr_review.exclusive`:

```python
from herdr_review.exclusive import (
    ExclusiveError, Where, _finish_off, command_text, format_duration, holder_text, live_wrappers, locate, queue_state,
    read_holder, remove_holder, runs_dir_of, wrapper_entry, write_holder,
)
```

Append at the end of the file:

```python


class WrapperRegistryTest(ExclusiveBase):
    """Inside a review every wrapper registers itself in <run_dir>/wrappers/ while it waits for its turn and while it
    runs its command: the runner keeps its agent working meanwhile, whatever herdr reports."""

    def entries(self) -> list[Path]:
        return sorted((self.run_dir / "wrappers").glob("*.json"))

    def start(self, *args: str) -> subprocess.Popen:
        p = subprocess.Popen(exclusive_cmd(*args), env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True)
        self.addCleanup(stop_quietly, p)
        return p

    def test_the_command_sees_its_own_entry_as_running(self):
        seen = self.root / "seen.json"
        args = ["sh", "-c", 'cp "$1/wrappers/$PPID.json" "$2"', "sh", str(self.run_dir), str(seen)]
        p = subprocess.run(exclusive_cmd("--", *args), env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        entry = json.loads(seen.read_text())
        self.assertEqual((entry["agent"], entry["run_id"], entry["phase"]), ("hrtest-codex", "hrtest", "running"))
        self.assertEqual(entry["command"], shlex.join(args))
        self.assertIsInstance(entry["pid"], int)
        self.assertIsNotNone(entry["started_at"])
        self.assertIsNotNone(entry["running_since"])
        self.assertEqual(self.entries(), [])                     # gone once the command is done

    def test_a_wrapper_waiting_for_its_turn_is_registered_as_waiting(self):
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        p = self.start("--wait", "30", "--", "true")
        self.assertIn("waiting —", p.stderr.readline())          # the entry is written before the wait
        entry = json.loads(wrapper_entry(self.run_dir, p.pid).read_text())
        self.assertEqual((entry["phase"], entry["running_since"], entry["agent"]), ("waiting", None, "hrtest-codex"))
        self.assertEqual([e["pid"] for e in live_wrappers(self.run_dir)], [p.pid])

    def test_the_entry_and_its_directory_are_readable_by_their_owner_only(self):
        p = self.start("--", "sleep", "30")
        path = wrapper_entry(self.run_dir, p.pid)
        self.assertTrue(wait_until(path.exists))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_the_entry_is_gone_after_every_way_out(self):
        def code(*args: str) -> int:
            return subprocess.run(exclusive_cmd(*args), env=self.env, capture_output=True, timeout=60).returncode

        self.assertEqual(code("--", "true"), 0)
        self.assertEqual(self.entries(), [])
        self.assertEqual(code("--", "no-such-program-for-herdr-review"), 127)
        self.assertEqual(self.entries(), [])
        self.hold(pid=42, agent="hrtest-gemini", run_id="hrtest", command="mvn test", started_at=iso_ago(5))
        self.assertEqual(code("--wait", "0.3", "--", "true"), 75)
        self.assertEqual(self.entries(), [])
        p = self.start("--wait", "30", "--", "true")
        self.assertIn("waiting —", p.stderr.readline())
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 143)
        self.assertEqual(self.entries(), [])

    def test_no_entry_for_a_nested_call_or_outside_a_review(self):
        p = subprocess.run(exclusive_cmd("--", "true"), env={**self.env, "HERDR_REVIEW_EXCLUSIVE": "1"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = self.root / "config.yaml"
        cfg.write_text(f"profiles:\n  codex: {{kind: codex}}\nsettings: {{runs_dir: {self.runs}}}\n")
        cfg.chmod(0o600)
        p = subprocess.run(exclusive_cmd("--", "true"), env=clean_env(HERDR_REVIEW_CONFIG=str(cfg)),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(list(self.root.rglob("wrappers")), [])

    def test_a_registry_it_cannot_write_does_not_stop_the_command(self):   # Review Focus 1
        (self.run_dir / "wrappers").write_text("a file where the registry's directory belongs")
        p = subprocess.run(exclusive_cmd("--", "sh", "-c", "exit 3"), env=self.env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn(f"exclusive: hrtest-codex could not register in {self.run_dir / 'wrappers'}: ",
                      (self.run_dir / "runner.log").read_text())

    def test_live_wrappers_keeps_the_live_ones_in_start_order_and_drops_a_dead_one(self):
        registry = self.run_dir / "wrappers"
        registry.mkdir()
        def write(name: str, data) -> None:
            (registry / name).write_text(data if isinstance(data, str) else json.dumps(data))
        write("11.json", {"pid": 11, "agent": "a", "phase": "running", "started_at": "2026-10-10T10:00:01+00:00"})
        write("12.json", {"pid": 12, "agent": "b", "phase": "running", "started_at": "2026-10-10T10:00:02+00:00"})
        write("15.json", {"pid": 15, "agent": "c", "phase": "waiting", "started_at": "2026-10-10T10:00:00+00:00"})
        write("13.json", "{not json")
        write("14.json", {"pid": "14"})
        found = live_wrappers(self.run_dir, alive=lambda pid: pid != 11)
        self.assertEqual([e["pid"] for e in found], [15, 12])
        self.assertFalse((registry / "11.json").exists())          # a dead wrapper's entry is removed
        self.assertTrue((registry / "13.json").exists())           # an unreadable one is left alone
        self.assertTrue((registry / "14.json").exists())

    def test_a_run_without_a_registry_has_no_live_wrappers(self):
        self.assertEqual(live_wrappers(self.run_dir), [])
```

Notes for the implementer: `exclusive_cmd` runs `bin/herdr-review` under `sys.executable` directly, so `p.pid` is the
wrapper's own pid, and inside `sh -c` the wrapper is `$PPID`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_exclusive -v 2>&1 | tail -5`
Expected: FAIL — `ImportError: cannot import name 'live_wrappers' from 'herdr_review.exclusive'`.

- [ ] **Step 3: Implement the registry**

In `herdr_review/exclusive.py`:

1. Below `HOLDER_NAME = "exclusive.json"` add:

```python
WRAPPERS_DIR = "wrappers"
```

2. Replace the whole `write_holder` function with a shared atomic writer and a thin `write_holder`:

```python
def _write_json(path: Path, data: dict) -> None:
    """Atomically: a reader sees the old content or the new, never half of it. Readable by its owner only, as the
    queue file is: it holds a command line and directories."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_holder(runs_dir: Path, holder: dict) -> None:
    _write_json(Path(runs_dir) / HOLDER_NAME, holder)
```

3. Directly after `remove_holder` (before `def holder_state`) add:

```python
def wrapper_entry(run_dir: Path, pid: int) -> Path:
    """The entry of wrapper <pid> in the registry of the run in <run_dir> (live_wrappers)."""
    return Path(run_dir) / WRAPPERS_DIR / f"{pid}.json"


def live_wrappers(run_dir: Path, alive: Callable[[int], bool] | None = None) -> list[dict]:
    """The registered wrappers of the run in <run_dir> that still run, in the order they started: each one waits for
    its turn or runs its command. <alive> tells whether a pid runs `herdr-review exclusive`; is_wrapper, looked up at
    call time, by default. The entry of a pid that does not is removed: its wrapper died without removing it, of
    SIGKILL say. A file that cannot be read or parsed is skipped and left alone."""
    alive = alive or is_wrapper
    found = []
    for path in sorted((Path(run_dir) / WRAPPERS_DIR).glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = entry.get("pid") if isinstance(entry, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if not alive(pid):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        found.append(entry)
    return sorted(found, key=lambda e: str(e.get("started_at") or ""))
```

4. Directly after `_log` (before `def _taken_off`) add:

```python
def _write_entry(where: Where, path: Path, entry: dict, who: str) -> bool:
    """Write a wrapper's entry; False, with a line in the run's log, when it cannot. The command runs either way."""
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        _write_json(path, entry)
    except OSError as e:
        _log(where, f"{who} could not register in {path.parent}: {e}")
        return False
    return True


def _register(where: Where, entry: dict, who: str) -> Path | None:
    """Register this wrapper in its run (live_wrappers): the runner keeps an agent `working` while a wrapper of it
    waits for its turn or runs its command, whatever herdr reports. None outside a review, where no runner looks, and
    when the entry cannot be written."""
    if where.run_dir is None:
        return None
    path = wrapper_entry(where.run_dir, entry["pid"])
    return path if _write_entry(where, path, entry, who) else None


def _unregister(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
```

5. In `run()`, three edits. After `fd = _open_queue(where.runs_dir)` and before `try:`:

```python
    fd = _open_queue(where.runs_dir)
    entry = {"pid": os.getpid(), "agent": agent, "run_id": where.run_id, "command": shown, "phase": "waiting",
             "started_at": now_iso(), "running_since": None}
    entry_path = _register(where, entry, who)
    try:
```

Between the second `_taken_off` check and `pid = os.getpid()`:

```python
        if _taken_off(where, agent):                 # taken off while it waited for its turn
            return _refuse(where, who)
        if entry_path is not None:
            _write_entry(where, entry_path, {**entry, "phase": "running", "running_since": now_iso()}, who)
        pid = os.getpid()
```

And the last `finally` of `run()`:

```python
    finally:
        os.close(fd)
        _unregister(entry_path)
```

A nested call returns through `_exec_in_turn` before any of this, so it never registers.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_exclusive tests.unit.test_runner_queue tests.unit.test_cli 2>&1 | tail -3`
Expected: PASS (`OK`; `test_exclusive` alone has 68 tests).

- [ ] **Step 5: Commit**

```bash
git add herdr_review/exclusive.py tests/unit/test_exclusive.py
git commit -m "feat: register the wrappers of a review while they wait and run"
```

---

### Task 2: Grok's still-running status line

**Files:**
- Modify: `herdr_review/dialogs.py` (module docstring; constants after `CODEX_SESSION`; a function before `recognize`)
- Test: `tests/unit/test_dialogs.py`

**Interfaces:**
- Consumes: `dialogs._flat` (exists).
- Produces:
  - `dialogs.grok_background(screen: str) -> str | None` — the matched text, e.g. `"1 command still running"`.
  - `dialogs.GROK_STATUS_LINES = 20`, `GROK_BACKGROUND`, `GROK_WAITING`.
  - Test fixtures in `tests/unit/test_dialogs.py`, imported by Task 3: `GROK_INPUT`, `GROK_WAIT_SCREEN`,
    `GROK_QUEUED_SCREEN`, `GROK_WORKING_SCREEN`, `GROK_IDLE_SCREEN`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_dialogs.py`, add `grok_background` to the import from `herdr_review.dialogs`:

```python
from herdr_review.dialogs import MCP_REFUSAL, DialogOutcome, codex_session_ready, codex_update_complete, codex_update_running, codex_update_started, grok_background, mcp_check, recognize, resolve_startup_dialog
```

Directly after the line `CLAUDE_IDLE = "❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"` add the fixtures:

```python
# Grok 1.0.50 in run hrlwi8: the bottom of the screen, below the transcript.
GROK_INPUT = (
    "  [Click here to Upgrade] or use Ctrl+O\n"
    "\n"
    "  ╭──────────────────────────────────────────────────────╮\n"
    "  │ ❯                                                    │\n"
    "  ╰──────────── DeepSeek-V4.1-Flash (LANIT) (max) · auto-review ─╯\n"
    "\n"
    "  Shift+Tab:mode  │  Ctrl+.:shortcuts\n"
)
GROK_WAIT_SCREEN = (            # the turn waits for its background task in get_command_or_subagent_output
    "     ◆ Ran read the wave's error recording\n"
    "     ◆ Thought for 2.4s\n"
    "\n"
    "    ◉ 1 command still running · send a message to interrupt\n"
    "\n" + GROK_INPUT
)
GROK_QUEUED_SCREEN = (          # a prompt sent meanwhile waits in grok's queue
    "     ◆ Thought for 2.4s\n"
    "\n"
    "    #1 You have not written a valid review to /run/reviews/deepseek.md (file missing). Read /run/prompts/…\n"
    "\n"
    "    ○ 1 command still running · 1 queued, Enter to send now\n"
    "\n" + GROK_INPUT
)
GROK_WORKING_SCREEN = (
    "  ┃  ◆ Thinking…\n"
    "\n"
    "    ⠦ Thinking… 12s                                                       12s ⇣150k [stop]\n"
    "\n" + GROK_INPUT
)
GROK_IDLE_SCREEN = "     ◆ Wrote the review\n\n" + GROK_INPUT
```

Append at the end of the file:

```python


class GrokBackgroundTest(unittest.TestCase):
    def test_the_line_of_a_wait_and_of_a_queued_prompt(self):
        self.assertEqual(grok_background(GROK_WAIT_SCREEN), "1 command still running")
        self.assertEqual(grok_background(GROK_QUEUED_SCREEN), "1 command still running")

    def test_the_documented_forms(self):
        self.assertEqual(grok_background("◎ 1 command · 2 monitors · 1 loop · 1 subagent still running\n" + GROK_INPUT),
                         "1 command · 2 monitors · 1 loop · 1 subagent still running")
        self.assertEqual(grok_background("◎ 2 commands still running\n" + GROK_INPUT), "2 commands still running")
        self.assertEqual(grok_background("◎ waiting · send a message to interrupt\n" + GROK_INPUT),
                         "waiting · send a message to interrupt")

    def test_a_line_a_narrow_pane_wrapped(self):
        screen = "  ◉ 1 command still\n  running · send a\n  message to interrupt\n" + GROK_INPUT
        self.assertEqual(grok_background(screen), "1 command still running")

    def test_escapes_and_another_glyph_around_the_line(self):   # Review Focus 4
        screen = "    \x1b[2m●\x1b[0m \x1b[33m1 command\x1b[0m still running · send a message to interrupt\n" + GROK_INPUT
        self.assertEqual(grok_background(screen), "1 command still running")

    def test_no_background_work(self):
        for screen in (GROK_IDLE_SCREEN, GROK_WORKING_SCREEN, CLAUDE_IDLE, ""):
            with self.subTest(screen=screen):
                self.assertIsNone(grok_background(screen))
        transcript = ("     ◆ Task completed in 2m13s: Run gofmt, vet and race tests\n"
                      "     The command is still running in the background. You can continue with other tasks.\n")
        self.assertIsNone(grok_background(transcript + GROK_INPUT))

    def test_only_the_bottom_of_the_screen_counts(self):
        screen = "    ◉ 1 command still running\n" + "     ◆ Ran a command\n" * 25 + GROK_INPUT
        self.assertIsNone(grok_background(screen))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_dialogs -v 2>&1 | tail -5`
Expected: FAIL — `ImportError: cannot import name 'grok_background' from 'herdr_review.dialogs'`.

- [ ] **Step 3: Implement `grok_background`**

In `herdr_review/dialogs.py`, replace the module docstring (line 1) with:

```python
"""Agent screens the runner reads without an LLM: the startup dialogs it answers, the one it never answers,
Codex's startup update and grok's background work."""
```

After the line `CODEX_SESSION = re.compile(r"\bOpenAI Codex\s*\(v\d")` add:

```python
# Grok: the status line above its input while background work runs and the agent looks idle — between turns, or
# while a turn waits in get_command_or_subagent_output: "◉ 1 command still running · send a message to interrupt",
# "○ 1 command still running · 1 queued, Enter to send now", "◎ 1 command · 2 monitors · 1 loop · 1 subagent still
# running", "◎ waiting · send a message to interrupt". The glyph in front changes. Only the bottom of the screen is
# searched, where grok draws the line: the transcript above it may quote "… still running in the background".
GROK_STATUS_LINES = 20
GROK_BACKGROUND = re.compile(r"\b\d+ (?:command|monitor|loop|subagent)s?(?:\s*·\s*\d+ (?:command|monitor|loop|subagent)s?)*\s+still running\b")
GROK_WAITING = re.compile(r"\bwaiting\s*·\s*send a message to interrupt\b")
```

Before `def recognize(screen: str)` add:

```python
def grok_background(screen: str) -> str | None:
    """The background work grok's status line names at the bottom of <screen>, in the line's own words (`1 command
    still running`, `waiting · send a message to interrupt`); None when the line is not there."""
    bottom = _flat("\n".join(screen.splitlines()[-GROK_STATUS_LINES:]))
    found = GROK_BACKGROUND.search(bottom) or GROK_WAITING.search(bottom)
    return found.group(0) if found else None
```

`_flat` already drops ANSI escapes and box-drawing characters and joins wrapped lines with single spaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_dialogs 2>&1 | tail -3`
Expected: PASS (`OK`).

- [ ] **Step 5: Commit**

```bash
git add herdr_review/dialogs.py tests/unit/test_dialogs.py
git commit -m "feat: recognise grok's still-running status line"
```

---

### Task 3: The runner keeps a busy agent working

**Files:**
- Modify: `herdr_review/runner.py` (the `.dialogs` import; constants after `OBSERVE_ATTEMPTS`; `_summary`; the end of
  `_observe`; a new `_background_work`; the per-agent dict in `wait`)
- Modify: `herdr_review/cli.py` (`cmd_status`, the reason column)
- Test: `tests/unit/test_runner_start.py` (a `grok` profile in `RAW`), `tests/unit/test_runner_wait.py`,
  `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `exclusive.live_wrappers`, `exclusive.wrapper_entry` (Task 1); `dialogs.grok_background`,
  `tests.unit.test_dialogs.GROK_WAIT_SCREEN` (Task 2).
- Produces:
  - `runner.BUSY_GRACE_SEC = 30`, `runner.SCREEN_BUSY_LIMIT_SEC = 1800`,
    `runner.BUSY_FIELDS = ("background", "busy_seen_at", "screen_busy_since", "screen_busy_logged")`.
  - `Runner._background_work(self, name: str) -> str | None`.
  - `background` (str or None) in each agent of `run wait`'s output, in `Runner._summary`, in `status.json`.
  - Test helpers in `tests/unit/test_runner_wait.py`, imported by Task 4:
    `register_wrapper(run_dir: Path, agent: str, pid: int, phase: str = "running", command: str = "go test ./...") -> None`
    and `LiveWrappers(test)` with a mutable `pids: set[int]` (substitutes `exclusive.is_wrapper`).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_runner_start.py`, add a grok profile to `RAW["profiles"]`, after the `gemini` line:

```python
        "grok": {"kind": "grok", "args": ["-m", "grok-4.6"]},
```

In `tests/unit/test_runner_wait.py`, replace the imports at the top of the file:

```python
import unittest
from pathlib import Path

from herdr_review.runner import Runner, RunnerError
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_runner_start import RunnerBase, make_run
```

with:

```python
import json
import unittest
from pathlib import Path
from unittest import mock

from herdr_review import exclusive
from herdr_review.runner import BUSY_GRACE_SEC, SCREEN_BUSY_LIMIT_SEC, Runner, RunnerError
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_dialogs import GROK_WAIT_SCREEN
from tests.unit.test_runner_start import RunnerBase, make_run


def register_wrapper(run_dir: Path, agent: str, pid: int, phase: str = "running", command: str = "go test ./...") -> None:
    """An entry of the run's wrapper registry, as `herdr-review exclusive` writes it."""
    path = exclusive.wrapper_entry(run_dir, pid)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "agent": agent, "run_id": "hrtest", "command": command, "phase": phase,
                                "started_at": "2026-10-10T10:00:00+00:00", "running_since": None}))


class LiveWrappers:
    """The pids that count as live wrappers in a test: is_wrapper, substituted."""

    def __init__(self, test: unittest.TestCase):
        self.pids: set[int] = set()
        patcher = mock.patch("herdr_review.exclusive.is_wrapper", side_effect=lambda pid: pid in self.pids)
        patcher.start()
        test.addCleanup(patcher.stop)
```

Append at the end of `tests/unit/test_runner_wait.py`:

```python


class BackgroundWorkTest(RunnerBase):
    """herdr reports an agent idle or done while it waits for its own background work: its command in the build
    queue, or for grok a background task. The runner keeps it working and names the work in `background`."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.run_dir = make_run(self.root, self.repo, reviewers=("claude-opus", "grok"), fixer="claude-opus")
        self.r = Runner(self.run_dir, herdr=self.herdr, poll_sec=5, clock=self.clock, sleep=self.clock.sleep,
                        wall_clock=lambda: 1000.0 + self.clock.t)
        self.r.start_reviewers()
        self.live = LiveWrappers(self)

    def busy(self, agent: str, pid: int = 4242, **entry) -> None:
        register_wrapper(self.run_dir, agent, pid, **entry)
        self.live.pids.add(pid)

    def log(self) -> str:
        return (self.run_dir / "runner.log").read_text()

    def test_done_with_a_live_wrapper_stays_working(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.herdr.agent_status["hrtest-grok"] = ["working"]
        out = self.r.wait()
        a = out["agents"]["hrtest-claude-opus"]
        self.assertEqual(out["reason"], "checkin")
        self.assertEqual((a["state"], a["background"]), ("working", "running its command: go test ./..."))
        self.assertIn("hrtest-claude-opus", out["pending"])
        self.assertEqual(self.log().count("hrtest-claude-opus: herdr reports done, kept working: running its command"), 1)

    def test_a_wrapper_waiting_for_its_turn_names_the_queue(self):
        self.busy("hrtest-claude-opus", phase="waiting", command="npm test")
        self.herdr.agent_status["hrtest-claude-opus"] = ["idle"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["background"], "waiting for its turn in the build queue: npm test")

    def test_the_running_wrapper_is_named_before_a_waiting_one(self):
        self.busy("hrtest-claude-opus", pid=4242, phase="waiting", command="npm test")
        self.busy("hrtest-claude-opus", pid=4343, phase="running", command="go test ./...")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["background"], "running its command: go test ./...")

    def test_another_agents_wrapper_or_a_dead_one_does_not_count(self):
        self.busy("hrtest-grok", pid=4242)
        register_wrapper(self.run_dir, "hrtest-claude-opus", 4343)       # its wrapper died: no live pid
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual((out["reason"], out["agents"]["hrtest-claude-opus"]["state"]), ("state_change", "done"))
        self.assertIsNone(out["agents"]["hrtest-claude-opus"]["background"])

    def test_the_grok_status_line_keeps_a_grok_agent_working(self):
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.herdr.screens["hrtest-grok"] = GROK_WAIT_SCREEN
        out = self.r.wait(agent="hrtest-grok")
        self.assertEqual(out["reason"], "checkin")
        self.assertEqual(out["agents"]["hrtest-grok"]["state"], "working")
        self.assertEqual(out["agents"]["hrtest-grok"]["background"], "grok: 1 command still running")

    def test_the_grok_status_line_means_nothing_for_another_kind(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["idle"]
        self.herdr.screens["hrtest-claude-opus"] = GROK_WAIT_SCREEN
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "idle")

    def test_the_grace_holds_the_agent_for_30_s_after_its_work_ends(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.assertEqual(self.r.wait(agent="hrtest-claude-opus")["reason"], "checkin")
        self.live.pids.clear()                                         # the command ended
        start = self.clock.t
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual((out["reason"], out["agents"]["hrtest-claude-opus"]["state"]), ("state_change", "done"))
        self.assertEqual(self.clock.t - start, BUSY_GRACE_SEC)
        self.assertIn("hrtest-claude-opus: herdr reports done, kept working: its background work ended moments ago", self.log())
        self.assertIn("hrtest-claude-opus: its background work is over", self.log())

    def test_the_grok_line_alone_counts_for_30_minutes(self):
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.herdr.screens["hrtest-grok"] = GROK_WAIT_SCREEN
        out = self.r.wait(agent="hrtest-grok")
        while out["reason"] == "checkin":
            out = self.r.wait(agent="hrtest-grok")
        self.assertEqual(out["agents"]["hrtest-grok"]["state"], "done")
        self.assertEqual(self.clock.t, SCREEN_BUSY_LIMIT_SEC + BUSY_GRACE_SEC)
        self.assertEqual(self.log().count('hrtest-grok: grok has shown "1 command still running" for 30 minutes'), 1)

    def test_blocked_wins_over_background_work(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["blocked"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "blocked")
        self.assertIsNone(out["agents"]["hrtest-claude-opus"]["background"])

    def test_background_is_cleared_once_herdr_sees_the_agent_work(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done", "working"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "working")
        self.assertIsNone(out["agents"]["hrtest-claude-opus"]["background"])
        self.assertIn("hrtest-claude-opus: its background work is over", self.log())

    def test_the_fixer_stays_working_while_its_tests_run(self):
        self.r.start_fixer()
        self.r.status.agent("hrtest-fixer")["prompted"] = True         # it was given its task
        self.busy("hrtest-fixer")
        self.herdr.agent_status["hrtest-fixer"] = ["done"]
        out = self.r.wait(agent="hrtest-fixer")
        self.assertEqual(out["agents"]["hrtest-fixer"]["state"], "working")
        labels = [c[2] for c in self.herdr.calls_named("tab_rename") if c[2].startswith("rv-hrtest: fixer")]
        self.assertEqual(labels[-1], "rv-hrtest: fixer ⏳")
        self.assertNotIn("rv-hrtest: fixer ✓", labels)

    def test_a_secret_in_the_command_is_masked(self):   # Review Focus 2
        self.busy("hrtest-claude-opus", command="env TOKEN=s3cret go test ./...")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["background"], "running its command: env TOKEN=*** go test ./...")
        self.assertNotIn("s3cret", (self.run_dir / "status.json").read_text())
        self.assertNotIn("s3cret", self.log())

    def test_an_unreadable_grok_screen_goes_by_herdrs_status(self):   # Review Focus 3
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.herdr.reads["hrtest-grok"] = [None]
        out = self.r.wait(agent="hrtest-grok")
        self.assertEqual((out["reason"], out["agents"]["hrtest-grok"]["state"]), ("state_change", "done"))

    def test_a_live_wrapper_is_named_before_the_grok_line_and_stops_its_clock(self):   # Review Focus 5
        self.busy("hrtest-grok", command="go test -race ./...")
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.herdr.screens["hrtest-grok"] = GROK_WAIT_SCREEN
        for _ in range(7):                                             # 35 minutes of check-ins
            out = self.r.wait(agent="hrtest-grok")
        self.assertEqual(out["agents"]["hrtest-grok"]["state"], "working")
        self.assertEqual(out["agents"]["hrtest-grok"]["background"], "running its command: go test -race ./...")
        self.assertIsNone(self.r.status.agent("hrtest-grok").get("screen_busy_since"))

    def test_the_summary_names_the_background_work(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(self.r._summary("hrtest-claude-opus")["background"], "running its command: go test ./...")
```

Notes for the implementer: `wait` returns `checkin` after `checkin_sec` (300 s in `make_run`) of the fake clock, and
the runner's wall clock follows the fake clock here, so the grace and the 30-minute limit play out in fake time.
`RAW`'s `claude-opus` profile carries `TOKEN: s3cret`, which the runner masks as `***`.

In `tests/unit/test_cli.py`, add this test to `CliParsingTest`, directly before
`test_status_says_the_working_tree_changed_not_who_changed_it`:

```python
    def test_status_names_an_agents_background_work_in_place_of_its_reason(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunStatus.create(Path(d), run_id="hrtest", repo=d, layout="tabs")
            st.add_agent("hrtest-grok", role="reviewer", profile="grok", kind="grok", state="working",
                         reason="an older reason", background="grok: 1 command still running")
            st.add_agent("hrtest-codex", role="reviewer", profile="codex", kind="codex", state="working", reason="its reason")
            out = io.StringIO()
            with redirect_stdout(out):
                main(["status", "--run", d])
            lines = out.getvalue().splitlines()
            self.assertTrue(next(l for l in lines if l.startswith("hrtest-grok")).endswith("фон: grok: 1 command still running"))
            self.assertTrue(next(l for l in lines if l.startswith("hrtest-codex")).endswith("its reason"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_wait tests.unit.test_cli -v 2>&1 | tail -6`
Expected: FAIL — `ImportError: cannot import name 'BUSY_GRACE_SEC' from 'herdr_review.runner'` for `test_runner_wait`,
and `test_status_names_an_agents_background_work_in_place_of_its_reason` fails with `AssertionError: False is not true`.

- [ ] **Step 3: Implement**

In `herdr_review/runner.py`:

1. Add `grok_background` to the import from `.dialogs`:

```python
from .dialogs import MCP_UNCHECKED, SCREEN_LINES, codex_session_ready, codex_update_complete, codex_update_running, codex_update_started, grok_background, mcp_check, resolve_startup_dialog
```

2. After `OBSERVE_ATTEMPTS = 3` add:

```python
# An agent herdr reports idle or done stays `working` while it waits for its own background work (_background_work):
# for this long after the last sign of that work, which covers the seconds before the agent's next step; and on
# grok's status line alone, without a wrapper of the build queue, for at most this long in a row.
BUSY_GRACE_SEC = 30
SCREEN_BUSY_LIMIT_SEC = 1800
BUSY_FIELDS = ("background", "busy_seen_at", "screen_busy_since", "screen_busy_logged")
```

3. In `_summary`, add `background` to `out`:

```python
        out = {"state": a["state"], "tab": a.get("tab"), "pane": a.get("pane"), "reason": a.get("reason"),
               "background": a.get("background"),
               "codex_update_pending": bool(a.get("codex_update_pending")), "update_restarts": int(a.get("update_restarts", 0))}
```

4. Replace the end of `_observe`, which today reads:

```python
        current = self.status.agent(name)["state"]
        new = "blocked-start" if (current == "blocked-start" and live == "blocked") else live
        if new != current:
            self._set_state(name, new, generation=generation)
        return True
```

with the following, and add `_background_work` right after `_observe`:

```python
        a = self.status.agent(name)
        current = a["state"]
        new = "blocked-start" if (current == "blocked-start" and live == "blocked") else live
        before = {k: a.get(k) for k in BUSY_FIELDS}
        background = self._background_work(name) if live in ("idle", "done") else None
        if background is not None:
            new = "working"
            background = self.herdr.mask(background)
        a["background"] = background
        if background != before["background"]:
            self.log(f"{name}: herdr reports {live}, kept working: {background}" if background is not None
                     else f"{name}: its background work is over")
        if any(a.get(k) != v for k, v in before.items()):
            self.status.mark_agent(name, generation=generation)
            if new == current:
                self.status.save()
        if new != current:
            self._set_state(name, new, generation=generation)
        return True

    def _background_work(self, name: str) -> str | None:
        """What <name> still waits for while herdr reports it idle or done, or None: its command in the build queue
        (the run's wrapper registry); for grok, its background task or subagent (the status line above its input);
        or work that ended less than BUSY_GRACE_SEC ago. Records in the agent's entry when it last saw such work."""
        a = self.status.agent(name)
        now = self.wall_clock()
        wrappers = [w for w in exclusive.live_wrappers(self.run_dir) if w.get("agent") == name]
        if wrappers:
            w = next((w for w in wrappers if w.get("phase") == "running"), wrappers[0])
            a.update(busy_seen_at=now, screen_busy_since=None, screen_busy_logged=False)
            if w.get("phase") == "running":
                return f"running its command: {w.get('command')}"
            return f"waiting for its turn in the build queue: {w.get('command')}"
        line = None
        if a.get("kind") == "grok":
            screen = self.herdr.agent_read(name, source="visible", lines=SCREEN_LINES)
            line = grok_background(screen) if screen is not None else None
        if line is None:
            a.update(screen_busy_since=None, screen_busy_logged=False)
        else:
            since = a.get("screen_busy_since")
            if since is None:
                a["screen_busy_since"] = since = now
            if now - since <= SCREEN_BUSY_LIMIT_SEC:
                a["busy_seen_at"] = now
                return f"grok: {line}"
            if not a.get("screen_busy_logged"):
                a["screen_busy_logged"] = True
                self.log(f'{name}: grok has shown "{line}" for {SCREEN_BUSY_LIMIT_SEC // 60} minutes without a'
                         " build-queue command; going by herdr's status")
        seen = a.get("busy_seen_at")
        if seen is not None and now - seen < BUSY_GRACE_SEC:
            return "its background work ended moments ago"
        return None
```

5. In `wait`, add `background` to each agent's dict, after `reason`:

```python
                "reason": a.get("reason"),
                "background": a.get("background"),
                "codex_update_pending": bool(a.get("codex_update_pending")),
```

In `herdr_review/cli.py`, `cmd_status`, replace

```python
        reason = (a.get("reason") or "")[:60]
```

with

```python
        reason = (f"фон: {a['background']}" if a.get("background") else a.get("reason") or "")[:60]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests/unit -t . 2>&1 | tail -3`
Expected: PASS — the whole unit suite (`OK`); the codex-update and collect suites must stay green too.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/runner.py herdr_review/cli.py tests/unit/test_runner_start.py tests/unit/test_runner_wait.py tests/unit/test_cli.py
git commit -m "fix: keep an agent working while it waits for its own background work"
```

---

### Task 4: `collect` looks again before it re-prompts or fails

**Files:**
- Modify: `herdr_review/runner.py` (`collect`, the branch for `idle`/`done`/`unknown`, at about line 1085)
- Test: `tests/unit/test_runner_collect.py`

**Interfaces:**
- Consumes: `register_wrapper`, `LiveWrappers` from `tests.unit.test_runner_wait` (Task 3); `_observe` with its
  background check (Task 3).
- Produces: nothing new for later tasks.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_runner_collect.py`, replace the imports

```python
from herdr_review import gitutil
from herdr_review.runner import DRIFT_NOTHING_NEW_OR_GONE, Runner, retry_text
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_runner_start import RunnerBase, git, make_run
```

with

```python
from herdr_review import gitutil
from herdr_review.herdr import HerdrResult
from herdr_review.runner import DRIFT_NOTHING_NEW_OR_GONE, Runner, retry_text
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_runner_start import RunnerBase, git, make_run
from tests.unit.test_runner_wait import LiveWrappers, register_wrapper
```

Append at the end of the file:

```python


class CollectBackgroundTest(RunnerBase):
    """collect looks at an idle or done reviewer without a valid review once more before it prompts or fails it."""

    def setUp(self):
        super().setUp()
        self.run_dir = make_run(self.root, self.repo, reviewers=("claude-opus", "codex"))
        self.r = Runner(self.run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        self.r.start_reviewers()
        self.live = LiveWrappers(self)
        self.n = "hrtest-claude-opus"

    def busy(self, pid: int = 4242) -> None:
        register_wrapper(self.run_dir, self.n, pid)
        self.live.pids.add(pid)

    def prompts(self) -> int:
        return len([c for c in self.herdr.calls_named("agent_prompt") if c[1] == self.n])

    def test_a_busy_reviewer_without_a_review_is_pending_and_not_prompted(self):
        self.r.status.set_agent_state(self.n, "done")                  # what the last wait stored
        self.busy()
        before = self.prompts()
        out = self.r.collect()
        self.assertIn(self.n, out["pending"])
        self.assertEqual(self.prompts(), before)
        a = self.r.status.agent(self.n)
        self.assertEqual((a["state"], a["collect_retries"]), ("working", 0))
        self.assertEqual(a["background"], "running its command: go test ./...")

    def test_a_second_miss_while_busy_does_not_fail_the_reviewer(self):
        self.r.status.set_agent_state(self.n, "idle")
        self.r.collect()                                               # the first miss: collect's re-prompt
        self.assertEqual(self.r.status.agent(self.n)["collect_retries"], 1)
        self.r.status.set_agent_state(self.n, "done")
        self.busy()
        out = self.r.collect()
        self.assertIn(self.n, out["pending"])
        self.assertEqual(out["failed"], {})
        self.assertEqual(self.r.status.agent(self.n)["state"], "working")

    def test_a_reviewer_working_again_is_not_prompted(self):
        self.r.status.set_agent_state(self.n, "done")
        self.herdr.agent_status[self.n] = ["working"]
        before = self.prompts()
        out = self.r.collect()
        self.assertIn(self.n, out["pending"])
        self.assertEqual(self.prompts(), before)
        self.assertEqual(self.r.status.agent(self.n)["collect_retries"], 0)

    def test_a_reviewer_herdr_cannot_report_stays_pending(self):
        self.r.status.set_agent_state(self.n, "idle")
        self.r.status.agent(self.n)["collect_retries"] = 1             # a second miss would fail it
        self.r.status.mark_agent(self.n)
        self.r.status.save()
        self.herdr.agent_get = lambda name: HerdrResult(False, 1, error_code="timeout", message="herdr timed out")
        out = self.r.collect()
        self.assertIn(self.n, out["pending"])
        self.assertEqual(out["failed"], {})
        self.assertEqual(self.r.status.agent(self.n)["state"], "idle")
```

Notes for the implementer: after `start_reviewers`, FakeHerdr answers `agent get` with `idle` for every reviewer
unless a test sets `agent_status`; `set_agent_state` only changes what `status.json` stores, as an older `wait` would
have left it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_collect.CollectBackgroundTest -v 2>&1 | tail -6`
Expected: FAIL — four failures: `AssertionError: 2 != 1` (prompted although busy or working again) and
`AssertionError: 'hrtest-claude-opus' not found in ['hrtest-codex']` (marked `failed`).

- [ ] **Step 3: Implement the fresh look**

In `herdr_review/runner.py`, `collect`, in the branch `if st in ("idle", "done", "unknown"):`, directly after

```python
                if st == "unknown":
                    pending.append(n)
                    continue
```

insert:

```python
                # A fresh look before any prompt or failure: herdr may report an agent idle or done while it waits
                # for its own background work, and the state the last `wait` stored may be old.
                if not self._observe(n):
                    pending.append(n)
                    continue
                a = self.status.agent(n)
                generation = self._codex_generation(n)
                if a["state"] not in ("idle", "done"):
                    pending.append(n)
                    continue
```

The existing code that follows (`if not a.get("prompted"): …`, the one re-prompt, the `failed` on the second miss)
stays as it is and now works on the fresh state.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests/unit -t . 2>&1 | tail -3`
Expected: PASS — the whole unit suite (`OK`), including the existing `test_missing_file_is_retried_once_then_failed`
(without background work the first miss still re-prompts and the second fails).

- [ ] **Step 5: Commit**

```bash
git add herdr_review/runner.py tests/unit/test_runner_collect.py
git commit -m "fix: collect looks at a reviewer again before it re-prompts or fails it"
```

---

### Task 5: Prompts

**Files:**
- Modify: `prompts/exclusive.md`, `prompts/orchestrator.md`
- Test: `tests/unit/test_prompts.py`

**Interfaces:**
- Consumes: the field name `background` (Task 3).
- Produces: nothing for later tasks.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_prompts.py`, `test_the_heavy_command_rules_reach_the_reviewer_and_the_fixer`, inside the loop,
directly after the line that asserts `"give the call a timeout that covers the command and up to 60 s of waiting for the turn"`, add:

```python
                self.assertIn("You may start the wrapper in the background and go on working while it runs. Before you"
                              " write your result and reply DONE, wait for every command you started, the wrapper's"
                              " included, and read its output. Never end your turn while one of your commands still"
                              " runs.", text)
```

Add a test method to `PromptTemplatesTest`, directly before `test_orchestrator_prompt_renders_and_names_every_subcommand`:

```python
    def test_the_orchestrator_leaves_an_agent_busy_with_background_work_alone(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        self.assertIn("`screen_changed` (did the screen change since the previous `wait`?), `reason`, `background`.", text)
        self.assertIn("`working` with `background` set → it waits for its own work. Keep waiting, and send it no keys and"
                      " no prompt, however long its screen stays the same.", text)
        self.assertIn("`working` without `background`, with `screen_changed: false` on two consecutive waits →", text)
        self.assertIn("`N queued, Enter to send now` there means a prompt waits in grok's queue. grok delivers that prompt"
                      " when the agent's turn ends; Enter would send it at once and cut the turn short. Never press Enter"
                      " or any other key in such a tab.", text)
        self.assertIn("A grok agent may only hold the prompt in its queue while it waits for its own background work",
                      text)
        self.assertIn("Stop when the fixer is `idle` or `done`; while its `background` is set, the runner keeps it"
                      " `working`.", text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_prompts -v 2>&1 | tail -5`
Expected: FAIL — `AssertionError` in both tests: the text is not in the prompts yet.

- [ ] **Step 3: Edit the prompts**

`prompts/exclusive.md`: insert a new bullet directly before the line that starts with

```text
- A command still running after 30 minutes is stopped:
```

The new bullet:

```markdown
- You may start the wrapper in the background and go on working while it runs. Before you write your result and reply DONE, wait for every command you started, the wrapper's included, and read its output. Never end your turn while one of your commands still runs.
```

`prompts/orchestrator.md`, four find-and-replace edits; each "find" text occurs exactly once in the file (checked
with `grep -cF` at `d373b80`).

1. Phase 1, the `prompt_stalled` bullet. Find:

```text
Do nothing now: the next `wait` / `collect` cycle re-prompts it once automatically.
```

Replace with:

```text
Do nothing now: the next `wait` / `collect` cycle re-prompts it once automatically. A grok agent may only hold the prompt in its queue while it waits for its own background work; grok delivers it when that work ends.
```

2. Phase 2, step 1. Find:

```text
(did the screen change since the previous `wait`?), `reason`.
```

Replace with:

```text
(did the screen change since the previous `wait`?), `reason`, `background`. `background` is set while an agent that herdr shows idle still waits for its own work — its command in the build queue, or a grok background task — and the runner keeps that agent `working` meanwhile.
```

3. Phase 2, step 3. Find (the start of a bullet; the rest of that line stays as it is):

```text
   - `working` with `screen_changed: false` on two consecutive waits →
```

Replace with (two new bullets, then the reworded start of the old one):

```text
   - `working` with `background` set → it waits for its own work. Keep waiting, and send it no keys and no prompt, however long its screen stays the same.
   - A grok tab that shows `N command(s) … still running` above its input runs a background task of its agent, and `N queued, Enter to send now` there means a prompt waits in grok's queue. grok delivers that prompt when the agent's turn ends; Enter would send it at once and cut the turn short. Never press Enter or any other key in such a tab.
   - `working` without `background`, with `screen_changed: false` on two consecutive waits →
```

4. Phase 4, step 3, which Phase 5 reuses. Find:

```text
Stop when the fixer is `idle` or `done`.
```

Replace with:

```text
Stop when the fixer is `idle` or `done`; while its `background` is set, the runner keeps it `working`.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_prompts 2>&1 | tail -3`
Expected: PASS (`OK`, 33 tests).

- [ ] **Step 5: Commit**

```bash
git add prompts/exclusive.md prompts/orchestrator.md tests/unit/test_prompts.py
git commit -m "fix: the prompts leave an agent busy with background work alone"
```

---

### Task 6: Documentation and the full run

**Files:**
- Modify: `README.md`, `CHANGELOG.md`
- Modify, untracked and never staged: `docs/2026-10-10-grok-dialogs-and-background-tasks.md`

**Interfaces:**
- Consumes: the behaviour and names of Tasks 1–5.
- Produces: nothing.

- [ ] **Step 1: README**

In the bullet `- **Tabs:** …` (one line). Find:

```text
` ❓` stuck on a dialog or waiting for you. Once the run has finished
```

Replace with:

```text
` ❓` stuck on a dialog or waiting for you. An agent stays ` ⏳` while its command in the build queue or a grok background task still runs, even when herdr shows it idle; `herdr-review status` names that work under «фон:» (`background` with `--json`). Once the run has finished
```

In the troubleshooting list, add a new bullet on its own line directly after the line that starts with

```text
- A reviewer stays ` ⏳` a long time and its tab shows `herdr-review exclusive: waiting`
```

The new bullet:

```markdown
- A grok reviewer or the fixer shows ` ⏳` while its tab looks idle, with `1 command still running` above the input — it waits for its own background command; `herdr-review status` shows it under «фон:». Do not press Enter in that tab: grok would send its queued prompt at once and cut the agent's turn short.
```

- [ ] **Step 2: CHANGELOG**

Insert directly before `## [0.5.0] - 2026-10-04`:

```markdown
## [Unreleased]

### Fixed
- A reviewer or the fixer that herdr reports idle or done while it waits for its own background work stays `working`:
  its command in the build queue, for every agent kind, and for grok any background task or subagent that grok's
  status line shows above its input (trusted alone for 30 minutes at most). `collect` looks at an idle or done
  reviewer again before it re-prompts or fails it, so a reviewer waiting for its own tests is neither re-prompted nor
  failed.

### Added
- `herdr-review exclusive` registers each wrapper of a review in `<run_dir>/wrappers/<pid>.json` while it waits for
  its turn and while it runs its command.
- `background`: what an agent that herdr shows idle still waits for, in `run wait`, `run prompt`, `start-reviewers`,
  `start-fixer`, `status.json` and `herdr-review status` («фон:»).

### Changed
- The heavy-command rules ask agents to wait for every command they started before they reply DONE. The
  orchestrator leaves an agent with `background` alone and never presses Enter in a grok tab that shows a running
  background task or a queued prompt.

```

- [ ] **Step 3: The owner's analysis (untracked; edit in place, never stage it)**

In `docs/2026-10-10-grok-dialogs-and-background-tasks.md`:

1. Replace the two lines

```markdown
The logs explain both. Neither is a defect in the review logic. Still, herdr-review can stop both from costing a
run time and from putting a working reviewer at risk.
```

with

```markdown
The logs explain both. The first is not a defect of herdr-review. The second is partly one: the runner trusts herdr's
`done`, which herdr reports for a grok agent that waits for its own background task. herdr-review can stop both from
costing a run time and from putting a working reviewer at risk.
```

2. Replace the summary table's row that starts with `| 2 | deepseek-flash-lanit` with:

```markdown
| 2 | deepseek-flash-lanit waited inside its turn for its `exclusive` race tests, a grok background task. herdr reported `done`, `collect` re-prompted it, grok queued the prompt, herdr answered `agent_prompt_stalled`, and the orchestrator's Enter cut the agent's turn short | grok moves a long command to the background, and while a turn waits for it the screen shows an empty input: herdr reports `done`. The runner takes `done` for finished | In herdr-review: an agent whose wrapper still runs, or whose grok screen shows background work, stays working; `collect` looks again before it re-prompts or fails; the orchestrator never presses Enter in such a tab. In herdr (upstream): report such a grok agent as working |
```

3. Replace everything from the heading line

   ```text
   ## 2. deepseek-flash-lanit: `done` while its tests ran
   ```

   up to, not including, the heading line `## 3. Other observations from the run`, with:

````markdown
## 2. deepseek-flash-lanit: `done` while it waited for its tests

Corrected from the grok session's `events.jsonl` and `chat_history.jsonl` and the orchestrator's transcript: the
agent did not end its turn. The design of the fix: `docs/superpowers/specs/2026-10-10-agent-background-work-design.md`.

### Timeline (UTC)

| Time | Event | Source |
|---|---|---|
| 21:43:37 | The model calls `run_terminal_command` with `"<runner>" exclusive --timeout 1500 -- sh -c '… go test -race …'` and `block_until_ms: 0`: a grok background task | `chat_history.jsonl`, `events.jsonl` |
| 21:44:19 | The wrapper gets the queue after 42 s | runner.log, `exclusive:` |
| 21:45:04 | The model waits for its task: `get_command_or_subagent_output(task_ids=[…], timeout_ms=120000)`. The turn goes on: no `turn_ended` until 21:45:57 | `chat_history.jsonl`, `events.jsonl` |
| 21:45:09 | herdr reports `agent_status: done`. The screen shows an empty input under `◉ 1 command still running · send a message to interrupt`, the line grok shows while a turn waits for background work | runner.log |
| 21:45:12 | `collect` finds no review file and re-prompts with `retry_text` (`--until working`); `collect_retries` becomes 1 | runner.log, status.json |
| 21:45:18 | herdr answers `agent_prompt_stalled: … within 5000 ms; current status is done`: grok queued the text | runner.log |
| 21:45:31 | herdr reports `idle`; the runner sets `idle`. The screen shows `#1 You have not written a valid review …` and `○ 1 command still running · 1 queued, Enter to send now`. By orchestrator.md the next `collect` would mark the reviewer `failed`; the orchestrator did not call it | runner.log, the orchestrator's transcript |
| 21:45:51 | The wrapper's command ends with exit 0, and the model's wait returns; at 21:45:54 it starts `npm test` the same way | runner.log, `events.jsonl` |
| 21:45:57 | The orchestrator runs `herdr agent send-keys … enter` ("Send the queued re-prompt to deepseek now"). grok sends the queued prompt at once and aborts the turn: `turn_ended: cancelled, mid_turn_abort, trigger: send_now` | the orchestrator's transcript, `events.jsonl` |
| 21:50:11 | In the new turn the agent writes its review; it is collected at 21:50:28 | `events.jsonl`, runner.log |

### Root cause

- **grok runs long commands in the background.** `run_terminal_command` moves a command that outlives
  `block_until_ms` (30 000 ms by default) to the background; `0` starts it there at once. A test run through the
  wrapper, which may wait up to 60 s for its turn, is almost always a background task.
- **The model waited for it, as it should.** It read code meanwhile, then waited in `get_command_or_subagent_output`.
  Its turn did not end before the orchestrator's Enter.
- **herdr reports such a wait as `done`.** Its grok integration only reports the session id at start
  (`SessionStart`); the state comes from the terminal, which shows an empty input and no spinner during the wait.
- **The runner takes `done` for finished.** `STATE_CLASS` (runner.py:26) maps `done` and `idle` to `settled`, and
  `collect` (runner.py:1050) spends the reviewer's only re-prompt; a second miss marks it `failed`. The review survived
  only because the orchestrator skipped that `collect`.
- **A queued prompt reads as stalled.** grok queues a prompt sent during the wait; herdr waits 5 s for `working` and
  answers `agent_prompt_stalled` (runner.py:671).
- **Enter on the queued prompt cuts the turn short.** grok's own hint (`Enter to send now`) invites it, and nothing in
  orchestrator.md forbade it.

### What to change

See the design; in short:

- **2.1 An agent with a live wrapper is working.** A registry of live wrappers per run, `<run_dir>/wrappers/<pid>.json`.
- **2.2 grok's status line as a second signal.** The documented `N command(s) … still running` line, and
  `waiting · send a message to interrupt`, searched at the bottom of the screen; trusted alone for 30 minutes at most.
- **2.3 `collect` looks again before it re-prompts or fails**, instead of giving back `collect_retries`, which
  `status.py` merges with `max()` and would undo.
- **2.4 The orchestrator leaves such an agent alone**, and never presses Enter in a grok tab with a running task or a
  queued prompt.
- **2.5 Prompts: wait for every command you started before DONE.** It would not have prevented this incident (the
  model waited), but Claude Code too can end a turn while a background command runs.
- **2.6 herdr (upstream).** Report a grok agent that waits for background work as `working`; return a distinct result
  instead of `agent_prompt_stalled` when grok queues a prompt. The texts are in the design's appendix.

````

4. Replace everything from the heading `## 4. Order of work` up to, not including, the heading
   `## Appendix: how the evidence was gathered` with:

```markdown
## 4. Order of work

1. Section 2, with tests: no reviewer that is still working can be failed any more
   (`docs/superpowers/specs/2026-10-10-agent-background-work-design.md`).
2. 1.2: prompt text, pinned in `test_prompts.py`.
3. 1.3 (rules for grok agents) and 1.6 (README).
4. 1.4 and 1.5.
5. The always-approve-with-limits mode of 1.3, if a model's classifier stays broken.

Outside herdr-review: 1.1 (the adapter) and 2.6 (the herdr issues).

```

Then make sure it stays untracked: `git status --short docs/2026-10-10-grok-dialogs-and-background-tasks.md` must
print `?? docs/2026-10-10-grok-dialogs-and-background-tasks.md` (or nothing, when `docs/` as a whole shows as `?? docs/`).

- [ ] **Step 4: Run every test**

Run: `tests/run.sh`
Expected: the unit tests end with `Ran 619 tests` and `OK`; bats prints 37 `ok` lines and no `not ok`.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: agents busy with background work in README and CHANGELOG"
git status --short
```

Expected after the commit: only the owner's untracked paths remain (`?? docs/…` entries); nothing staged.
