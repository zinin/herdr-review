import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from herdr_review.exclusive import is_wrapper, queue_state, stop_holder
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

    def test_run_fail_stops_a_wrapper_stopped_by_sigstop(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = self.runner(run_dir)
        r.start_reviewers()
        p = self.holding(run_dir, "hrtest-codex")
        os.kill(p.pid, signal.SIGSTOP)
        self.addCleanup(p.send_signal, signal.SIGCONT)             # a failed test leaves no stopped wrapper behind
        self.assertTrue(os.WIFSTOPPED(os.waitpid(p.pid, os.WUNTRACED)[1]))    # stopped before run fail looks
        out = r.fail("hrtest-codex", "stuck")
        self.assertEqual(out["exclusive_stopped"], "hrtest-codex: sleep 30")      # no "still running after" note
        self.assertEqual(p.wait(timeout=30), 143)
        self.assertEqual(queue_state(self.runs), {"held": False})

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
        self.assertEqual(kill.call_args_list, [mock.call(99999, signal.SIGTERM), mock.call(99999, signal.SIGCONT)])
        self.assertEqual(out, "hrtest-codex: mvn test (still running after 15s)")
        self.assertIn("left to its --timeout", lines[-1])

    def test_a_free_queue_or_another_run_is_left_alone(self):
        for state in ({"held": False}, {"held": True, "run_id": "hrother", "agent": "x", "pid": 1, "command": "c"}):
            with self.subTest(state=state), mock.patch("herdr_review.exclusive.queue_state", return_value=state), \
                    mock.patch("herdr_review.exclusive.os.kill") as kill:
                self.assertIsNone(stop_holder(Path("/nowhere"), "hrtest"))
                kill.assert_not_called()


class IsWrapperTest(unittest.TestCase):
    def process(self, *words: str) -> int:
        """A live process whose command line ends with <words>. It says so once it runs: until then, just after the
        exec, its command line can still read as empty."""
        p = subprocess.Popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)", *words],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop_quietly, p)
        self.assertEqual(p.stdout.readline(), "up\n")
        return p.pid

    def test_exclusive_must_come_right_after_the_herdr_review_word(self):
        self.assertTrue(is_wrapper(self.process("/x/bin/herdr-review", "exclusive", "--", "make")))
        self.assertFalse(is_wrapper(self.process("grep", "-rw", "exclusive", "/x/herdr-review")))
        self.assertFalse(is_wrapper(self.process("/x/bin/herdr-review", "status", "--", "exclusive")))

    def test_only_a_word_that_names_herdr_review_itself_counts(self):
        self.assertFalse(is_wrapper(self.process("/x/bin/herdr-reviewer", "exclusive", "--", "make")))
        self.assertFalse(is_wrapper(self.process("/tmp/my-herdr-review-notes", "exclusive", "--", "make")))
        self.assertTrue(is_wrapper(self.process("/x/bin/herdr-review", "exclusive", "--", "make")))
        self.assertTrue(is_wrapper(self.process("herdr_review.cli", "exclusive", "--", "make")))    # python3 -m herdr_review.cli

    def test_pid_1_0_or_a_negative_one_is_never_a_wrapper(self):
        argv = ["python3", "/x/bin/herdr-review", "exclusive", "--", "make"]
        ps = subprocess.CompletedProcess(["ps"], 0, " ".join(argv) + "\n", "")
        with mock.patch.object(Path, "read_bytes", return_value="\0".join(argv).encode() + b"\0"), \
                mock.patch("herdr_review.exclusive.subprocess.run", return_value=ps):
            self.assertTrue(is_wrapper(4242))                 # any other pid now passes for a wrapper
            for pid in (1, 0, -1):
                with self.subTest(pid=pid):
                    self.assertFalse(is_wrapper(pid))          # os.kill would signal init, this process group, or all
