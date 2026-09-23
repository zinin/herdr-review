import json
import subprocess
import unittest
from pathlib import Path

from herdr_review import gitutil
from herdr_review.runner import Runner, retry_text
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_runner_start import RunnerBase, git, make_run

GOOD_REVIEW = "### Strengths\nx\n### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\n**Ready to merge:** Yes\n"


class CollectTest(RunnerBase):
    def setUp(self):
        super().setUp()
        self.run_dir = make_run(self.root, self.repo, reviewers=("claude-opus", "codex"))
        self.r = Runner(self.run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        self.r.start_reviewers()

    def settle(self, *names, state="done"):
        for n in names:
            self.r.status.set_agent_state(n, state)

    def test_valid_file_is_collected(self):
        (self.run_dir / "reviews" / "codex.md").write_text(GOOD_REVIEW)
        self.settle("hrtest-codex")
        out = self.r.collect()
        self.assertEqual(out["collected"], ["hrtest-codex"])
        self.assertEqual(out["pending"], ["hrtest-claude-opus"])
        self.assertEqual(out["failed"], {})
        self.assertFalse(out["drift"])
        self.assertTrue(self.r.status.agent("hrtest-codex")["result_ok"])
        self.assertIn(("tab_rename", "w1:t3", "rv-hrtest: codex ✓"), self.herdr.calls)

    def test_retry_text_matches_the_documented_invariant_literally(self):
        self.assertEqual(
            retry_text("/run/reviews/codex.md", "file missing", "/run/prompts/codex.md"),
            "You have not written a valid review to /run/reviews/codex.md (file missing)."
            " Read /run/prompts/codex.md and follow it exactly."
            " Write your complete review there now, in the required format, and reply DONE.",
        )
        self.assertEqual(
            retry_text("/run/reviews/codex.md", "file missing"),
            "You have not written a valid review to /run/reviews/codex.md (file missing)."
            " Write your complete review there now, in the required format, and reply DONE.",
        )

    def test_missing_file_is_retried_once_then_failed(self):
        self.settle("hrtest-codex", state="idle")
        out = self.r.collect()
        self.assertEqual(out["pending"], ["hrtest-claude-opus", "hrtest-codex"])
        last = self.herdr.calls_named("agent_prompt")[-1]
        self.assertEqual(last[1], "hrtest-codex")
        self.assertEqual(
            last[2],
            retry_text(str(self.run_dir / "reviews" / "codex.md"), "file missing", str(self.run_dir / "prompts" / "codex.md")),
        )
        a = self.r.status.agent("hrtest-codex")
        self.assertEqual((a["state"], a["collect_retries"], a["retries"]), ("working", 1, 0))
        self.settle("hrtest-codex", state="idle")
        self.herdr.pane_screens["w1:p3"] = "I could not write the file\n"
        out = self.r.collect()
        self.assertEqual(list(out["failed"]), ["hrtest-codex"])
        self.assertIn("file missing", out["failed"]["hrtest-codex"])
        self.assertIn("could not write", self.r.status.agent("hrtest-codex")["last_screen"])
        self.assertEqual(self.r.status.agent("hrtest-codex")["state"], "failed")

    def test_invalid_file_retry_mentions_missing_sections(self):
        (self.run_dir / "reviews" / "codex.md").write_text("### Strengths\nonly this\n")
        self.settle("hrtest-codex")
        self.r.collect()
        last = self.herdr.calls_named("agent_prompt")[-1]
        self.assertIn("missing sections: ### Critical Issues", last[2])

    def test_unprompted_idle_agent_gets_the_review_prompt_without_a_retry(self):
        a = self.r.status.agent("hrtest-codex")
        a["prompted"] = False
        self.settle("hrtest-codex", state="idle")
        out = self.r.collect()
        self.assertIn("hrtest-codex", out["pending"])
        last = self.herdr.calls_named("agent_prompt")[-1]
        self.assertEqual(last[2], f"Read {self.run_dir}/prompts/codex.md and follow it exactly.")
        self.assertEqual(self.r.status.agent("hrtest-codex")["retries"], 0)

    def test_a_manual_retry_does_not_spend_collects_own_re_prompt(self):
        self.r.prompt("hrtest-codex", retry=True)                    # the Phase 2 manual shake
        self.assertEqual(self.r.status.agent("hrtest-codex")["retries"], 1)
        self.assertEqual(self.r.status.agent("hrtest-codex")["collect_retries"], 0)
        self.settle("hrtest-codex", state="idle")
        out = self.r.collect()                                       # first miss: still re-prompted
        self.assertIn("hrtest-codex", out["pending"])
        self.assertEqual(out["failed"], {})
        self.assertIn("You have not written a valid review", self.herdr.calls_named("agent_prompt")[-1][2])
        a = self.r.status.agent("hrtest-codex")
        self.assertEqual((a["retries"], a["collect_retries"]), (1, 1))
        self.settle("hrtest-codex", state="idle")
        out = self.r.collect()                                       # second miss: failed
        self.assertIn("hrtest-codex", out["failed"])

    def test_a_prompt_stalled_reviewer_is_only_pending(self):
        self.settle("hrtest-codex", state="prompt_stalled")
        before = len(self.herdr.calls_named("agent_prompt"))
        out = self.r.collect()
        self.assertIn("hrtest-codex", out["pending"])
        self.assertEqual(out["failed"], {})
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), before)
        a = self.r.status.agent("hrtest-codex")
        self.assertEqual((a["retries"], a["collect_retries"]), (0, 0))

    def test_unknown_state_without_file_stays_pending(self):
        self.settle("hrtest-codex", state="unknown")
        out = self.r.collect()
        self.assertIn("hrtest-codex", out["pending"])
        self.assertEqual([c for c in self.herdr.calls_named("agent_prompt") if c[1] == "hrtest-codex"][1:], [])
        (self.run_dir / "reviews" / "codex.md").write_text(GOOD_REVIEW)
        out = self.r.collect()
        self.assertIn("hrtest-codex", out["collected"])

    def test_failed_and_gone_are_reported(self):
        self.r.fail("hrtest-codex", "quota exceeded")
        self.r.status.set_agent_state("hrtest-claude-opus", "gone", reason="agent exited")
        out = self.r.collect()
        self.assertEqual(out["failed"], {"hrtest-codex": "quota exceeded", "hrtest-claude-opus": "agent exited"})

    def test_gone_with_valid_file_is_collected(self):
        (self.run_dir / "reviews" / "codex.md").write_text(GOOD_REVIEW)
        self.r.status.set_agent_state("hrtest-codex", "gone", reason="agent exited")
        out = self.r.collect()
        self.assertIn("hrtest-codex", out["collected"])
        self.assertNotIn("hrtest-codex", out["failed"])
        self.assertEqual(self.r.status.agent("hrtest-codex")["state"], "collected")

    def test_retry_prompt_error_does_not_keep_agent_pending(self):
        self.settle("hrtest-codex", state="idle")
        self.herdr.prompt_errors["hrtest-codex"] = ("agent_not_found", "gone")
        out = self.r.collect()
        self.assertNotIn("hrtest-codex", out["pending"])
        self.assertEqual(self.r.status.agent("hrtest-codex")["state"], "gone")
        self.assertIn("hrtest-codex", out["failed"])

    def test_drift_detected_only_while_reviewing(self):
        (self.repo / "a.txt").write_text("changed by a reviewer\n")
        out = self.r.collect()
        self.assertTrue(out["drift"])
        self.assertIn("a.txt", out["drift_status"])
        self.assertTrue(self.r.status.data["drift"])
        (self.repo / "a.txt").write_text("one\n")
        self.assertTrue(self.r.collect()["drift"])          # sticky
        run2 = make_run(self.root / "second", self.repo, reviewers=("codex",))
        r2 = Runner(run2, herdr=FakeHerdr(), poll_sec=0, sleep=lambda s: None)
        r2.start_reviewers()
        r2.phase("aggregating")
        (self.repo / "a.txt").write_text("changed by the fixer\n")
        self.assertFalse(r2.collect()["drift"])


