import unittest
from pathlib import Path

from herdr_review.runner import Runner, RunnerError
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_runner_start import RunnerBase, make_run


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


if __name__ == "__main__":
    unittest.main()
