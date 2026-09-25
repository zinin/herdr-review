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
        self.env = clean_env(HERDR_REVIEW_RUN=str(self.run_dir), HERDR_REVIEW_AGENT="hrtest-codex")

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
        first = p.stderr.readline()                  # the waiting notice: the wrapper found the queue held
        self.assertIn("waiting —", first)
        self.assertIsNone(p.poll())
        self.assertFalse(marker.exists())
        held.release()
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, first + err)
        self.assertTrue(marker.exists())
        self.assertIn("herdr-review exclusive: your turn after", err)

    def test_the_command_gets_no_input(self):
        p = self.exclusive("--", "cat", input="typed by someone\n")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_every_argument_after_the_separator_reaches_the_command(self):
        p = self.exclusive("--", "sh", "-c", 'printf "%s\\n" "$@"', "sh", "--", "--wait", "b c", "привет")
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


HARNESS = (
    "import os, sys\n"
    "from herdr_review import exclusive\n"
    "sys.exit(exclusive.run(sys.argv[1:], timeout_sec=0.5, grace_sec=0.5, poll_sec=0.05, environ=os.environ))\n"
)
SIGNAL_HARNESS = (       # a timeout no test reaches: only a stop signal ends the command, SIGKILL follows 0.5 s later
    "import os, sys\n"
    "from herdr_review import exclusive\n"
    "sys.exit(exclusive.run(sys.argv[1:], timeout_sec=60, grace_sec=0.5, poll_sec=0.05, environ=os.environ))\n"
)
CLEANUP_HARNESS = (      # the command times out at once and has 5 s to clean up before SIGKILL
    "import os, sys\n"
    "from herdr_review import exclusive\n"
    "sys.exit(exclusive.run(sys.argv[1:], timeout_sec=0.5, grace_sec=5, poll_sec=0.05, environ=os.environ))\n"
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

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_CHILD_SUBREAPER is Linux-only")
    def test_ctrl_c_to_the_whole_group_also_stops_a_background_job_of_the_command(self):
        pidfile = self.root / "bg.pid"
        job = 'sleep 300 >/dev/null 2>&1 & echo $! > "$1"; sleep 300'      # the job must not hold the stderr pipe
        p = subprocess.Popen([sys.executable, "-c", SIGNAL_HARNESS, "sh", "-c", job, "sh", str(pidfile)],
                             cwd=PACKAGE_ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        self.addCleanup(stop_quietly, p)
        self.assertTrue(self.holder_written())
        self.assertTrue(wait_until(lambda: pidfile.exists() and pidfile.read_text().strip() != ""))
        background = int(pidfile.read_text())
        self.addCleanup(kill_quietly, background)
        os.killpg(p.pid, signal.SIGINT)       # sh dies of it; the job ignores SIGINT and outlives its parent
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertTrue(wait_until(lambda: not alive(background)))
        self.assertEqual(queue_state(self.runs), {"held": False})

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_CHILD_SUBREAPER is Linux-only")
    def test_the_command_sees_an_orphaned_helper_it_stopped_as_gone(self):
        pidfile = self.root / "helper.pid"
        # The helper's parent exits at once, so the wrapper adopts it; `kill -0` counts a zombie as alive.
        script = ('sh -c \'sleep 30 >/dev/null 2>&1 & echo $! > "$1"\' helper "$1"; pid=$(cat "$1"); kill "$pid"; '
                  'while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done')
        p = subprocess.run(exclusive_cmd("--timeout", "5", "--", "sh", "-c", script, "sh", str(pidfile)),
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)                    # 124: the helper stayed a zombie to the timeout
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(self):
        started = time.monotonic()
        p = subprocess.run([sys.executable, "-c", HARNESS, "sh", "-c", "trap '' TERM; sleep 30"], cwd=PACKAGE_ROOT,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_cleanup_that_a_term_trap_starts_gets_the_rest_of_the_grace(self):
        marker = self.root / "cleaned"
        trap = '(sleep 1; echo cleaned > "$1") >/dev/null 2>&1 & exit 143'
        command = ["sh", "-c", f"trap '{trap}' TERM; sleep 30 & wait", "sh", str(marker)]
        started = time.monotonic()
        p = subprocess.run([sys.executable, "-c", CLEANUP_HARNESS, *command], cwd=PACKAGE_ROOT, env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertTrue(marker.exists(), "the cleanup was killed before it wrote its marker")
        self.assertLess(time.monotonic() - started, 4.5)        # once nothing is left, the wrapper stops waiting
        self.assertEqual(queue_state(self.runs), {"held": False})

    def test_a_process_that_a_term_trap_starts_is_killed_when_the_grace_ends(self):
        pidfile = self.root / "late.pid"
        trap = 'sleep 30 >/dev/null 2>&1 & echo $! > "$1"; exit 143'
        command = ["sh", "-c", f"trap '{trap}' TERM; sleep 30 & wait", "sh", str(pidfile)]
        started = time.monotonic()
        p = subprocess.run([sys.executable, "-c", HARNESS, *command], cwd=PACKAGE_ROOT, env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertLess(time.monotonic() - started, 10)
        late = int(pidfile.read_text())
        self.addCleanup(kill_quietly, late)
        self.assertTrue(wait_until(lambda: not alive(late)))
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
