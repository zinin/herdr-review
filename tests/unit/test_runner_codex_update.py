import json
import unittest
from unittest import mock

from herdr_review.herdr import HerdrResult
from herdr_review.runner import Runner, RunnerError
from herdr_review.status import RunStatus, StatusError
from tests.unit.fakeherdr import SHELL_SCREEN, FakeHerdr
from tests.unit.test_runner_start import RunnerBase, make_run
from tests.unit.test_runner_wait import FakeClock

UPDATE_MENU = (
    "Update available · 0.159.2 → 0.160.0\n"
    "› 1. Update now (runs installer)\n  2. Skip\n  3. Skip until next version\n"
    "enter continue · esc skip\n"
)
UPDATING = "Updating Codex via `installer`...\n==> Downloading Codex CLI\n"
UPDATED = UPDATING + "Codex CLI 0.160.0 installed successfully.\n🎉 Update ran successfully! Please restart Codex.\nuser@host$ "
CODEX_IDLE = "╭ OpenAI Codex (v0.160.0) ╮\n› Ask Codex to do anything\n"
REVIEW = "### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\nReady to merge: Yes\n"


class UpdatingCodex(FakeHerdr):
    """The startup menu Herdr calls idle, followed by an installer that exits to the shell."""

    def __init__(self, *, name="hrtest-codex", repeat=False, exit_during_start=False,
                 first_prompt_error="agent_prompt_stalled", still_updating=False, restart_error=None,
                 restarted_prompt_error=None, startup_screen=UPDATE_MENU, blocked_start=False, progress_lines=0):
        super().__init__()
        self.name = name
        self.starts = 0
        self.updated = UPDATING + "".join(f"installer progress {i}\n" for i in range(progress_lines)) + UPDATED[len(UPDATING):]
        self.repeat = repeat
        self.exit_during_start = exit_during_start
        self.first_prompt_error = first_prompt_error
        self.still_updating = still_updating
        self.restart_error = restart_error
        self.restarted_prompt_error = restarted_prompt_error
        self.startup_screen = startup_screen
        self.blocked_start = blocked_start
        self.panes = {}

    def agent_start(self, name, kind, pane, args, timeout_ms=300000):
        self.panes[name] = pane
        if name != self.name:
            return super().agent_start(name, kind, pane, args, timeout_ms)
        self.starts += 1
        if self.starts > 1 and self.restart_error:
            self.start_errors[name] = self.restart_error
        result = super().agent_start(name, kind, pane, args, timeout_ms)
        if not result.ok:
            return result
        if self.starts == 1 or self.repeat:
            self.screens[name] = self.startup_screen if self.starts == 1 else UPDATE_MENU
            self.append_pane_output(pane, self.screens[name])
            self.agent_status[name] = ["idle"]
            if self.blocked_start and self.starts == 1:
                self.agent_status[name] = ["blocked"]
                return HerdrResult(False, 1, error_code="agent_not_ready", message="startup menu is blocking")
            if self.exit_during_start:
                self.append_pane_output(pane, self.updated)
                self.agent_status[name] = ["gone"]
                return HerdrResult(False, 1, error_code="agent_start_failed", message="agent exited during startup")
        else:
            self.screens[name] = CODEX_IDLE
            self.append_pane_output(pane, CODEX_IDLE)
            self.agent_status[name] = ["idle"]
            if self.restarted_prompt_error:
                self.prompt_errors[name] = self.restarted_prompt_error
        return result

    def agent_prompt(self, name, text, until=None, timeout_ms=None):
        if name == self.name and (self.starts == 1 or self.repeat):
            code = self.first_prompt_error if self.starts == 1 else "agent_prompt_stalled"
            if code:
                self.prompt_errors[name] = (code, "no activity after the startup menu")
            result = super().agent_prompt(name, text, until, timeout_ms)
            pane = self.panes[name]
            self.screens[name] = UPDATING
            self.append_pane_output(pane, UPDATING if self.still_updating else self.updated)
            self.agent_status[name] = ["idle" if self.still_updating else "gone"]
            return result
        result = super().agent_prompt(name, text, until, timeout_ms)
        if result.ok:
            self.agent_status[name] = ["working"]
        return result