class FinishTest(RunnerBase):
    def test_finish_tabs_without_closing(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        (run_dir / "reviews" / "codex.md").write_text(GOOD_REVIEW)
        r.status.set_agent_state("hrtest-codex", "done")
        r.collect()
        r.start_fixer()
        r.notify("x", sound="request")
        out = r.finish(["abc123", ""])
        self.assertEqual(out, {"phase": "finished", "commits": ["abc123"], "closed": []})
        self.assertEqual(r.status.data["phase"], "finished")
        self.assertIsNotNone(r.status.data["finished_at"])
        self.assertFalse(r.status.data["waiting_for_user"])
        self.assertIn(("tab_rename", "w1:t1", "rv-hrtest: orch ✓"), self.herdr.calls)
        note = self.herdr.calls_named("notification_show")[-1]
        self.assertEqual(note[1], "herdr-review: готово")
        self.assertIn("отзывов 1", note[2])
        self.assertIn("коммитов 1", note[2])
        self.assertEqual(note[3], "done")
        self.assertEqual(self.herdr.calls_named("tab_close"), [])

    def reviewed_run(self, commits: int = 2) -> Path:
        """A run whose merge_base is real, with <commits> commits on top of it."""
        base = gitutil.merge_base(self.repo, "HEAD")
        for i in range(commits):
            (self.repo / "a.txt").write_text(f"change {i}\n")
            git(self.repo, "commit", "-q", "-am", f"change {i}")
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        run = json.loads((run_dir / "run.json").read_text())
        run["merge_base"] = base
        (run_dir / "run.json").write_text(json.dumps(run))
        return run_dir

    def test_finish_records_the_commits_git_reports(self):
        run_dir = self.reviewed_run(2)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        out = r.finish([])                                   # --commits omitted entirely
        full = gitutil.log_oneline(self.repo, "HEAD~2..HEAD").splitlines()
        self.assertEqual(out["commits"], [line.split()[0] for line in full])
        self.assertEqual(r.status.data["commits"], out["commits"])
        note = self.herdr.calls_named("notification_show")[-1]
        self.assertIn("коммитов 2", note[2])

    def test_finish_prefers_git_over_the_orchestrator_and_logs_the_difference(self):
        run_dir = self.reviewed_run(1)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        out = r.finish(["deadbee", "cafe123"])
        self.assertEqual(len(out["commits"]), 1)
        self.assertNotIn("deadbee", out["commits"])
        log = (run_dir / "runner.log").read_text()
        self.assertIn("deadbee", log)
        self.assertIn("does not match", log)

    def test_finish_falls_back_to_the_passed_commits_when_git_fails(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))   # merge_base is 0000…
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        out = r.finish(["abc123", ""])
        self.assertEqual(out["commits"], ["abc123"])
        self.assertIn("cannot read the commit list", (run_dir / "runner.log").read_text())

    def test_finish_closes_agents_when_configured(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",), close=True)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        r.start_fixer()
        out = r.finish([])
        self.assertEqual(sorted(out["closed"]), ["w1:t2", "w1:t3"])
        self.assertEqual(sorted(c[1] for c in self.herdr.calls_named("tab_close")), ["w1:t2", "w1:t3"])

    def test_finish_grid_closes_panes(self):
        run_dir = make_run(self.root, self.repo, layout="grid", reviewers=("codex",), close=True)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        r.start_fixer()
        out = r.finish([])
        self.assertEqual(sorted(out["closed"]), ["w1:p2", "w1:p3"])
        self.assertEqual(self.herdr.calls_named("tab_close"), [])

    def test_finish_does_not_report_failed_close_as_closed(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",), close=True)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        r.start_fixer()
        self.herdr.close_errors["w1:t2"] = ("server_error", "boom")
        out = r.finish([])
        self.assertEqual(out["closed"], ["w1:t3"])
        self.assertNotIn("w1:t2", out["closed"])
        self.assertIn("finish: close w1:t2 failed: server_error: boom", (run_dir / "runner.log").read_text())

    def test_finish_counts_only_the_commits_made_during_the_run(self):
        run_dir = self.reviewed_run(2)                      # two branch commits made before the launch
        run = json.loads((run_dir / "run.json").read_text())
        run["head"] = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        (run_dir / "run.json").write_text(json.dumps(run))
        (self.repo / "a.txt").write_text("fixed during the run\n")
        git(self.repo, "commit", "-q", "-am", "fix during the run")
        out = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
        self.assertEqual(out["commits"], [gitutil.log_oneline(self.repo, "HEAD~1..HEAD").split()[0]])
        self.assertIn("коммитов 1", self.herdr.calls_named("notification_show")[-1][2])

    def test_finish_removes_the_scratch_directory(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        (run_dir / "scratch" / "codex" / "copy").mkdir(parents=True)
        (run_dir / "scratch" / "codex" / "copy" / "x.go").write_text("package main\n")
        Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
        self.assertFalse((run_dir / "scratch").exists())
        self.assertIn("finish: removed scratch/", (run_dir / "runner.log").read_text())

    def test_finish_removes_scratch_with_a_read_only_directory_in_it(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        locked = run_dir / "scratch" / "codex" / "gomod" / "mod@v1"      # a Go module cache is read-only
        locked.mkdir(parents=True)
        (locked / "x.go").write_text("package main\n")
        locked.chmod(0o555)
        try:
            Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
            self.assertFalse((run_dir / "scratch").exists())
        finally:
            if locked.exists():
                locked.chmod(0o755)                                       # tearDown must be able to remove the temp dir


if __name__ == "__main__":
    unittest.main()
