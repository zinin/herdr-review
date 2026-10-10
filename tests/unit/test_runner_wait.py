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


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class WaitTest(RunnerBase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.run_dir = make_run(self.root, self.repo, reviewers=("claude-opus", "codex"))
        self.r = Runner(self.run_dir, herdr=self.herdr, poll_sec=5, clock=self.clock, sleep=self.clock.sleep)
        self.r.start_reviewers()

    def test_returns_on_state_change_and_reports_screen_change(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["working", "working", "done"]
        self.herdr.agent_status["hrtest-codex"] = ["working"]
        out = self.r.wait()
        self.assertEqual(out["reason"], "state_change")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "done")
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        self.assertFalse(out["settled"])
        self.assertTrue(out["agents"]["hrtest-codex"]["screen_changed"])   # first wait: no baseline
        self.assertEqual(self.clock.t, 10)                                  # two sleeps of 5 s
        self.herdr.screens["hrtest-codex"] = "same"
        out = self.r.wait()
        self.assertEqual(out["reason"], "checkin")
        out = self.r.wait()
        self.assertFalse(out["agents"]["hrtest-codex"]["screen_changed"])
        self.herdr.screens["hrtest-codex"] = "different"
        out = self.r.wait()
        self.assertTrue(out["agents"]["hrtest-codex"]["screen_changed"])

    def test_checkin_after_interval(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        self.herdr.agent_status["hrtest-codex"] = ["working"]
        out = self.r.wait()
        self.assertEqual(out["reason"], "checkin")
        self.assertGreaterEqual(self.clock.t, 300)
        self.assertEqual(sorted(out["pending"]), ["hrtest-claude-opus", "hrtest-codex"])
        self.assertGreaterEqual(out["agents"]["hrtest-codex"]["since_sec"], 0)

    def test_blocked_returns_immediately(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        self.herdr.agent_status["hrtest-codex"] = ["working", "blocked"]
        out = self.r.wait()
        self.assertEqual(out["reason"], "state_change")
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked")
        out = self.r.wait()                     # still blocked: return at once, do not spin to checkin
        self.assertEqual(out["reason"], "blocked")
        self.assertIn(("tab_rename", "w1:t3", "rv-hrtest: codex ❓"), self.herdr.calls)

    def test_blocked_start_stays_until_prompted(self):
        run_dir = make_run(self.root / "b", self.repo, reviewers=("codex",))
        herdr = FakeHerdr()
        herdr.start_errors["hrtest-codex"] = ("agent_not_ready", "startup dialog")
        r = Runner(run_dir, herdr=herdr, poll_sec=5, clock=self.clock, sleep=self.clock.sleep)
        r.start_reviewers()
        herdr.agent_status["hrtest-codex"] = ["blocked"]
        out = r.wait()
        self.assertEqual(out["reason"], "blocked")
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked-start")
        herdr.agent_status["hrtest-codex"] = ["idle"]
        out = r.wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "idle")
        self.assertFalse(r.status.agent("hrtest-codex")["prompted"])

    def test_gone_agent_is_terminal_with_last_screen(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.pane_screens["w1:p3"] = "Segmentation fault\nuser@host$ "
        out = self.r.wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(out["agents"]["hrtest-codex"]["reason"], "agent exited")
        self.assertIn("Segmentation", self.r.status.agent("hrtest-codex")["last_screen"])
        self.assertEqual(out["pending"], [])
        self.assertTrue(out["settled"])
        self.assertIn(("tab_rename", "w1:t3", "rv-hrtest: codex ✗"), self.herdr.calls)

    def test_settled_when_all_done(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.herdr.agent_status["hrtest-codex"] = ["idle"]
        out = self.r.wait()
        self.assertIn(out["reason"], ("state_change", "settled"))
        self.assertTrue(out["settled"])
        out = self.r.wait()
        self.assertEqual(out["reason"], "settled")
        self.assertEqual(self.herdr.calls_named("agent_get")[-1][1], "hrtest-codex")

    def test_wait_single_agent(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        self.herdr.agent_status["hrtest-codex"] = ["working", "done"]
        out = self.r.wait(agent="hrtest-codex")
        self.assertEqual(list(out["agents"]), ["hrtest-codex"])
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "done")
        with self.assertRaises(RunnerError):
            self.r.wait(agent="hrtest-nobody")

    def test_named_idle_wait_observes_live_herdr_state(self):
        self.r.status.set_agent_state("hrtest-codex", "idle")
        self.r.status.set_agent_state("hrtest-claude-opus", "done")
        self.herdr.agent_status["hrtest-codex"] = ["working"]
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        out = self.r.wait(agent="hrtest-codex")
        self.assertEqual(out["reason"], "state_change")
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertFalse(out["settled"])

    def test_unobservable_agent_keeps_its_state_and_the_run_continues(self):
        from herdr_review.herdr import HerdrResult
        orig = self.herdr.agent_get

        def get(name):
            if name == "hrtest-codex":
                self.herdr.calls.append(("agent_get", name))
                return HerdrResult(False, 1, error_code="server_error", message="socket closed")
            return orig(name)

        self.herdr.agent_get = get
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        out = self.r.wait()
        self.assertEqual(self.r.status.agent("hrtest-codex")["state"], "working")   # not taken out of the run
        self.assertIn("server_error", self.r.status.agent("hrtest-codex")["reason"])
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertIn("hrtest-codex", out["pending"])
        self.assertEqual(out["reason"], "checkin")
        self.assertNotEqual(out["agents"]["hrtest-claude-opus"]["state"], "failed")

    def test_agent_get_is_retried_before_the_observation_is_given_up(self):
        from herdr_review.herdr import HerdrResult
        orig = self.herdr.agent_get
        left = {"fails": 2}

        def get(name):
            if name == "hrtest-codex" and left["fails"]:
                left["fails"] -= 1
                self.herdr.calls.append(("agent_get", name))
                return HerdrResult(False, 1, error_code="timeout", message="herdr call timed out")
            return orig(name)

        self.herdr.agent_get = get
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        self.herdr.agent_status["hrtest-codex"] = ["done"]
        out = self.r.wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "done")
        self.assertEqual(len([c for c in self.herdr.calls_named("agent_get") if c[1] == "hrtest-codex"]), 3)

    def test_wait_is_an_error_when_no_agent_can_be_observed(self):
        from herdr_review.herdr import HerdrResult

        def get(name):
            self.herdr.calls.append(("agent_get", name))
            return HerdrResult(False, 1, error_code="herdr_not_found", message="herdr not found in PATH")

        self.herdr.agent_get = get
        with self.assertRaises(RunnerError) as ctx:
            self.r.wait()
        self.assertIn("could not observe any agent", str(ctx.exception))
        self.assertIn("herdr_not_found", str(ctx.exception))
        self.assertEqual(self.r.status.agent("hrtest-codex")["state"], "working")

    def test_agent_not_found_is_gone_without_retrying(self):
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.r.wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len([c for c in self.herdr.calls_named("agent_get") if c[1] == "hrtest-codex"]), 1)

    def test_unexpected_agent_status_becomes_unknown(self):
        self.herdr.agent_status["hrtest-codex"] = ["launch_pending"]
        self.herdr.agent_status["hrtest-claude-opus"] = ["working"]
        out = self.r.wait()
        self.assertEqual(self.r.status.agent("hrtest-codex")["state"], "unknown")
        self.assertIn("launch_pending", self.r.status.agent("hrtest-codex")["reason"])
        self.assertNotEqual(out["agents"]["hrtest-claude-opus"]["state"], "failed")


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

    def test_the_grace_holds_in_the_next_runner_process(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.r.wait(agent="hrtest-claude-opus")                        # this `run wait` sees the wrapper
        self.live.pids.clear()                                         # the command ends
        later = Runner(self.run_dir, herdr=self.herdr, poll_sec=5, clock=self.clock, sleep=self.clock.sleep,
                       wall_clock=lambda: 1000.0 + self.clock.t)       # the next `run wait`, another process
        start = self.clock.t
        out = later.wait(agent="hrtest-claude-opus")
        self.assertEqual((out["reason"], out["agents"]["hrtest-claude-opus"]["state"]), ("state_change", "done"))
        self.assertEqual(self.clock.t - start, BUSY_GRACE_SEC)         # status.json carried when it was last busy

    def test_the_grok_line_alone_counts_for_30_minutes(self):
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.herdr.screens["hrtest-grok"] = GROK_WAIT_SCREEN
        out = self.r.wait(agent="hrtest-grok")
        while out["reason"] == "checkin":
            out = self.r.wait(agent="hrtest-grok")
        self.assertEqual(out["agents"]["hrtest-grok"]["state"], "done")
        self.assertEqual(self.clock.t, SCREEN_BUSY_LIMIT_SEC + 5)                # the first poll past the limit
        self.assertEqual(self.log().count('hrtest-grok: grok has shown "1 command still running" for 30 minutes'), 1)
        self.assertNotIn("ended moments ago", self.log())              # nothing ended: no grace after the cutoff

    def test_the_screen_clock_starts_again_after_herdr_sees_the_agent_work(self):
        self.herdr.screens["hrtest-grok"] = GROK_WAIT_SCREEN
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.r.wait(agent="hrtest-grok")                               # the line is seen: its 30-minute clock starts
        self.herdr.agent_status["hrtest-grok"] = ["working"]
        for _ in range(7):                                             # 35 minutes of work
            self.r.wait(agent="hrtest-grok")
        self.herdr.agent_status["hrtest-grok"] = ["done"]              # it waits for another background task
        out = self.r.wait(agent="hrtest-grok")
        self.assertEqual(out["agents"]["hrtest-grok"]["state"], "working")
        self.assertEqual(out["agents"]["hrtest-grok"]["background"], "grok: 1 command still running")
        self.assertNotIn("for 30 minutes", self.log())

    def test_blocked_wins_over_background_work(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["blocked"]
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "blocked")
        self.assertIsNone(out["agents"]["hrtest-claude-opus"]["background"])

    def test_unknown_with_a_live_wrapper_stays_working(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["unknown"]    # herdr cannot tell what the agent does
        out = self.r.wait(agent="hrtest-claude-opus")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "working")
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["background"], "running its command: go test ./...")

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

    def test_a_failed_agent_keeps_no_background_work(self):
        self.busy("hrtest-claude-opus")
        self.herdr.agent_status["hrtest-claude-opus"] = ["done"]
        self.r.wait(agent="hrtest-claude-opus")
        self.r.fail("hrtest-claude-opus", "the user took it off")
        a = self.r.status.agent("hrtest-claude-opus")
        self.assertEqual((a["state"], a["reason"], a.get("background")), ("failed", "the user took it off", None))
        self.assertIsNone(self.r._summary("hrtest-claude-opus")["background"])

    def test_an_exited_agent_keeps_no_background_work(self):
        self.busy("hrtest-grok")                                       # its wrapper outlives it
        self.herdr.agent_status["hrtest-grok"] = ["done"]
        self.r.wait(agent="hrtest-grok")
        self.herdr.agent_status["hrtest-grok"] = ["gone"]
        out = self.r.wait(agent="hrtest-grok")
        self.assertEqual((out["agents"]["hrtest-grok"]["state"], out["agents"]["hrtest-grok"]["background"]), ("gone", None))
        self.assertEqual(out["agents"]["hrtest-grok"]["reason"], "agent exited")


if __name__ == "__main__":
    unittest.main()
