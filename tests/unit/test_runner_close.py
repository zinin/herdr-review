import json
import unittest

from herdr_review.herdr import HerdrResult
from herdr_review.runner import RunnerError
from herdr_review.status import RunStatus
from tests.unit.test_runner_start import RunnerBase, make_run


class CloseTest(RunnerBase):
    def finished_run(self, layout="tabs"):
        run_dir = make_run(self.root, self.repo, layout=layout, reviewers=("codex", "gemini"))
        r = self.runner(run_dir)
        r.start_reviewers()
        r.start_fixer()
        r.finish([])
        self.herdr.calls.clear()
        return run_dir

    def test_close_shuts_every_tab_of_a_finished_run(self):
        run_dir = self.finished_run()
        out = self.runner(run_dir).close()
        self.assertEqual(out, {"closed": ["w1:t2", "w1:t3", "w1:t4", "w1:t1"], "already_closed": [], "failed": {}})
        self.assertEqual([c[1] for c in self.herdr.calls_named("tab_close")], ["w1:t2", "w1:t3", "w1:t4", "w1:t1"])
        self.assertIn("closed_at", json.loads((run_dir / "status.json").read_text()))

    def test_grid_closes_the_orchestrator_tab_only(self):
        run_dir = self.finished_run(layout="grid")
        out = self.runner(run_dir).close()
        self.assertEqual(out["closed"], ["w1:t1"])
        self.assertEqual(self.herdr.calls_named("pane_close"), [])

    def test_a_run_in_progress_is_refused_unless_forced(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = self.runner(run_dir)
        r.start_reviewers()
        with self.assertRaises(RunnerError) as ctx:
            r.close()
        self.assertIn("still in phase reviewing", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))
        self.assertEqual(self.herdr.calls_named("tab_close"), [])
        self.assertEqual(r.close(force=True)["closed"], ["w1:t2", "w1:t1"])

    def test_a_forced_close_aborts_a_run_in_progress(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        (run_dir / "scratch" / "codex").mkdir(parents=True)
        r = self.runner(run_dir)
        r.start_reviewers()
        r.notify("x", sound="request")
        r.close(force=True)
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["phase"], "aborted")
        self.assertEqual(status["abort_reason"], "closed with --force")
        self.assertFalse(status["waiting_for_user"])
        self.assertIn("closed_at", status)
        self.assertFalse((run_dir / "scratch").exists())

    def test_a_forced_close_that_leaves_a_tab_open_keeps_the_run_in_progress(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        (run_dir / "scratch" / "codex").mkdir(parents=True)
        r = self.runner(run_dir)
        r.start_reviewers()
        self.herdr.close_errors["w1:t2"] = ("server_error", "boom")
        self.assertEqual(r.close(force=True)["failed"], {"w1:t2": "server_error: boom"})
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["phase"], "reviewing")
        self.assertNotIn("abort_reason", status)
        self.assertTrue((run_dir / "scratch" / "codex").is_dir())
        self.assertIn("close --force: 1 of 2 did not close; the run stays in phase reviewing", (run_dir / "runner.log").read_text())
        del self.herdr.close_errors["w1:t2"]
        r.close(force=True)                                   # the retry closes the rest and ends the run
        self.assertEqual(json.loads((run_dir / "status.json").read_text())["phase"], "aborted")
        self.assertFalse((run_dir / "scratch").exists())

    def test_a_forced_close_of_a_finished_run_keeps_it_finished(self):
        run_dir = self.finished_run()
        self.runner(run_dir).close(force=True)
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["phase"], "finished")
        self.assertNotIn("abort_reason", status)

    def test_a_tab_closed_by_hand_is_not_an_error(self):
        run_dir = self.finished_run()
        self.herdr.close_errors["w1:t3"] = ("tab_not_found", "tab w1:t3 not found")
        self.herdr.close_errors["w1:t4"] = ("server_error", "boom")
        out = self.runner(run_dir).close()
        self.assertEqual(out["closed"], ["w1:t2", "w1:t1"])
        self.assertEqual(out["already_closed"], ["w1:t3"])
        self.assertEqual(out["failed"], {"w1:t4": "server_error: boom"})

    def test_a_missing_herdr_binary_is_a_failure_not_a_closed_tab(self):
        run_dir = self.finished_run()
        self.herdr.close_errors["w1:t2"] = ("herdr_not_found", "herdr not found in PATH")
        out = self.runner(run_dir).close()
        self.assertEqual(out["already_closed"], [])
        self.assertEqual(out["failed"], {"w1:t2": "herdr_not_found: herdr not found in PATH"})

    def test_an_aborted_run_closes_the_orchestrator_tab_it_left_open(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        RunStatus.load(run_dir).set_phase("aborted")
        self.assertEqual(self.runner(run_dir).close()["closed"], ["w1:t1"])

    def launched_in(self, run_dir, session, socket):
        run = json.loads((run_dir / "run.json").read_text())
        run.update(herdr_session=session, herdr_socket_path=socket)
        (run_dir / "run.json").write_text(json.dumps(run))

    def test_a_caller_in_another_herdr_session_is_refused(self):
        ai = {"HERDR_SESSION": "ai", "HERDR_SOCKET_PATH": "/home/u/.config/herdr/sessions/ai/herdr.sock"}
        work = {"HERDR_SESSION": "work", "HERDR_SOCKET_PATH": "/home/u/.config/herdr/sessions/work/herdr.sock"}
        for i, (recorded, caller) in enumerate((
            (ai, work), (ai, {}),                                                        # {}: a shell outside herdr
            ({"HERDR_SESSION": "ai", "HERDR_SOCKET_PATH": None}, {"HERDR_SESSION": "work"}),
        )):
            with self.subTest(recorded=recorded, caller=caller):
                run_dir = make_run(self.root / str(i), self.repo, reviewers=("codex",))
                r = self.runner(run_dir)
                r.start_reviewers()
                self.launched_in(run_dir, recorded["HERDR_SESSION"], recorded["HERDR_SOCKET_PATH"])
                before = (run_dir / "status.json").read_text()
                with self.assertRaises(RunnerError) as ctx:
                    self.runner(run_dir).close(force=True, environ=caller)
                self.assertEqual(str(ctx.exception), "run hrtest was started in herdr session 'ai'; run close from a pane of that session")
                self.assertEqual(self.herdr.calls_named("tab_close"), [])
                self.assertEqual((run_dir / "status.json").read_text(), before)
        out = self.runner(run_dir).close(force=True, environ={"HERDR_SESSION": "ai"})   # the session of the launch
        self.assertEqual(out["closed"], ["w1:t4", "w1:t1"])

    def test_a_run_launched_before_the_session_was_recorded_still_closes(self):
        run_dir = self.finished_run()                                                  # run.json without the fields
        out = self.runner(run_dir).close(environ={"HERDR_SESSION": "work", "HERDR_SOCKET_PATH": "/tmp/work.sock"})
        self.assertEqual(out["closed"], ["w1:t2", "w1:t3", "w1:t4", "w1:t1"])

    def test_a_tab_id_that_now_names_someone_elses_tab_is_left_open(self):
        run_dir = self.finished_run()
        self.herdr.tab_labels["w1:t3"] = "build"                 # a restarted herdr gave the ID to another tab
        out = self.runner(run_dir).close()
        self.assertEqual(out, {"closed": ["w1:t2", "w1:t4", "w1:t1"], "already_closed": ["w1:t3"], "failed": {}})
        self.assertNotIn(("tab_close", "w1:t3"), self.herdr.calls)
        self.assertIn("close: w1:t3 is now labelled 'build', not a tab of this run; left open", (run_dir / "runner.log").read_text())

    def test_a_tab_herdr_cannot_describe_is_not_closed(self):
        run_dir = self.finished_run()
        self.herdr.tab_get = lambda tab: HerdrResult(False, 124, error_code="timeout", message="herdr call timed out")
        out = self.runner(run_dir).close()
        self.assertEqual(out["closed"], [])
        self.assertEqual(out["failed"]["w1:t2"], "timeout: herdr call timed out")
        self.assertEqual(self.herdr.calls_named("tab_close"), [])
        self.assertIn("close: cannot check w1:t2: timeout: herdr call timed out", (run_dir / "runner.log").read_text())


if __name__ == "__main__":
    unittest.main()
