import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