class CodexUpdateTest(RunnerBase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.wall_clock = FakeClock()
        self.wall_clock.t = 1_800_000_000.0

    def runner(self, run_dir):
        return Runner(run_dir, herdr=self.herdr, poll_sec=5, clock=self.clock, sleep=self.clock.sleep, wall_clock=self.wall_clock)

    def test_verbose_update_exits_recover_from_pane_history(self):
        for layout in ("tabs", "grid"):
            for command in ("wait", "collect"):
                with self.subTest(layout=layout, command=command):
                    run_dir, _ = self.start_update(herdr=UpdatingCodex(progress_lines=400), layout=layout,
                                                   root=self.root / f"{layout}-{command}")
                    out = getattr(self.runner(run_dir), command)()
                    self.assertEqual(out["pending"], ["hrtest-codex"])
                    a = RunStatus.load(run_dir).agent("hrtest-codex")
                    self.assertEqual(a["state"], "working")
                    self.assertEqual((a["codex_launch_generation"], a["update_restarts"]), (1, 1))
                    self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
                    self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_verbose_fast_startup_update_exits_recover_from_pane_history(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(exit_during_start=True, progress_lines=400))
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["state"], "working")
        self.assertEqual((a["codex_launch_generation"], a["update_restarts"]), (1, 1))
        self.assertTrue(a["prompted"])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)
        self.assertEqual(runner.status.agent("hrtest-codex"), a)

    def test_running_update_fails_at_600_seconds_without_spending_budgets(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True))
        name = "hrtest-codex"
        calls = {kind: len(self.herdr.calls_named(kind)) for kind in ("agent_start", "agent_prompt", "agent_send_keys")}
        budgets = {key: runner.status.agent(name).get(key, 0) for key in ("retries", "collect_retries", "update_restarts")}
        self.wall_clock.t += 599
        runner._observe(name)
        self.assertEqual(runner.status.agent(name)["state"], "prompt_stalled")
        self.wall_clock.t += 1
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"][name]["state"], "failed")
        self.assertEqual(out["pending"], [])
        a = RunStatus.load(run_dir).agent(name)
        self.assertEqual(a["reason"], "Codex startup update did not finish within 600 seconds")
        self.assertFalse(a["codex_update_pending"])
        self.assertEqual(a["last_screen"], SHELL_SCREEN + UPDATE_MENU + UPDATING)
        self.assertEqual({key: a.get(key, 0) for key in budgets}, budgets)
        self.assertEqual({kind: len(self.herdr.calls_named(kind)) for kind in calls}, calls)

    def start_update(self, *, layout="tabs", herdr=None, reviewers=("codex",), root=None):
        self.herdr = herdr or UpdatingCodex()
        self.herdr.tab_labels["w1:t1"] = "rv-hrtest: orch"
        run_dir = make_run(root or self.root, self.repo, layout=layout, reviewers=reviewers)
        runner = self.runner(run_dir)
        runner.start_reviewers()
        return run_dir, runner

    def test_changing_screens_prompt_and_collect_keep_the_original_deadline(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True))
        name = "hrtest-codex"
        a = runner.status.agent(name)
        a.update(retries=2, collect_retries=1)
        runner.status.mark_agent(name)
        runner.status.save()
        deadline = self.wall_clock() + 600
        calls = {kind: len(self.herdr.calls_named(kind)) for kind in ("agent_start", "agent_prompt", "agent_send_keys")}
        for i in range(3):
            self.wall_clock.t += 199
            self.herdr.screens[name] = f"Downloading package {i}: {i * 25}%\n"
            self.herdr.append_pane_output("w1:p2", self.herdr.screens[name])
            self.runner(run_dir)._observe(name)
            self.assertTrue(self.runner(run_dir).prompt(name, retry=True)["codex_update_pending"])
            self.assertEqual(self.runner(run_dir).collect()["pending"], [name])
            self.assertEqual(RunStatus.load(run_dir).agent(name).get("codex_update_deadline"), deadline)
        self.wall_clock.t += 3
        self.herdr.append_pane_output("w1:p2", "TOKEN=s3cret\ninstaller still running\n")
        runner = self.runner(run_dir)
        out = runner.collect()
        self.assertEqual(out["failed"], {name: "Codex startup update did not finish within 600 seconds"})
        self.assertEqual(out["pending"], [])
        a = RunStatus.load(run_dir).agent(name)
        self.assertEqual((a["retries"], a["collect_retries"], a.get("update_restarts", 0)), (2, 1, 0))
        self.assertEqual(a["last_screen"], self.herdr.pane_screens["w1:p2"].replace("s3cret", "***"))
        self.assertEqual({kind: len(self.herdr.calls_named(kind)) for kind in calls}, calls)

    def test_a_success_banner_on_a_live_installer_expires_in_wait_and_collect(self):
        for command in ("wait", "collect"):
            with self.subTest(command=command):
                run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True), root=self.root / command)
                self.herdr.screens["hrtest-codex"] = UPDATED
                self.herdr.append_pane_output("w1:p2", UPDATED)
                self.wall_clock.t += 600
                out = getattr(self.runner(run_dir), command)()
                self.assertEqual(out["pending"], [])
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["state"], "failed")
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(a["last_screen"], SHELL_SCREEN + UPDATE_MENU + UPDATING + UPDATED)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
                self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)
                self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_an_installer_observed_by_start_opens_the_timer_without_prompting(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(startup_screen=UPDATING, still_updating=True))
        self.assertEqual(runner.status.agent("hrtest-codex").get("codex_update_deadline"), self.wall_clock() + 600)
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        self.wall_clock.t += 600
        self.assertEqual(self.runner(run_dir).wait()["agents"]["hrtest-codex"]["state"], "failed")
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])

    def test_legacy_pending_status_initializes_once_on_the_first_current_observation(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        path = run_dir / "status.json"
        legacy = json.loads(path.read_text())
        legacy["agents"]["hrtest-codex"].pop("codex_update_deadline", None)
        path.write_text(json.dumps(legacy))
        self.wall_clock.t += 100
        self.herdr.screens["hrtest-codex"] = "Downloading standalone package: 75%\n"
        runner = self.runner(run_dir)
        runner._observe("hrtest-codex")
        deadline = self.wall_clock() + 600
        self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex").get("codex_update_deadline"), deadline)
        self.wall_clock.t = deadline - 1
        self.assertEqual(self.runner(run_dir).collect()["pending"], ["hrtest-codex"])
        self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex")["codex_update_deadline"], deadline)
        self.wall_clock.t = deadline
        self.assertIn("hrtest-codex", self.runner(run_dir).collect()["failed"])

    def test_an_idle_update_choice_has_no_timer_until_the_installer_runs(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(blocked_start=True, still_updating=True))
        self.wall_clock.t += 1_000
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked-start")
        self.assertEqual(self.runner(run_dir).collect()["pending"], ["hrtest-codex"])
        self.assertIsNone(RunStatus.load(run_dir).agent("hrtest-codex").get("codex_update_deadline"))
        self.herdr.agent_status["hrtest-codex"] = ["idle"]
        self.runner(run_dir).prompt("hrtest-codex")
        self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex").get("codex_update_deadline"), self.wall_clock() + 600)

    def test_live_blocked_dialogs_survive_the_deadline_and_keep_it_when_resolved(self):
        for state in ("blocked", "blocked-start"):
            with self.subTest(state=state):
                run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True), root=self.root / state)
                deadline = self.wall_clock() + 600
                runner.status.set_agent_state("hrtest-codex", state, reason="installer needs authentication")
                self.herdr.screens["hrtest-codex"] = UPDATING + "Authentication required\n"
                self.herdr.agent_status["hrtest-codex"] = ["blocked"]
                calls = {kind: len(self.herdr.calls_named(kind)) for kind in ("agent_start", "agent_prompt", "agent_send_keys")}
                self.wall_clock.t += 1_000
                self.assertEqual(self.runner(run_dir).wait()["agents"]["hrtest-codex"]["state"], state)
                self.assertEqual(self.runner(run_dir).collect()["pending"], ["hrtest-codex"])
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["state"], state)
                self.assertTrue(a["codex_update_pending"])
                self.assertEqual(a.get("codex_update_deadline"), deadline)
                self.herdr.agent_status["hrtest-codex"] = ["idle"]
                self.herdr.screens["hrtest-codex"] = "Installing package...\n"
                self.assertIn("hrtest-codex", self.runner(run_dir).collect()["failed"])
                self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex")["codex_update_deadline"], deadline)
                self.assertEqual({kind: len(self.herdr.calls_named(kind)) for kind in calls}, calls)

    def test_session_metadata_and_normal_tui_close_the_timer_before_timeout(self):
        for command in ("wait", "collect"):
            for proof in ("native", "tui"):
                with self.subTest(command=command, proof=proof):
                    run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True), root=self.root / f"{command}-{proof}")
                    self.wall_clock.t += 600
                    self.herdr.agent_status["hrtest-codex"] = ["working"]
                    if proof == "tui":
                        self.herdr.screens["hrtest-codex"] = CODEX_IDLE
                        getattr(self.runner(run_dir), command)()
                    else:
                        original_get = self.herdr.agent_get

                        def get(name):
                            result = original_get(name)
                            if result.ok:
                                result.result["agent"]["agent_session"] = {"agent": "codex", "kind": "id", "value": "native-session"}
                            return result

                        with mock.patch.object(self.herdr, "agent_get", side_effect=get):
                            getattr(self.runner(run_dir), command)()
                    a = RunStatus.load(run_dir).agent("hrtest-codex")
                    self.assertEqual(a["state"], "working")
                    self.assertTrue(a["codex_startup_closed"])
                    self.assertFalse(a["codex_update_pending"])
                    self.assertIsNone(a.get("codex_update_deadline"))
                    self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
                    self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_fresh_successful_exit_has_priority_before_and_at_the_deadline(self):
        for command in ("wait", "collect"):
            for elapsed in (599, 600):
                with self.subTest(command=command, elapsed=elapsed):
                    run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True), root=self.root / f"{command}-{elapsed}")
                    self.wall_clock.t += elapsed
                    self.herdr.append_pane_output("w1:p2", UPDATED)
                    self.herdr.agent_status["hrtest-codex"] = ["gone"]
                    getattr(self.runner(run_dir), command)()
                    a = RunStatus.load(run_dir).agent("hrtest-codex")
                    self.assertEqual(a["state"], "working")
                    self.assertEqual(a["update_restarts"], 1)
                    self.assertIsNone(a.get("codex_update_deadline"))
                    self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
                    self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_a_recovered_generation_opens_its_own_new_timer(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True, repeat=True))
        name = "hrtest-codex"
        old_deadline = self.wall_clock() + 600
        self.wall_clock.t += 599
        self.herdr.append_pane_output("w1:p2", UPDATED)
        self.herdr.agent_status[name] = ["gone"]
        self.runner(run_dir)._observe(name)
        a = RunStatus.load(run_dir).agent(name)
        self.assertEqual(a["codex_launch_generation"], 1)
        self.assertEqual(a.get("codex_update_deadline"), self.wall_clock() + 600)
        self.wall_clock.t = old_deadline
        self.assertEqual(self.runner(run_dir).collect()["pending"], [name])
        self.wall_clock.t = a["codex_update_deadline"]
        self.assertIn(name, self.runner(run_dir).collect()["failed"])
        a = RunStatus.load(run_dir).agent(name)
        self.assertEqual((a["retries"], a["collect_retries"], a["update_restarts"]), (0, 0, 1))
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_an_ordinary_update_exit_keeps_failure_diagnostics_at_the_deadline(self):
        for command in ("wait", "collect"):
            with self.subTest(command=command):
                run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True), root=self.root / command)
                self.wall_clock.t += 600
                self.herdr.append_pane_output("w1:p2", "Error: download failed\nuser@host$ ")
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                getattr(self.runner(run_dir), command)()
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["state"], "gone")
                self.assertEqual(a["reason"], "agent exited")
                self.assertEqual(a["last_screen"], SHELL_SCREEN + UPDATE_MENU + UPDATING + "Error: download failed\nuser@host$ ")
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(a.get("update_restarts", 0), 0)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_collect_checks_live_blocking_before_expiring_a_stale_blocked_state(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True))
        runner.status.set_agent_state("hrtest-codex", "blocked", reason="old approval dialog")
        self.herdr.agent_status["hrtest-codex"] = ["idle"]
        self.wall_clock.t += 600
        self.assertIn("hrtest-codex", self.runner(run_dir).collect()["failed"])

    def test_timeout_preserves_the_latest_available_diagnostic_if_the_pane_is_unreadable(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        self.herdr.pane_screens["w1:p2"] = None
        self.herdr.screens["hrtest-codex"] = UPDATING + "TOKEN=s3cret\n"
        self.wall_clock.t += 600
        self.runner(run_dir).collect()
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["state"], "failed")
        self.assertEqual(a["last_screen"], UPDATING + "TOKEN=***\n")

    def start_stalled_with_history(self, history, *, layout="tabs", screen="Starting Codex...\n", root=None):
        self.herdr = FakeHerdr()
        self.herdr.tab_labels["w1:t1"] = "rv-hrtest: orch"
        self.herdr.pane_screens["w1:p2"] = history
        self.herdr.screens["hrtest-codex"] = screen
        self.herdr.prompt_errors["hrtest-codex"] = ("agent_prompt_stalled", "startup stalled")
        original_start = self.herdr.agent_start
        launches = 0

        def start(name, kind, pane, args, timeout_ms=300000):
            nonlocal launches
            launches += 1
            result = original_start(name, kind, pane, args, timeout_ms)
            if launches > 1:
                self.herdr.screens[name] = CODEX_IDLE
                self.herdr.agent_status[name] = ["working"]
                self.herdr.append_pane_output(pane, CODEX_IDLE)
            return result

        self.herdr.agent_start = start
        run_dir = make_run(root or self.root, self.repo, layout=layout, reviewers=("codex",))
        runner = self.runner(run_dir)
        runner.start_reviewers()
        self.assertEqual(runner.status.agent("hrtest-codex")["state"], "prompt_stalled")
        self.assertTrue(runner.status.agent("hrtest-codex")["codex_startup_pending"])
        return run_dir, runner

    def test_unobserved_update_in_scrollback_does_not_recover_a_stalled_start(self):
        for layout in ("tabs", "grid"):
            with self.subTest(layout=layout):
                history = SHELL_SCREEN + UPDATED
                run_dir, runner = self.start_stalled_with_history(history, layout=layout, root=self.root / layout)
                self.assertFalse(runner.status.agent("hrtest-codex")["codex_update_seen"])
                failure = "\nStarting Codex...\nFatal: current startup failed\nuser@host$ "
                self.herdr.append_pane_output("w1:p2", failure)
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
                self.assertEqual(out["pending"], [])
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["last_screen"], history + failure)
                self.assertEqual(a["reason"], "agent exited")
                self.assertFalse(a["codex_update_seen"])
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(a.get("update_restarts", 0), 0)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_an_old_success_marker_cannot_complete_a_current_installer(self):
        for layout in ("tabs", "grid"):
            with self.subTest(layout=layout):
                self.herdr = UpdatingCodex(still_updating=True)
                history = SHELL_SCREEN + UPDATED + "\n"
                self.herdr.pane_screens["w1:p2"] = history
                run_dir = make_run(self.root / layout, self.repo, layout=layout, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                self.assertTrue(runner.status.agent("hrtest-codex")["codex_update_seen"])
                failure = "Error: current download failed\nuser@host$ "
                self.herdr.append_pane_output("w1:p2", failure)
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["last_screen"], history + UPDATE_MENU + UPDATING + failure)
                self.assertEqual(a.get("update_restarts", 0), 0)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_an_old_installer_banner_cannot_authorize_a_fresh_success_marker(self):
        history = SHELL_SCREEN + UPDATING
        run_dir, _ = self.start_stalled_with_history(history)
        marker = "🎉 Update ran successfully! Please restart Codex.\nuser@host$ "
        self.herdr.append_pane_output("w1:p2", marker)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertFalse(a["codex_update_seen"])
        self.assertEqual(a["last_screen"], history + marker)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_long_prelaunch_history_is_excluded_from_verbose_update_evidence(self):
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                history = "prior secret: s3cret\n" + SHELL_SCREEN + UPDATED + "".join(f"past line {i}\n" for i in range(400))
                run_dir, _ = self.start_stalled_with_history(history, screen=UPDATE_MENU, root=self.root / str(fresh))
                self.herdr.screens["hrtest-codex"] = CODEX_IDLE
                self.herdr.prompt_errors.pop("hrtest-codex", None)
                suffix = UPDATED[len(UPDATING):] if fresh else "Error: download failed\nuser@host$ "
                output = UPDATING + "".join(f"current progress {i}\n" for i in range(400)) + suffix
                self.herdr.append_pane_output("w1:p2", output)
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                self.runner(run_dir).wait()
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["state"], "working" if fresh else "gone")
                self.assertEqual(a.get("update_restarts", 0), 1 if fresh else 0)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 2 if fresh else 1)
                self.assertNotIn("s3cret", (run_dir / "status.json").read_text())

    def test_a_success_marker_split_across_the_baseline_is_not_fresh(self):
        history = SHELL_SCREEN + UPDATING + "🎉 Update ran successfully!"
        run_dir, _ = self.start_stalled_with_history(history, screen=UPDATE_MENU)
        fragment = " Please restart Codex.\nFatal: current startup failed\n"
        self.herdr.append_pane_output("w1:p2", fragment)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex")["last_screen"], history + fragment)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_an_unchanged_pane_baseline_cannot_authorize_a_restart(self):
        history = SHELL_SCREEN + UPDATED
        run_dir, _ = self.start_stalled_with_history(history, screen=UPDATE_MENU)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["last_screen"], history)
        self.assertEqual(a.get("update_restarts", 0), 0)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_unreadable_missing_or_wrong_generation_baselines_fail_closed(self):
        for case in ("unreadable", "missing", "missing-generation", "old-generation"):
            with self.subTest(case=case):
                history = None if case == "unreadable" else SHELL_SCREEN
                run_dir, runner = self.start_stalled_with_history(history, screen=UPDATE_MENU, root=self.root / case)
                a = runner.status.agent("hrtest-codex")
                if case == "missing":
                    a.pop("codex_pane_baseline", None)
                    a.pop("codex_pane_baseline_generation", None)
                elif case == "missing-generation":
                    a.pop("codex_pane_baseline_generation", None)
                elif case == "old-generation":
                    a["codex_pane_baseline_generation"] = -1
                runner.status.mark_agent("hrtest-codex")
                runner.status.save()
                self.herdr.append_pane_output("w1:p2", UPDATED)
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
                a = RunStatus.load(run_dir).agent("hrtest-codex")
                self.assertEqual(a["last_screen"], (history or "") + UPDATED)
                self.assertEqual(a.get("update_restarts", 0), 0)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_truncated_or_ambiguous_pane_alignment_fails_closed(self):
        repeated = "duplicate-a\nduplicate-b\nduplicate-c\n"
        cases = (
            ("ambiguous", "discarded-header\n" + repeated * 2, "new output\n" * 30 + UPDATED),
            ("too-short", "old-a\nold-b\nlast-anchor\n", "new output\n" * 34 + UPDATED),
            ("no-overlap", "old-a\nold-b\nold-c\n", "new output\n" * 40 + UPDATED),
        )
        for case, history, appended in cases:
            with self.subTest(case=case):
                run_dir, _ = self.start_stalled_with_history(history, screen=UPDATE_MENU, root=self.root / case)
                self.herdr.append_pane_output("w1:p2", appended)
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                snapshot = "".join((history + appended).splitlines(keepends=True)[-40:])
                self.herdr.pane_screens["w1:p2"] = snapshot  # the host discarded older rows
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
                self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex")["last_screen"], snapshot)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_a_unique_retained_baseline_suffix_proves_a_fresh_success(self):
        for layout in ("tabs", "grid"):
            with self.subTest(layout=layout):
                history = SHELL_SCREEN + UPDATED + "\n" + "".join(f"retained-{i}\n" for i in range(5))
                run_dir, _ = self.start_stalled_with_history(history, screen=UPDATE_MENU, layout=layout, root=self.root / layout)
                appended = "new output\n" * 30 + UPDATED
                self.herdr.append_pane_output("w1:p2", appended)
                self.herdr.pane_screens["w1:p2"] = "".join((history + appended).splitlines(keepends=True)[-40:])
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
                self.assertEqual(out["pending"], ["hrtest-codex"])
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
                self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_fresh_success_after_old_scrollback_is_recovered_in_both_layouts(self):
        for layout in ("tabs", "grid"):
            with self.subTest(layout=layout):
                history = SHELL_SCREEN + UPDATED + "\n"
                run_dir, _ = self.start_stalled_with_history(history, layout=layout, root=self.root / layout)
                self.herdr.append_pane_output("w1:p2", UPDATING + "fresh download finished\n" + "🎉 Update ran successfully! Please restart Codex.\n")
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
                self.assertEqual(out["pending"], ["hrtest-codex"])
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
                self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_old_scrollback_cannot_recover_an_ordinary_agent_start_failure(self):
        self.herdr.pane_screens["w1:p2"] = SHELL_SCREEN + UPDATED + "\n"
        self.herdr.start_errors["hrtest-codex"] = ("agent_start_failed", "ordinary startup error")
        original_start = self.herdr.agent_start
        failure = "Fatal: current startup failed\n"

        def start(name, kind, pane, args, timeout_ms=300000):
            result = original_start(name, kind, pane, args, timeout_ms)
            self.herdr.append_pane_output(pane, failure)
            self.herdr.agent_status[name] = ["gone"]
            return result

        self.herdr.agent_start = start
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        runner = self.runner(run_dir)
        out = runner.start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "failed")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["last_screen"], SHELL_SCREEN + UPDATED + "\n" + failure)
        self.assertIn("ordinary startup error", a["reason"])
        self.assertEqual(a.get("update_restarts", 0), 0)

    def test_a_fast_update_exit_with_an_unreadable_baseline_is_failed(self):
        self.herdr = UpdatingCodex(exit_during_start=True)
        self.herdr.pane_screens["w1:p2"] = None
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        runner = self.runner(run_dir)
        out = runner.start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "failed")
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["last_screen"], UPDATE_MENU + UPDATED)
        self.assertEqual(a.get("update_restarts", 0), 0)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_fast_fresh_update_exit_after_old_history_is_recovered_in_both_layouts(self):
        for layout in ("tabs", "grid"):
            with self.subTest(layout=layout):
                self.herdr = UpdatingCodex(exit_during_start=True)
                history = SHELL_SCREEN + UPDATED + "\n"
                self.herdr.pane_screens["w1:p2"] = history
                run_dir = make_run(self.root / layout, self.repo, layout=layout, reviewers=("codex",))
                out = self.runner(run_dir).start_reviewers()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
                self.assertEqual(out["agents"]["hrtest-codex"]["update_restarts"], 1)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
                self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_each_launch_baseline_is_persisted_before_start_and_rebound_after_recovery(self):
        self.herdr = UpdatingCodex()
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        original_start = self.herdr.agent_start
        recorded = []

        def start(name, kind, pane, args, timeout_ms=300000):
            a = RunStatus.load(run_dir).agent(name)
            recorded.append((a["codex_launch_generation"], a.get("codex_pane_baseline_generation"), a.get("codex_pane_baseline")))
            return original_start(name, kind, pane, args, timeout_ms)

        self.herdr.agent_start = start
        runner = self.runner(run_dir)
        runner.start_reviewers()
        runner.wait()
        self.assertEqual(recorded, [(0, 0, SHELL_SCREEN), (1, 1, SHELL_SCREEN + UPDATE_MENU + UPDATED)])
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["codex_pane_baseline_generation"], 1)
        self.assertEqual(a["codex_pane_baseline"], SHELL_SCREEN + UPDATE_MENU + UPDATED)
        self.assertEqual(self.herdr.calls_named("agent_start")[0], self.herdr.calls_named("agent_start")[1])
        self.assertEqual(len(self.herdr.calls_named("tab_create")), 1)
        self.assertEqual([p[2] for p in self.herdr.calls_named("agent_prompt")], [f"Read {run_dir}/prompts/codex.md and follow it exactly."] * 2)
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_persisted_baselines_are_masked_and_still_prove_fresh_output(self):
        self.herdr = UpdatingCodex()
        self.herdr.pane_screens["w1:p2"] = "prior secret: s3cret\n" + SHELL_SCREEN
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        runner = self.runner(run_dir)
        runner.start_reviewers()
        self.assertEqual(runner.status.agent("hrtest-codex").get("codex_pane_baseline"), "prior secret: ***\n" + SHELL_SCREEN)
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertNotIn("s3cret", (run_dir / "status.json").read_text())

    def test_a_readable_empty_baseline_can_prove_fresh_update_output(self):
        run_dir, runner = self.start_stalled_with_history("", screen=UPDATE_MENU)
        self.assertEqual(runner.status.agent("hrtest-codex")["codex_pane_baseline"], "")
        self.herdr.append_pane_output("w1:p2", UPDATED)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["codex_pane_baseline_generation"], 1)
        self.assertEqual(a["codex_pane_baseline"], UPDATED)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_an_unreadable_visible_screen_does_not_promote_old_pane_history(self):
        history = SHELL_SCREEN + UPDATED
        run_dir, runner = self.start_stalled_with_history(history)
        self.herdr.reads["hrtest-codex"] = [None]
        runner.wait()
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertFalse(a["codex_update_seen"])
        self.assertFalse(a["codex_update_pending"])
        self.herdr.append_pane_output("w1:p2", "\nFatal: current startup failed\n")
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
        self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex")["last_screen"], history + "\nFatal: current startup failed\n")

    def test_successful_update_is_restarted_and_the_review_is_collected(self):
        run_dir, _ = self.start_update()
        runner = self.runner(run_dir)  # every CLI call reloads the status
        out = runner.wait()
        agent = runner.status.agent("hrtest-codex")
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        self.assertEqual(out["agents"]["hrtest-codex"]["update_restarts"], 1)
        self.assertEqual((agent["retries"], agent["collect_retries"]), (0, 0))
        self.assertIsNone(agent["reason"])
        self.assertFalse(agent["codex_update_pending"])
        starts = self.herdr.calls_named("agent_start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0], starts[1])
        self.assertEqual(len(self.herdr.calls_named("tab_create")), 1)
        prompts = self.herdr.calls_named("agent_prompt")
        self.assertEqual([p[2] for p in prompts], [f"Read {run_dir}/prompts/codex.md and follow it exactly."] * 2)
        self.assertIn("restarting after successful Codex update", (run_dir / "runner.log").read_text())
        self.herdr.agent_status["hrtest-codex"] = ["done"]
        (run_dir / "reviews" / "codex.md").write_text(REVIEW)
        runner.wait()
        self.assertEqual(self.runner(run_dir).collect()["collected"], ["hrtest-codex"])

    def test_grid_recovery_reuses_the_same_pane(self):
        run_dir, _ = self.start_update(layout="grid")
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(self.herdr.calls_named("agent_start")[0], self.herdr.calls_named("agent_start")[1])
        self.assertEqual(len(self.herdr.calls_named("pane_split")), 1)
        self.assertEqual(self.herdr.calls_named("tab_create"), [])

    def test_an_update_that_exits_during_agent_start_is_recovered(self):
        _, runner = self.start_update(herdr=UpdatingCodex(exit_during_start=True))
        self.assertEqual(runner.status.agent("hrtest-codex")["state"], "working")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_an_update_that_exits_during_the_prompt_is_recovered(self):
        _, runner = self.start_update(herdr=UpdatingCodex(first_prompt_error="agent_not_found"))
        self.assertEqual(runner.status.agent("hrtest-codex")["state"], "working")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_late_update_screen_is_detected_after_the_first_prompt_stalls(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(startup_screen="Starting Codex...\n"))
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_blocking_update_menu_is_left_for_the_orchestrator(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(blocked_start=True))
        out = self.runner(run_dir).wait()
        self.assertEqual(out["reason"], "blocked")
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked-start")
        self.assertFalse(out["agents"]["hrtest-codex"]["codex_update_pending"])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_a_blocked_installer_is_reported_for_normal_dialog_handling(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        self.herdr.agent_status["hrtest-codex"] = ["blocked"]
        self.herdr.screens["hrtest-codex"] = "Authentication required\n"
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked")
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_a_running_installer_replaces_stale_blocking_states(self):
        for state in ("blocked-start", "blocked"):
            for live in ("idle", "working"):
                with self.subTest(state=state, live=live):
                    self.herdr = UpdatingCodex(blocked_start=True)
                    run_dir = make_run(self.root / f"{state}-{live}", self.repo, reviewers=("codex",))
                    runner = self.runner(run_dir)
                    runner.start_reviewers()
                    runner.status.set_agent_state("hrtest-codex", state, reason="old permission dialog")
                    self.herdr.screens["hrtest-codex"] = UPDATING
                    self.herdr.agent_status["hrtest-codex"] = [live]
                    runner = self.runner(run_dir)
                    out = runner.wait()
                    self.assertEqual(out["reason"], "state_change")
                    self.assertEqual(out["agents"]["hrtest-codex"]["state"], "prompt_stalled")
                    self.assertEqual(out["agents"]["hrtest-codex"]["reason"], "Codex startup update is running")
                    self.assertTrue(out["agents"]["hrtest-codex"]["codex_update_pending"])
                    self.assertEqual(out["pending"], ["hrtest-codex"])
                    self.assertFalse(out["settled"])
                    self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
                    self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_a_running_installer_can_block_on_a_permission_dialog_again(self):
        for state in ("blocked-start", "blocked"):
            with self.subTest(state=state):
                self.herdr = UpdatingCodex(blocked_start=True)
                run_dir = make_run(self.root / state, self.repo, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                runner.status.set_agent_state("hrtest-codex", state, reason="old permission dialog")
                self.herdr.screens["hrtest-codex"] = UPDATING
                self.herdr.agent_status["hrtest-codex"] = ["working"]
                self.assertEqual(runner.wait()["agents"]["hrtest-codex"]["state"], "prompt_stalled")
                self.herdr.screens["hrtest-codex"] = UPDATING + "Authentication required\n"
                self.herdr.agent_status["hrtest-codex"] = ["blocked"]
                out = runner.wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked")
                self.assertTrue(out["agents"]["hrtest-codex"]["codex_update_pending"])
                self.assertEqual(out["pending"], ["hrtest-codex"])
                self.assertEqual(runner.wait()["reason"], "blocked")
                self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
                self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_native_startup_metadata_closes_eligibility_despite_a_stale_menu(self):
        self.herdr.screens["hrtest-codex"] = UPDATE_MENU
        original_start = self.herdr.agent_start

        def start(name, kind, pane, args, timeout_ms=300000):
            result = original_start(name, kind, pane, args, timeout_ms)
            result.result["agent"]["agent_session"] = {
                "agent": "codex", "kind": "id", "source": "herdr:codex", "value": "native-review-session",
            }
            return result

        self.herdr.agent_start = start
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        runner = self.runner(run_dir)
        runner.start_reviewers()
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        out = runner.wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_manual_revival_closes_eligibility_for_every_prompt_error(self):
        for i, code in enumerate(("agent_blocked", "agent_prompt_stalled", "herdr_failed", "timeout")):
            with self.subTest(code=code):
                self.herdr = UpdatingCodex(restarted_prompt_error=(code, "manual prompt failed"))
                run_dir = make_run(self.root / str(i), self.repo, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                self.herdr.pane_screens["w1:p2"] = None
                runner.wait()
                self.herdr.agent_start("hrtest-codex", "codex", "w1:p2", ["-m", "gpt-5.5"])
                self.runner(run_dir).prompt("hrtest-codex")
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                self.herdr.append_pane_output("w1:p2", UPDATED)
                out = self.runner(run_dir).wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_manual_revival_preserves_terminal_diagnostics_until_the_prompt_result(self):
        for state in ("gone", "failed"):
            for code in ("herdr_failed", "herdr_not_found"):
                with self.subTest(state=state, code=code):
                    self.herdr = UpdatingCodex()
                    run_dir = make_run(self.root / f"{state}-{code}", self.repo, reviewers=("codex",))
                    runner = self.runner(run_dir)
                    runner.start_reviewers()
                    a = runner.status.agent("hrtest-codex")
                    a["update_restarts"] = 1
                    runner.status.set_agent_state("hrtest-codex", state, reason="old startup failure", last_screen="old failure screen\n")
                    self.herdr.first_prompt_error = code
                    self.herdr.pane_screens["w1:p2"] = None
                    original_prompt = self.herdr.agent_prompt
                    diagnostics_at_prompt = []

                    def prompt(name, text, until=None, timeout_ms=None):
                        current = self.runner(run_dir).status.agent(name)
                        diagnostics_at_prompt.append((current["reason"], current["last_screen"]))
                        return original_prompt(name, text, until, timeout_ms)

                    self.herdr.agent_prompt = prompt
                    runner = self.runner(run_dir)
                    out = runner.prompt("hrtest-codex")
                    self.assertEqual(diagnostics_at_prompt, [("old startup failure", "old failure screen\n")])
                    self.assertEqual(out["state"], state)
                    self.assertIn(code, out["reason"])
                    a = self.runner(run_dir).status.agent("hrtest-codex")
                    self.assertEqual(a["last_screen"], "old failure screen\n")
                    self.assertEqual(a["update_restarts"], 1)
                    self.assertTrue(a["codex_startup_closed"])
                    self.assertFalse(a["codex_startup_pending"])
                    self.assertFalse(a["codex_update_seen"])
                    self.assertFalse(a["codex_update_pending"])
                    self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_failed_manual_revival_without_a_new_screen_keeps_terminal_last_screen(self):
        for state in ("gone", "failed"):
            with self.subTest(state=state):
                self.herdr = FakeHerdr()
                self.herdr.screens["hrtest-codex"] = UPDATE_MENU
                run_dir = make_run(self.root / state, self.repo, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                a = runner.status.agent("hrtest-codex")
                a["update_restarts"] = 1
                runner.status.set_agent_state("hrtest-codex", state, reason="old failure", last_screen="old failure screen\n")
                self.herdr.prompt_errors["hrtest-codex"] = ("agent_prompt_failed", "manual revival failed")
                self.herdr.pane_screens["w1:p2"] = None
                out = self.runner(run_dir).prompt("hrtest-codex")
                self.assertEqual(out["state"], "failed")
                self.assertIn("agent_prompt_failed", out["reason"])
                a = self.runner(run_dir).status.agent("hrtest-codex")
                self.assertEqual(a["last_screen"], "old failure screen\n")
                self.assertEqual(a["update_restarts"], 1)
                self.assertTrue(a["codex_startup_closed"])
                self.assertFalse(a["codex_startup_pending"])
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_successful_manual_revival_clears_obsolete_terminal_diagnostics(self):
        for state in ("gone", "failed"):
            with self.subTest(state=state):
                self.herdr = UpdatingCodex()
                run_dir = make_run(self.root / state, self.repo, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                a = runner.status.agent("hrtest-codex")
                a["update_restarts"] = 1
                runner.status.set_agent_state("hrtest-codex", state, reason="old failure", last_screen="old failure screen\n")
                self.herdr.agent_start("hrtest-codex", "codex", "w1:p2", ["-m", "gpt-5.5"])
                out = self.runner(run_dir).prompt("hrtest-codex")
                self.assertEqual(out["state"], "working")
                self.assertIsNone(out["reason"])
                a = self.runner(run_dir).status.agent("hrtest-codex")
                self.assertIsNone(a["last_screen"])
                self.assertEqual(a["update_restarts"], 1)
                self.assertTrue(a["codex_startup_closed"])
                self.assertFalse(a["codex_startup_pending"])
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_stale_runner_cannot_spend_the_update_budget_again(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(repeat=True, first_prompt_error=None))
        first, stale = self.runner(run_dir), self.runner(run_dir)
        first.wait()
        stale.wait()
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(self.runner(run_dir).status.agent("hrtest-codex")["update_restarts"], 1)

    def test_delayed_exit_observation_does_not_mark_the_restarted_agent_gone(self):
        self.herdr.screens["hrtest-codex"] = UPDATE_MENU
        self.herdr.prompt_errors["hrtest-codex"] = ("agent_prompt_stalled", "menu is still idle")
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.runner(run_dir).start_reviewers()
        first, stale = self.runner(run_dir), self.runner(run_dir)
        self.assertFalse(stale.status.agent("hrtest-codex")["codex_update_pending"])
        original_start = self.herdr.agent_start

        def start(name, kind, pane, args, timeout_ms=300000):
            result = original_start(name, kind, pane, args, timeout_ms)
            self.herdr.screens[name] = CODEX_IDLE
            self.herdr.agent_status[name] = ["working"]
            return result

        self.herdr.agent_start = start
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        original_get = self.herdr.agent_get
        delayed = True
        fresh_observations = []

        def get(name):
            nonlocal delayed
            result = original_get(name)
            if delayed:
                delayed = False
                self.assertEqual(result.error_code, "agent_not_found")
                first._observe(name)
                self.assertEqual(first.status.agent(name)["state"], "working")
            elif result.ok:
                fresh_observations.append(name)
            return result

        self.herdr.agent_get = get
        out = stale.wait()
        self.assertTrue(original_get("hrtest-codex").ok)
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        self.assertFalse(out["settled"])
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual((a["state"], a["update_restarts"]), ("working", 1))
        self.assertIsNone(a["reason"])
        self.assertTrue(fresh_observations)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_update_record_save_cannot_lose_the_exit_observation_generation(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(blocked_start=True))
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        first, stale = self.runner(run_dir), self.runner(run_dir)
        original_save = stale.status.save
        raced = False

        def save():
            nonlocal raced
            if not raced:
                raced = True
                first._observe("hrtest-codex")
                self.assertEqual(first.status.agent("hrtest-codex")["state"], "working")
            original_save()

        stale.status.save = save
        out = stale.wait()
        self.assertTrue(raced)
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual((a["state"], a["update_restarts"]), ("working", 1))
        self.assertIsNone(a["reason"])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_a_restart_claim_immediately_before_the_gone_write_preserves_the_live_generation(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        first, stale = self.runner(run_dir), self.runner(run_dir)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", "Error: old installation failed\n")
        original_set_state = stale.status.set_agent_state
        raced = False

        def set_state(name, state, *args, **kwargs):
            nonlocal raced
            if state == "gone" and not raced:
                raced = True
                self.herdr.append_pane_output("w1:p2", UPDATED)
                first._observe(name)
                self.assertEqual(first.status.agent(name)["state"], "working")
            return original_set_state(name, state, *args, **kwargs)

        stale.status.set_agent_state = set_state
        out = stale.wait()
        self.assertTrue(raced)
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual((a["state"], a["update_restarts"]), ("working", 1))
        self.assertIsNone(a["reason"])
        self.assertIsNone(a["last_screen"])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)
        self.assertNotIn(("tab_rename", "w1:t2", "rv-hrtest: codex ✗"), self.herdr.calls)

    def test_a_wait_screen_save_keeps_a_concurrently_started_installer(self):
        name = "hrtest-codex"
        run_dir, initial = self.start_update(herdr=UpdatingCodex(blocked_start=True, still_updating=True))
        initial.status.set_agent_state(name, "idle")
        self.herdr.agent_status[name] = ["idle"]
        first, stale = self.runner(run_dir), self.runner(run_dir)
        original_save = stale.status.save
        raced = False

        def save():
            nonlocal raced
            if not raced and stale.status.agent(name).get("screen_hash"):
                raced = True
                self.herdr.screens[name] = UPDATING
                first._observe(name)
            original_save()

        stale.status.save = save
        out = stale.wait()
        self.assertTrue(raced)
        self.assertFalse(out["settled"])
        self.assertEqual(out["pending"], [name])
        self.assertEqual(out["agents"][name]["state"], "prompt_stalled")
        self.assertTrue(out["agents"][name]["codex_update_pending"])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_a_stale_menu_collect_defers_a_concurrently_started_installer(self):
        name = "hrtest-codex"
        run_dir, initial = self.start_update(herdr=UpdatingCodex(blocked_start=True, still_updating=True))
        initial.status.set_agent_state(name, "idle")
        stale = self.runner(run_dir)
        self.herdr.screens[name] = UPDATING
        self.herdr.agent_status[name] = ["idle"]
        self.runner(run_dir)._observe(name)
        out = stale.collect()
        self.assertEqual(out["pending"], [name])
        self.assertEqual(out["failed"], {})
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        a = RunStatus.load(run_dir).agent(name)
        self.assertTrue(a["codex_update_pending"])
        self.assertFalse(a["prompted"])
        self.assertEqual((a["retries"], a["collect_retries"], a.get("update_restarts", 0)), (0, 0, 0))

    def test_wait_reobserves_responses_predating_concurrent_startup_progress(self):
        name = "hrtest-codex"
        for race_at in ("agent_get", "agent_read"):
            with self.subTest(race_at=race_at):
                herdr = FakeHerdr()
                herdr.screens[name] = UPDATING
                run_dir, _ = self.start_update(herdr=herdr, root=self.root / race_at)
                first, stale = self.runner(run_dir), self.runner(run_dir)
                original_get, original_read = self.herdr.agent_get, self.herdr.agent_read
                delayed = True

                def finish_startup():
                    self.herdr.screens[name] = CODEX_IDLE
                    self.herdr.agent_status[name] = ["idle"]
                    first._observe(name)
                    first.prompt(name)
                    self.herdr.agent_status[name] = ["working"]

                def get(name):
                    nonlocal delayed
                    result = original_get(name)
                    if delayed and race_at == "agent_get":
                        delayed = False
                        finish_startup()
                    return result

                def read(name, source="visible", lines=60):
                    nonlocal delayed
                    screen = original_read(name, source, lines)
                    if delayed and race_at == "agent_read":
                        delayed = False
                        finish_startup()
                    return screen

                self.herdr.agent_get, self.herdr.agent_read = get, read
                out = stale.wait()
                self.assertFalse(delayed)
                self.assertEqual(out["agents"][name]["state"], "working")
                self.assertEqual(out["pending"], [name])
                self.assertFalse(out["settled"])
                a = RunStatus.load(run_dir).agent(name)
                self.assertTrue(a["prompted"])
                self.assertTrue(a["codex_startup_closed"])
                self.assertEqual(stale.collect()["pending"], [name])
                self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_wait_keeps_a_concurrent_terminal_startup_result(self):
        name = "hrtest-codex"
        for state in ("failed", "collected"):
            with self.subTest(state=state):
                herdr = FakeHerdr()
                herdr.screens[name] = UPDATING
                run_dir, first = self.start_update(herdr=herdr, root=self.root / state)
                stale = self.runner(run_dir)
                if state == "collected":
                    first._close_codex_startup(name)
                    first.status.agent(name).update(prompted=True, result_ok=True)
                    (run_dir / "reviews" / "codex.md").write_text(REVIEW)
                first.status.agent(name)["codex_update_pending"] = False
                reason = "installation failed" if state == "failed" else None
                first.status.set_agent_state(name, state, reason=reason)
                self.herdr.screens[name] = CODEX_IDLE
                self.herdr.agent_status[name] = ["idle"]
                out = stale.wait()
                self.assertEqual(out["agents"][name]["state"], state)
                self.assertEqual(out["agents"][name]["reason"], reason)
                self.assertTrue(out["settled"])
                self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
                if state == "collected":
                    self.assertTrue(RunStatus.load(run_dir).agent(name)["result_ok"])

    def test_wait_applies_live_state_after_adopting_a_concurrent_startup_transition(self):
        name = "hrtest-codex"
        herdr = FakeHerdr()
        herdr.screens[name] = UPDATING
        run_dir, initial = self.start_update(herdr=herdr)
        initial.status.agent(name)["codex_update_pending"] = False
        initial.status.set_agent_state(name, "idle")
        first, stale = self.runner(run_dir), self.runner(run_dir)
        first._close_codex_startup(name)
        first.status.agent(name)["prompted"] = True
        first.status.set_agent_state(name, "working")
        self.herdr.screens[name] = CODEX_IDLE
        self.herdr.agent_status[name] = ["idle"]
        out = stale.wait()
        self.assertTrue(out["settled"])
        self.assertEqual(out["agents"][name]["state"], "idle")
        self.assertTrue(RunStatus.load(run_dir).agent(name)["prompted"])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])

    def test_an_inflight_successful_prompt_survives_concurrent_startup_closure(self):
        name = "hrtest-codex"
        herdr = FakeHerdr()
        herdr.screens[name] = UPDATING
        run_dir, initial = self.start_update(herdr=herdr)
        initial.status.agent(name)["codex_update_pending"] = False
        initial.status.set_agent_state(name, "idle")
        self.herdr.screens[name] = UPDATE_MENU
        first, stale = self.runner(run_dir), self.runner(run_dir)
        original_prompt = self.herdr.agent_prompt

        def prompt(name, text, until=None, timeout_ms=None):
            result = original_prompt(name, text, until, timeout_ms)
            self.herdr.screens[name] = CODEX_IDLE
            self.herdr.agent_status[name] = ["working"]
            first._observe(name)
            return result

        self.herdr.agent_prompt = prompt
        out = stale.prompt(name)
        self.assertEqual(out["state"], "working")
        a = RunStatus.load(run_dir).agent(name)
        self.assertTrue(a["prompted"])
        self.assertTrue(a["codex_startup_closed"])
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_a_wait_screen_save_keeps_a_concurrently_prompted_session(self):
        name = "hrtest-codex"
        herdr = FakeHerdr()
        herdr.screens[name] = UPDATING
        run_dir, _ = self.start_update(herdr=herdr)
        first, stale = self.runner(run_dir), self.runner(run_dir)
        self.assertFalse(stale.status.agent(name)["prompted"])
        original_save = stale.status.save
        raced = False

        def save():
            nonlocal raced
            if not raced and stale.status.agent(name).get("screen_hash"):
                raced = True
                self.herdr.screens[name] = CODEX_IDLE
                self.herdr.agent_status[name] = ["idle"]
                first._observe(name)
                first.prompt(name)
                self.herdr.agent_status[name] = ["working"]
            original_save()

        stale.status.save = save
        out = stale.wait()
        self.assertTrue(raced)
        self.assertEqual(out["agents"][name]["state"], "working")
        self.assertEqual(out["pending"], [name])
        a = RunStatus.load(run_dir).agent(name)
        self.assertTrue(a["prompted"])
        self.assertTrue(a["codex_startup_closed"])
        self.assertFalse(a["codex_update_pending"])
        self.assertIsNone(a["reason"])
        self.herdr.agent_status[name] = ["idle"]
        settled = self.runner(run_dir)
        self.assertTrue(settled.wait()["settled"])
        (run_dir / "reviews" / "codex.md").write_text(REVIEW)
        self.assertEqual(settled.collect()["collected"], [name])
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_commands_defer_during_claimed_restart_and_first_prompt(self):
        name = "hrtest-codex"
        for point in ("claim", "agent_start", "first_prompt"):
            for command in ("wait", "prompt", "collect"):
                with self.subTest(point=point, command=command):
                    run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True),
                                                   root=self.root / f"{point}-{command}")
                    self.herdr.agent_status[name] = ["gone"]
                    self.herdr.append_pane_output("w1:p2", UPDATED)
                    restarting = self.runner(run_dir)
                    original_claim = restarting.status.claim_codex_update_restart
                    original_start, original_read = self.herdr.agent_start, self.herdr.agent_read
                    original_send = restarting._send_review_prompt
                    raced = False

                    def observe_during_restart():
                        nonlocal raced
                        raced = True
                        prompts = len(self.herdr.calls_named("agent_prompt"))
                        gets = len(self.herdr.calls_named("agent_get"))
                        peer = self.runner(run_dir)
                        reply = peer.prompt(name) if command == "prompt" else getattr(peer, command)()
                        expected = "idle" if point == "first_prompt" else "starting"
                        self.assertEqual(peer.status.agent(name)["state"], expected)
                        if command == "prompt":
                            self.assertTrue(reply.get("codex_restart_pending"))
                        else:
                            self.assertEqual(reply["pending"], [name])
                        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), prompts)
                        self.assertEqual(len(self.herdr.calls_named("agent_get")), gets)
                        self.assertEqual((peer.status.agent(name)["retries"], peer.status.agent(name)["collect_retries"]), (0, 0))

                    def claim(agent):
                        claimed = original_claim(agent)
                        if claimed and point == "claim":
                            observe_during_restart()
                        return claimed

                    def start(*args, **kwargs):
                        if point == "agent_start":
                            observe_during_restart()
                        return original_start(*args, **kwargs)

                    def read(agent, source="visible", lines=60):
                        if self.herdr.agent_status[agent][0] == "gone":
                            return None
                        return original_read(agent, source, lines)

                    def send(agent):
                        if point == "first_prompt":
                            observe_during_restart()
                        original_send(agent)

                    restarting.status.claim_codex_update_restart = claim
                    self.herdr.agent_start, self.herdr.agent_read = start, read
                    restarting._send_review_prompt = send
                    out = restarting.wait()
                    self.assertTrue(raced)
                    self.assertEqual(out["agents"][name]["state"], "working")
                    self.assertEqual(out["pending"], [name])
                    self.assertNotIn("codex_restart_pending", out["agents"][name])
                    a = RunStatus.load(run_dir).agent(name)
                    self.assertEqual((a["codex_launch_generation"], a["update_restarts"]), (1, 1))
                    self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
                    self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_an_abandoned_claim_releases_startup_observers(self):
        name = "hrtest-codex"
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        self.herdr.agent_status[name] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        restarting = self.runner(run_dir)
        original_claim = restarting.status.claim_codex_update_restart

        def claim(agent):
            self.assertTrue(original_claim(agent))
            raise RuntimeError("restart owner stopped")

        restarting.status.claim_codex_update_restart = claim
        with self.assertRaisesRegex(RuntimeError, "restart owner stopped"):
            restarting.wait()
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"][name]["state"], "gone")
        self.assertTrue(out["settled"])
        self.assertNotIn("codex_restart_pending", out["agents"][name])
        self.assertEqual(RunStatus.load(run_dir).agent(name)["update_restarts"], 1)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_concurrent_fail_is_preserved_through_codex_restart_startup(self):
        name = "hrtest-codex"
        for race_at in ("restart_claim", "pane_read", "agent_start", "agent_read", "closure_save", "closed_save",
                        "idle_write", "prompt_boundary", "agent_prompt"):
            with self.subTest(race_at=race_at):
                run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True), root=self.root / race_at)
                self.herdr.agent_status[name] = ["gone"]
                self.herdr.append_pane_output("w1:p2", UPDATED)
                restarting = self.runner(run_dir)
                original_start, original_read = self.herdr.agent_start, self.herdr.agent_read
                original_pane, original_prompt = self.herdr.pane_read, self.herdr.agent_prompt
                original_save, original_state = restarting.status.save, restarting.status.set_agent_state
                original_send, original_claim = restarting._send_review_prompt, restarting.status.claim_codex_update_restart
                raced = False

                def fail_at(where):
                    nonlocal raced
                    generation = 0 if where == "restart_claim" else 1
                    if raced or race_at != where or RunStatus.load(run_dir).agent(name)["codex_launch_generation"] != generation:
                        return
                    raced = True
                    out = self.runner(run_dir).fail(name, "reviewer removed during restart")
                    self.assertEqual(out["state"], "failed")

                def claim(agent):
                    fail_at("restart_claim")
                    return original_claim(agent)

                def start(*args, **kwargs):
                    result = original_start(*args, **kwargs)
                    fail_at("agent_start")
                    return result

                def read(*args, **kwargs):
                    result = original_read(*args, **kwargs)
                    fail_at("agent_read")
                    return result

                def pane(*args, **kwargs):
                    result = original_pane(*args, **kwargs)
                    fail_at("pane_read")
                    return result

                def prompt(*args, **kwargs):
                    result = original_prompt(*args, **kwargs)
                    fail_at("agent_prompt")
                    return result

                def send_review_prompt(agent):
                    fail_at("prompt_boundary")
                    original_send(agent)

                def save():
                    if restarting.status.agent(name).get("codex_startup_closed"):
                        fail_at("closure_save")
                    original_save()
                    if restarting.status.agent(name).get("codex_startup_closed"):
                        fail_at("closed_save")

                def set_state(agent, state, *args, **kwargs):
                    if state == "idle":
                        fail_at("idle_write")
                    return original_state(agent, state, *args, **kwargs)

                self.herdr.agent_start, self.herdr.agent_read, self.herdr.pane_read = start, read, pane
                self.herdr.agent_prompt = prompt
                restarting.status.save, restarting.status.set_agent_state = save, set_state
                restarting.status.claim_codex_update_restart = claim
                restarting._send_review_prompt = send_review_prompt
                out = restarting.wait()
                self.assertTrue(raced)
                self.assertEqual(out["agents"][name]["state"], "failed")
                self.assertEqual(out["agents"][name]["reason"], "reviewer removed during restart")
                self.assertTrue(out["settled"])
                a = RunStatus.load(run_dir).agent(name)
                self.assertEqual((a["codex_launch_generation"], a.get("update_restarts", 0)),
                                 (0, 0) if race_at == "restart_claim" else (1, 1))
                self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2 if race_at == "agent_prompt" else 1)
                self.assertEqual(len(self.herdr.calls_named("agent_start")),
                                 1 if race_at in ("restart_claim", "pane_read") else 2)

    def test_fail_reports_a_generation_conflict_without_stopping_the_replacement_queue(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        first, stale = self.runner(run_dir), self.runner(run_dir)
        original_save = stale.status.save
        raced = False

        def save():
            nonlocal raced
            if not raced:
                raced = True
                self.assertTrue(first.status.claim_codex_update_restart("hrtest-codex"))
                first.status.set_agent_state("hrtest-codex", "working", generation=1)
                self.herdr.agent_status["hrtest-codex"] = ["working"]
                self.herdr.screens["hrtest-codex"] = CODEX_IDLE
            original_save()

        stale.status.save = save
        with mock.patch.object(stale, "_stop_queue_holder", return_value="replacement command") as stop:
            with self.assertRaisesRegex(RunnerError, "run fail again"):
                stale.fail("hrtest-codex", "remove old startup")
            stop.assert_not_called()
        self.assertTrue(raced)
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual((a["codex_launch_generation"], a["state"]), (1, "working"))
        self.assertIsNone(a["reason"])
        self.assertNotIn(("tab_rename", "w1:t2", "rv-hrtest: codex ✗"), self.herdr.calls)

    def test_a_delayed_prompt_exit_observation_reobserves_without_resending(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(blocked_start=True))
        first, stale = self.runner(run_dir), self.runner(run_dir)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        original_get = self.herdr.agent_get
        delayed = True

        def get(name):
            nonlocal delayed
            result = original_get(name)
            if delayed:
                delayed = False
                first._observe(name)
            return result

        self.herdr.agent_get = get
        out = stale.prompt("hrtest-codex")
        self.assertEqual(out["state"], "working")
        self.assertEqual(RunStatus.load(run_dir).agent("hrtest-codex")["state"], "working")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_a_delayed_agent_prompt_result_keeps_the_new_generation_and_owned_retry(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(blocked_start=True))
        first, stale = self.runner(run_dir), self.runner(run_dir)
        self.herdr.agent_status["hrtest-codex"] = ["idle"]
        original_prompt = self.herdr.agent_prompt
        delayed = True

        def prompt(name, text, until=None, timeout_ms=None):
            nonlocal delayed
            if delayed:
                delayed = False
                self.herdr.calls.append(("agent_prompt", name, text, until, timeout_ms))
                self.herdr.agent_status[name] = ["gone"]
                self.herdr.append_pane_output("w1:p2", UPDATED)
                first._observe(name)
                return HerdrResult(False, 1, error_code="agent_not_found", message="old launch exited")
            return original_prompt(name, text, until, timeout_ms)

        self.herdr.agent_prompt = prompt
        out = stale.prompt("hrtest-codex", retry=True)
        self.assertEqual(out["state"], "working")
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual((a["state"], a["retries"], a["collect_retries"]), ("working", 1, 0))
        self.assertIsNone(a["reason"])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)

    def test_the_restart_generation_is_reserved_before_agent_start(self):
        self.herdr = UpdatingCodex()
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        original_start = self.herdr.agent_start
        reservations = []

        def start(name, kind, pane, args, timeout_ms=300000):
            a = RunStatus.load(run_dir).agent(name)
            reservations.append((a.get("codex_launch_generation", 0), a.get("update_restarts", 0)))
            return original_start(name, kind, pane, args, timeout_ms)

        self.herdr.agent_start = start
        runner = self.runner(run_dir)
        runner.start_reviewers()
        runner.wait()
        self.assertEqual(reservations, [(0, 0), (1, 1)])

    def test_the_current_restarted_generation_exit_is_gone_with_its_own_diagnostics(self):
        run_dir, _ = self.start_update()
        runner = self.runner(run_dir)
        runner.wait()
        self.assertEqual(runner.status.agent("hrtest-codex").get("codex_launch_generation", 0), 1)
        screen = "Fatal: restarted Codex session exited\nuser@host$ "
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", screen)
        last_screen = SHELL_SCREEN + UPDATE_MENU + UPDATED + CODEX_IDLE + screen
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(out["pending"], [])
        a = RunStatus.load(run_dir).agent("hrtest-codex")
        self.assertEqual(a["codex_launch_generation"], 1)
        self.assertEqual(a["reason"], "agent exited")
        self.assertEqual(a["last_screen"], last_screen)
        self.assertFalse(a["codex_update_pending"])
        self.assertTrue(a["codex_startup_closed"])
        self.assertEqual(a["update_restarts"], 1)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_prompt_retry_count_survives_an_atomic_update_recovery(self):
        run_dir, _ = self.start_update()
        self.herdr.first_prompt_error = "agent_not_found"
        runner = self.runner(run_dir)
        out = runner.prompt("hrtest-codex", retry=True)
        self.assertEqual(out["state"], "working")
        a = runner.status.agent("hrtest-codex")
        self.assertEqual((a["retries"], a["collect_retries"], a["update_restarts"]), (1, 0, 1))
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_explicit_prompt_defers_a_running_installer(self):
        for live in ("idle", "working"):
            with self.subTest(live=live):
                self.herdr = UpdatingCodex(still_updating=True)
                run_dir = make_run(self.root / live, self.repo, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                self.herdr.agent_status["hrtest-codex"] = [live]
                prompts = self.herdr.calls_named("agent_prompt")
                out = self.runner(run_dir).prompt("hrtest-codex")
                self.assertEqual(out["state"], "prompt_stalled")
                self.assertTrue(out["codex_update_pending"])
                self.assertEqual(out["update_restarts"], 0)
                self.assertEqual(self.herdr.calls_named("agent_prompt"), prompts)
                self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
                a = self.runner(run_dir).status.agent("hrtest-codex")
                self.assertEqual((a["retries"], a["collect_retries"]), (0, 0))

    def test_explicit_prompt_defers_a_late_appearing_installer(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(startup_screen="Starting Codex...\n", blocked_start=True))
        self.assertFalse(runner.status.agent("hrtest-codex")["codex_update_pending"])
        self.herdr.screens["hrtest-codex"] = UPDATING
        self.herdr.agent_status["hrtest-codex"] = ["idle"]
        out = self.runner(run_dir).prompt("hrtest-codex")
        self.assertTrue(out["codex_update_pending"])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
        a = self.runner(run_dir).status.agent("hrtest-codex")
        self.assertEqual((a["retries"], a["collect_retries"], a.get("update_restarts", 0)), (0, 0, 0))

    def test_explicit_prompt_defers_a_blocked_installer(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True))
        self.herdr.screens["hrtest-codex"] = UPDATING + "Authentication required\n"
        self.herdr.agent_status["hrtest-codex"] = ["blocked"]
        self.assertEqual(runner.wait()["agents"]["hrtest-codex"]["state"], "blocked")
        prompts = self.herdr.calls_named("agent_prompt")
        out = self.runner(run_dir).prompt("hrtest-codex")
        self.assertEqual(out["state"], "blocked")
        self.assertTrue(out["codex_update_pending"])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), prompts)
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        a = self.runner(run_dir).status.agent("hrtest-codex")
        self.assertEqual((a["retries"], a["collect_retries"]), (0, 0))

    def test_deferred_explicit_prompt_consumes_no_retry_budget(self):
        run_dir, runner = self.start_update(herdr=UpdatingCodex(still_updating=True))
        a = runner.status.agent("hrtest-codex")
        a.update(retries=2, collect_retries=1)
        runner.status.mark_agent("hrtest-codex")
        runner.status.save()
        prompts = self.herdr.calls_named("agent_prompt")
        out = self.runner(run_dir).prompt("hrtest-codex", retry=True)
        self.assertTrue(out["codex_update_pending"])
        a = self.runner(run_dir).status.agent("hrtest-codex")
        self.assertEqual((a["retries"], a["collect_retries"], a.get("update_restarts", 0)), (2, 1, 0))
        self.assertEqual(self.herdr.calls_named("agent_prompt"), prompts)
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_explicit_prompt_still_answers_an_idle_update_menu(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(blocked_start=True))
        self.herdr.agent_status["hrtest-codex"] = ["idle"]
        out = self.runner(run_dir).prompt("hrtest-codex")
        self.assertEqual(out["state"], "prompt_stalled")
        self.assertTrue(out["codex_update_pending"])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [
            ("agent_prompt", "hrtest-codex", f"Read {run_dir}/prompts/codex.md and follow it exactly.", "working", 30000),
        ])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_explicit_prompt_recovers_a_confirmed_update_exit_without_prompting_the_installer(self):
        run_dir, _ = self.start_update()
        self.herdr.first_prompt_error = "agent_not_found"
        before = len(self.herdr.calls_named("agent_prompt"))
        out = self.runner(run_dir).prompt("hrtest-codex")
        self.assertEqual(out["state"], "working")
        self.assertFalse(out["codex_update_pending"])
        self.assertEqual(out["update_restarts"], 1)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)
        self.assertEqual(self.herdr.calls_named("agent_prompt")[before:], [
            ("agent_prompt", "hrtest-codex", f"Read {run_dir}/prompts/codex.md and follow it exactly.", "working", 30000),
        ])

    def test_explicit_prompt_handles_a_failed_installer_exit_without_prompting(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        screen = "Error: download failed\nuser@host$ "
        self.herdr.append_pane_output("w1:p2", screen)
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        last_screen = SHELL_SCREEN + UPDATE_MENU + UPDATING + screen
        prompts = self.herdr.calls_named("agent_prompt")
        out = self.runner(run_dir).prompt("hrtest-codex")
        self.assertEqual(out["state"], "gone")
        self.assertEqual(out["reason"], "agent exited")
        self.assertFalse(out["codex_update_pending"])
        self.assertEqual(out["update_restarts"], 0)
        self.assertEqual(self.runner(run_dir).status.agent("hrtest-codex")["last_screen"], last_screen)
        self.assertEqual(self.herdr.calls_named("agent_prompt"), prompts)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_update_output_stays_in_codexs_pane_with_other_reviewers(self):
        run_dir, runner = self.start_update(reviewers=("codex", "claude-opus"))
        runner.prompt("hrtest-codex")
        self.assertEqual(self.herdr.pane_screens["w1:p2"], SHELL_SCREEN + UPDATE_MENU + UPDATED + CODEX_IDLE)
        self.assertNotIn("w1:p3", self.herdr.pane_screens)
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")

    def test_collect_does_not_prompt_an_idle_startup_update_after_a_herdr_error(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(first_prompt_error="herdr_failed", still_updating=True))
        runner = self.runner(run_dir)
        out = runner.collect()
        self.assertEqual(out["pending"], ["hrtest-codex"])
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)
        self.assertEqual(runner.wait()["pending"], ["hrtest-codex"])

    def test_an_unrecognized_installer_screen_keeps_the_update_pending(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        self.herdr.screens["hrtest-codex"] = "==> Downloading standalone package: 75%\n"
        runner = self.runner(run_dir)
        self.assertEqual(runner.wait()["reason"], "checkin")
        self.assertTrue(runner.status.agent("hrtest-codex")["codex_update_pending"])
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        self.assertEqual(self.runner(run_dir).wait()["agents"]["hrtest-codex"]["state"], "working")

    def test_a_native_codex_session_clears_the_update_even_when_its_screen_lags(self):
        run_dir, _ = self.start_update()
        self.herdr.agent_status["hrtest-codex"] = ["working"]
        original_get = self.herdr.agent_get

        def get(name):
            result = original_get(name)
            if result.ok:
                result.result["agent"]["agent_session"] = {
                    "agent": "codex", "kind": "id", "source": "herdr:codex", "value": "native-review-session",
                }
            return result

        self.herdr.agent_get = get
        runner = self.runner(run_dir)
        self.assertEqual(runner.wait()["agents"]["hrtest-codex"]["state"], "working")
        self.assertFalse(runner.status.agent("hrtest-codex")["codex_update_pending"])
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_the_installer_is_waited_for_even_when_herdr_calls_it_idle(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        runner = self.runner(run_dir)
        out = runner.wait()
        self.assertEqual(out["reason"], "checkin")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        self.assertEqual(runner.collect()["pending"], ["hrtest-codex"])
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_update_failures_keep_the_exit_and_its_screen(self):
        for i, screen in enumerate((UPDATING + "Error: download failed\nuser@host$ ", UPDATING,
                                    "Please restart Codex.\nuser@host$ ", None)):
            with self.subTest(screen=screen):
                root = self.root / str(i)
                self.herdr = UpdatingCodex(still_updating=True)
                run_dir = make_run(root, self.repo, reviewers=("codex",))
                runner = self.runner(run_dir)
                runner.start_reviewers()
                self.herdr.agent_status["hrtest-codex"] = ["gone"]
                if screen is None:
                    self.herdr.pane_screens["w1:p2"] = None
                else:
                    self.herdr.append_pane_output("w1:p2", screen)
                last_screen = SHELL_SCREEN + UPDATE_MENU + UPDATING + screen if screen is not None else None
                out = runner.wait()
                self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
                self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
                a = runner.status.agent("hrtest-codex")
                self.assertEqual(a["last_screen"], last_screen)
                self.assertEqual(a["reason"], "agent exited")
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(a.get("update_restarts", 0), 0)
                self.assertTrue(a["codex_startup_pending"])
                self.assertFalse(a.get("codex_startup_closed"))
                self.clock.t += 1

    def test_an_old_success_message_without_an_observed_startup_update_is_ignored(self):
        self.herdr.pane_screens["w1:p2"] = SHELL_SCREEN + UPDATED
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        runner = self.runner(run_dir)
        runner.start_reviewers()
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", "Segmentation fault\nuser@host$ ")
        out = runner.wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_another_agent_kind_with_the_same_update_text_is_not_restarted(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(name="hrtest-claude-opus"), reviewers=("claude-opus",))
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-claude-opus"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_a_live_review_clears_the_startup_update_before_a_later_crash(self):
        run_dir, _ = self.start_update()
        self.herdr.agent_status["hrtest-codex"] = ["working"]
        self.herdr.screens["hrtest-codex"] = CODEX_IDLE + "Reviewing files...\n"
        runner = self.runner(run_dir)
        self.assertEqual(runner.wait()["agents"]["hrtest-codex"]["state"], "working")
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", "Segmentation fault\nuser@host$ ")
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)

    def test_a_manual_revival_clears_the_startup_update(self):
        run_dir, _ = self.start_update()
        self.herdr.agent_start("hrtest-codex", "codex", "w1:p2", ["-m", "gpt-5.5"])
        runner = self.runner(run_dir)
        runner.prompt("hrtest-codex")
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "gone")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_second_update_exit_spends_no_further_restart_after_reload(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(repeat=True, first_prompt_error=None))
        first = self.runner(run_dir).wait()
        self.assertEqual(first["agents"]["hrtest-codex"].get("update_restarts", 0), 1)
        runner = self.runner(run_dir)
        second = runner.wait()
        self.assertEqual(second["agents"]["hrtest-codex"]["state"], "gone")
        self.assertFalse(second["agents"]["hrtest-codex"]["codex_update_pending"])
        a = runner.status.agent("hrtest-codex")
        self.assertEqual(a["update_restarts"], 1)
        self.assertTrue(a["codex_startup_pending"])
        self.assertFalse(a.get("codex_startup_closed"))
        self.assertEqual(a["reason"], "agent exited")
        self.assertEqual(a["last_screen"], SHELL_SCREEN + UPDATE_MENU + UPDATED + UPDATE_MENU + UPDATED)
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_restart_failure_is_reported_without_looping(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(restart_error=("pane_busy", "pane is occupied")))
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "failed")
        self.assertIn("pane is occupied", out["agents"]["hrtest-codex"]["reason"])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_restarted_agent_waiting_for_permission_stays_pending(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(restarted_prompt_error=("agent_blocked", "approval needed")))
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "blocked")
        self.assertEqual(out["pending"], ["hrtest-codex"])
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)

    def test_a_restart_claim_status_error_is_reported_as_runner_error(self):
        run_dir, _ = self.start_update()
        runner = self.runner(run_dir)
        error = StatusError("cannot claim Codex restart: status.json is unavailable")
        with mock.patch.object(runner.status, "claim_codex_update_restart", side_effect=error) as claim:
            with self.assertRaises(RunnerError) as caught:
                runner.wait()
        self.assertIn(str(error), str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)
        claim.assert_called_once_with("hrtest-codex")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 1)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_a_codex_fixer_update_exit_stays_outside_reviewer_recovery(self):
        self.herdr = UpdatingCodex(name="hrtest-fixer", startup_screen=UPDATING)
        run_dir = make_run(self.root, self.repo, reviewers=())
        runner = self.runner(run_dir)
        runner.start_fixer()
        self.herdr.agent_status["hrtest-fixer"] = ["gone"]
        self.herdr.append_pane_output("w1:p2", UPDATED)
        with mock.patch.object(runner.status, "claim_codex_update_restart", wraps=runner.status.claim_codex_update_restart) as claim:
            out = runner.wait(agent="hrtest-fixer")
        self.assertEqual(out["agents"]["hrtest-fixer"]["state"], "gone")
        self.assertEqual(out["pending"], [])
        a = self.runner(run_dir).status.agent("hrtest-fixer")
        self.assertEqual(a["last_screen"], SHELL_SCREEN + UPDATING + UPDATED)
        for field in ("codex_startup_pending", "codex_startup_closed", "codex_update_seen", "codex_update_pending", "update_restarts", "codex_launch_generation", "codex_pane_baseline", "codex_pane_baseline_generation"):
            self.assertNotIn(field, a)
        claim.assert_not_called()
        self.assertEqual(self.herdr.calls_named("agent_start"), [
            ("agent_start", "hrtest-fixer", "codex", "w1:p2", ["-m", "gpt-5.5"]),
        ])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])

    def test_a_wrapped_and_colored_success_message_is_recovered(self):
        run_dir, _ = self.start_update(herdr=UpdatingCodex(still_updating=True))
        self.herdr.append_pane_output("w1:p2", "\x1b[32m🎉 Update ran successfully!\x1b[0m\n│ Please restart │\n│ Codex.         │\nuser@host$ ")
        self.herdr.agent_status["hrtest-codex"] = ["gone"]
        out = self.runner(run_dir).wait()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertEqual(len(self.herdr.calls_named("agent_start")), 2)


if __name__ == "__main__":
    unittest.main()
