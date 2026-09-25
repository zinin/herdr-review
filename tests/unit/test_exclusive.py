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
