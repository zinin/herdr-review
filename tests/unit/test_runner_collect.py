import json
import stat
import subprocess
import unittest
from pathlib import Path

from herdr_review import gitutil
from herdr_review.runner import DRIFT_NOTHING_NEW_OR_GONE, Runner, retry_text
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_runner_start import RunnerBase, git, make_run

GOOD_REVIEW = "### Strengths\nx\n### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\n**Ready to merge:** Yes\n"


def short(repo: Path, rev: str = "HEAD") -> str:
    """<rev>'s abbreviated hash, as git abbreviates it."""
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", rev], capture_output=True, text=True, check=True).stdout.strip()


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

    def reviewing_since_now(self, name: str) -> Runner:
        """A run whose run.json records the tree's `git status --short` lines as uncommitted, as launch does."""
        run_dir = make_run(self.root / name, self.repo, reviewers=("codex",))
        run = json.loads((run_dir / "run.json").read_text())
        run["uncommitted"] = gitutil.status_lines(self.repo)
        (run_dir / "run.json").write_text(json.dumps(run))
        r = Runner(run_dir, herdr=FakeHerdr(), poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        return r

    def test_drift_status_lists_only_what_is_new_since_launch(self):
        (self.repo / "mine.txt").write_text("the owner's, untracked at launch\n")
        r = self.reviewing_since_now("own")
        (self.repo / "new.txt").write_text("written during the review\n")
        out = r.collect()
        self.assertTrue(out["drift"])
        self.assertEqual(out["drift_status"], "?? new.txt\n")

    def test_drift_status_says_so_when_nothing_is_new_or_gone_since_launch(self):
        (self.repo / "a.txt").write_text("the owner's edit\n")
        r = self.reviewing_since_now("again")
        (self.repo / "a.txt").write_text("the owner's edit, edited again\n")
        out = r.collect()
        self.assertTrue(out["drift"])
        self.assertEqual(out["drift_status"], DRIFT_NOTHING_NEW_OR_GONE + "\n")
        self.assertIn("an edit or a revert", out["drift_status"])        # a revert is not ruled out

    def test_drift_status_names_a_file_inside_an_untracked_directory_of_the_launch(self):
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "one.md").write_text("the owner's notes\n")
        r = self.reviewing_since_now("untracked-dir")                    # `?? notes/`
        (self.repo / "notes" / "new.md").write_text("written during the review\n")   # still only `?? notes/`
        out = r.collect()
        self.assertTrue(out["drift"])
        self.assertEqual(out["drift_status"], DRIFT_NOTHING_NEW_OR_GONE + "\n")
        self.assertIn("a file appeared, changed or was deleted inside an untracked directory that was already there at"
                      " launch", out["drift_status"])
        self.assertNotIn("went inside", out["drift_status"])               # it read as "entered"

    def test_drift_status_names_uncommitted_work_gone_since_launch(self):
        (self.repo / "a.txt").write_text("the owner's edit\n")
        r = self.reviewing_since_now("gone")
        git(self.repo, "checkout", "--", "a.txt")                        # the owner's edit wiped during the review
        out = r.collect()
        self.assertTrue(out["drift"])
        self.assertEqual(out["drift_status"], "gone since launch:  M a.txt\n")

    def test_an_edit_staged_since_launch_is_not_gone(self):
        (self.repo / "a.txt").write_text("the owner's edit\n")
        r = self.reviewing_since_now("staged")                           # ` M a.txt`
        git(self.repo, "add", "a.txt")                                   # the same edit, only staged
        out = r.collect()
        self.assertTrue(out["drift"])
        self.assertEqual(out["drift_status"], "M  a.txt\n")

    def test_an_untracked_file_added_since_launch_is_not_gone(self):
        (self.repo / "n.txt").write_text("the owner's new file\n")
        r = self.reviewing_since_now("added")                            # `?? n.txt`
        git(self.repo, "add", "n.txt")
        out = r.collect()
        self.assertTrue(out["drift"])
        self.assertEqual(out["drift_status"], "A  n.txt\n")

    def test_an_untracked_directory_stays_while_a_path_under_it_is_listed(self):
        (self.repo / "dir").mkdir()
        (self.repo / "dir" / "f.txt").write_text("f\n")
        (self.repo / "dir" / "g.txt").write_text("g\n")
        r = self.reviewing_since_now("dir")                              # `?? dir/`
        git(self.repo, "add", "dir/f.txt")                               # `A  dir/f.txt` and `?? dir/g.txt` now
        out = r.collect()
        self.assertEqual(out["drift_status"], "A  dir/f.txt\n?? dir/g.txt\n")

    def test_both_paths_of_a_rename_count_and_quoted_paths_match(self):
        (self.repo / "a.txt").write_text("the owner's edit\n")
        (self.repo / "my dir").mkdir()
        (self.repo / "my dir" / "f g.txt").write_text("f\n")
        r = self.reviewing_since_now("quoted")                           # ` M a.txt`, `?? "my dir/"`
        git(self.repo, "mv", "a.txt", "b c.txt")                         # a.txt is the rename's old path now
        git(self.repo, "add", "my dir/f g.txt")                          # git quotes a path with a space
        out = r.collect()
        self.assertEqual(out["drift_status"], 'RM a.txt -> "b c.txt"\nA  "my dir/f g.txt"\n')

    def test_a_rename_of_the_launch_counts_by_its_new_path(self):
        git(self.repo, "mv", "a.txt", "b.txt")
        r = self.reviewing_since_now("renamed")                          # `R  a.txt -> b.txt`
        git(self.repo, "checkout", "HEAD", "--", "a.txt")                # a.txt is back, b.txt still holds the work
        out = r.collect()
        self.assertEqual(out["drift_status"], "A  b.txt\n")

    def test_a_file_that_shows_only_as_its_untracked_directory_now_is_not_gone(self):
        (self.repo / "newdir").mkdir()
        (self.repo / "newdir" / "x.py").write_text("the owner's new module\n")
        git(self.repo, "add", "newdir/x.py")
        r = self.reviewing_since_now("unstaged")                         # `A  newdir/x.py`
        git(self.repo, "rm", "-q", "--cached", "newdir/x.py")            # unstaged: git lists only `?? newdir/`
        out = r.collect()
        self.assertEqual(out["drift_status"], "?? newdir/\n")

    def test_an_untracked_directory_inside_one_that_shows_whole_now_is_not_gone(self):
        (self.repo / "a").mkdir()
        (self.repo / "a" / "t.txt").write_text("tracked\n")
        git(self.repo, "add", "a/t.txt")
        git(self.repo, "commit", "-q", "-m", "a")
        (self.repo / "a" / "b").mkdir()
        (self.repo / "a" / "b" / "n.txt").write_text("the owner's\n")
        r = self.reviewing_since_now("nested")                           # `?? a/b/`
        git(self.repo, "rm", "-q", "--cached", "a/t.txt")                # a/ holds no tracked file now: `?? a/`
        out = r.collect()
        self.assertEqual(out["drift_status"], "D  a/t.txt\n?? a/\n")


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

    def reviewed_run(self, commits: int = 2, launched_now: bool = False) -> Path:
        """A run whose merge_base is real, with <commits> commits on top of it; <launched_now>: its run.json
        records the HEAD of now as the HEAD at launch."""
        base = gitutil.merge_base(self.repo, "HEAD")
        for i in range(commits):
            (self.repo / "a.txt").write_text(f"change {i}\n")
            git(self.repo, "commit", "-q", "-am", f"change {i}")
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        run = json.loads((run_dir / "run.json").read_text())
        run["merge_base"] = base
        if launched_now:
            run["head"] = gitutil.head_commit(self.repo)
        (run_dir / "run.json").write_text(json.dumps(run))
        return run_dir

    def test_finish_records_the_commits_git_reports(self):
        run_dir = self.reviewed_run(2)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        out = r.finish([])                                   # --commits omitted entirely
        self.assertEqual(out["commits"], [short(self.repo, "HEAD"), short(self.repo, "HEAD~1")])
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

    def test_finish_leaves_open_a_tab_id_that_now_names_someone_elses_tab(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",), close=True)
        r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        r.start_fixer()
        self.herdr.tab_labels["w1:t2"] = "build"
        out = r.finish([])
        self.assertEqual(out["closed"], ["w1:t3"])
        self.assertNotIn(("tab_close", "w1:t2"), self.herdr.calls)

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
        self.assertEqual(out["commits"], [short(self.repo)])
        self.assertIn("коммитов 1", self.herdr.calls_named("notification_show")[-1][2])

    def test_a_branch_rebased_during_the_run_records_the_orchestrators_commits(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        run_dir = self.reviewed_run(2, launched_now=True)       # two branch commits, then the launch
        git(self.repo, "switch", "-q", "master")
        (self.repo / "b.txt").write_text("master moves on\n")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", "master moves on")
        git(self.repo, "switch", "-q", "feat")
        git(self.repo, "rebase", "-q", "master")                # the owner rebases during the run
        (self.repo / "a.txt").write_text("fixed during the run\n")
        git(self.repo, "commit", "-q", "-am", "fix during the run")
        fix = short(self.repo)
        out = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([fix])
        self.assertEqual(out["commits"], [fix])                 # not the rebased commits and master's
        self.assertIn("коммитов 1", self.herdr.calls_named("notification_show")[-1][2])
        log = (run_dir / "runner.log").read_text()
        self.assertIn("finish: the branch was rewritten or switched during the run", log)
        self.assertIn("recording the orchestrator's --commits", log)
        self.assertNotIn("does not match", log)

    def test_a_merge_of_the_base_during_the_run_brings_none_of_the_bases_commits(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        run_dir = self.reviewed_run(2, launched_now=True)       # two branch commits, then the launch
        git(self.repo, "switch", "-q", "master")
        for i in range(3):                                      # master moves on by three commits
            (self.repo / f"m{i}.txt").write_text("master moves on\n")
            git(self.repo, "add", f"m{i}.txt")
            git(self.repo, "commit", "-q", "-m", f"master moves on {i}")
        git(self.repo, "switch", "-q", "feat")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "master")   # the owner merges the base during the run
        merge = short(self.repo)
        (self.repo / "a.txt").write_text("fixed during the run\n")
        git(self.repo, "commit", "-q", "-am", "fix during the run")
        fix = short(self.repo)
        out = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([fix])
        self.assertEqual(out["commits"], [fix, merge])          # the owner's merge is a commit of the run; master's three are not
        self.assertIn("коммитов 2", self.herdr.calls_named("notification_show")[-1][2])

    def test_a_fix_commit_made_before_a_merge_of_the_base_stays_listed(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        run_dir = self.reviewed_run(2, launched_now=True)       # two branch commits, then the launch
        (self.repo / "a.txt").write_text("first fix\n")
        git(self.repo, "commit", "-q", "-am", "first fix")
        fix1 = short(self.repo)
        git(self.repo, "switch", "-q", "master")
        for i in range(3):                                      # master moves on by three commits
            (self.repo / f"m{i}.txt").write_text("master moves on\n")
            git(self.repo, "add", f"m{i}.txt")
            git(self.repo, "commit", "-q", "-m", f"master moves on {i}")
        git(self.repo, "switch", "-q", "feat")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "master")   # the owner merges the base between two fixes
        merge = short(self.repo)
        (self.repo / "a.txt").write_text("second fix\n")
        git(self.repo, "commit", "-q", "-am", "second fix")
        fix2 = short(self.repo)
        out = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([fix1, fix2])
        # newest first: the fix made before the merge is on the first-parent line too; master's three are not
        self.assertEqual(out["commits"], [fix2, merge, fix1])
        self.assertIn("коммитов 3", self.herdr.calls_named("notification_show")[-1][2])

    def test_a_subject_with_a_cr_a_form_feed_and_a_line_separator_is_one_commit(self):
        run_dir = self.reviewed_run(0, launched_now=True)
        (self.repo / "a.txt").write_text("fixed during the run\n")
        git(self.repo, "commit", "-q", "-am", "fix\r one\x0c two\u2028 three")
        out = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
        self.assertEqual(out["commits"], [short(self.repo)])

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

    def test_finish_removes_a_scratch_link_and_leaves_its_target_alone(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        target = self.root / "elsewhere"                                  # outside the run directory
        locked = target / "gomod"
        locked.mkdir(parents=True)
        (locked / "x.go").write_text("package main\n")
        locked.chmod(0o555)
        modes = {p: stat.S_IMODE(p.stat().st_mode) for p in (target, locked)}
        (run_dir / "scratch").symlink_to(target, target_is_directory=True)
        try:
            Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
            self.assertFalse((run_dir / "scratch").is_symlink())
            self.assertEqual({p: stat.S_IMODE(p.stat().st_mode) for p in (target, locked)}, modes)
            self.assertEqual((locked / "x.go").read_text(), "package main\n")
            self.assertIn("finish: scratch/ was a symlink", (run_dir / "runner.log").read_text())
        finally:
            if locked.exists():
                locked.chmod(0o755)                                       # tearDown must be able to remove the temp dir


if __name__ == "__main__":
    unittest.main()
