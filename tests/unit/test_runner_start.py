import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_review import PROMPTS_DIR
from herdr_review.config import parse_config
from herdr_review.herdr import HerdrResult
from herdr_review.runner import Runner, RunnerError, check_review_file
from herdr_review.status import RunStatus
from tests.unit.fakeherdr import FakeHerdr

RAW = {
    "profiles": {
        "claude-opus": {"kind": "claude", "args": ["--model", "opus"], "env": {"TOKEN": "s3cret"}},
        "codex": {"kind": "codex", "args": ["-m", "gpt-5.5"]},
        "gemini": {"kind": "gemini", "args": ["--yolo"]},
    },
    "presets": {"default": {"reviewers": ["claude-opus", "codex", "gemini"], "orchestrator": "claude-opus", "fixer": "codex"}},
}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_repo(d: Path) -> Path:
    repo = d / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def make_run(root: Path, repo: Path, layout="tabs", reviewers=("claude-opus", "codex", "gemini"), close=False) -> Path:
    """Write run.json + status.json the way launch does (Task 10), without herdr."""
    run_dir = root / "runs" / "repo" / "20260908-100000-hrtest"
    (run_dir / "prompts").mkdir(parents=True)
    (run_dir / "reviews").mkdir()
    cfg = parse_config(RAW, {})
    def spec(p, name):
        pr = cfg.profiles[p]
        return {"name": name, "profile": p, "kind": pr.kind, "args": pr.args, "env_keys": sorted(pr.env)}
    run = {
        "run_id": "hrtest", "run_dir": str(run_dir), "repo": str(repo), "project": "repo", "branch": "feat", "base": "master",
        "merge_base": "0" * 40, "description": "d", "plan": "p", "autodecide": False, "layout": layout, "checkin_sec": 300,
        "close_agents_on_finish": close, "workspace_id": "w1",
        "reviewers": [spec(p, f"hrtest-{p}") for p in reviewers],
        "orchestrator": spec("claude-opus", "hrtest-orch"), "fixer": spec("codex", "hrtest-fixer"),
        "runner": "/opt/hr/bin/herdr-review", "started_at": "2026-09-08T10:00:00+0000",
    }
    (run_dir / "run.json").write_text(json.dumps(run))
    for p in reviewers:
        (run_dir / "prompts" / f"{p}.md").write_text(f"review prompt for {p}\n")
    st = RunStatus.create(run_dir, run_id="hrtest", repo=str(repo), layout=layout)
    st.set("orchestrator", {"name": "hrtest-orch", "profile": "claude-opus", "kind": "claude", "tab": "w1:t1", "pane": "w1:p1"})
    st.set_phase("reviewing")
    return run_dir


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root)
        self.herdr = FakeHerdr()
        self.cfg = parse_config(RAW, {})
        patcher = mock.patch("herdr_review.runner.load_config", return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, run_dir: Path) -> Runner:
        return Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)


class StartReviewersTest(RunnerBase):
    def test_tabs_layout_starts_every_reviewer(self):
        run_dir = make_run(self.root, self.repo)
        out = self.runner(run_dir).start_reviewers()
        self.assertEqual({n: a["state"] for n, a in out["agents"].items()}, {"hrtest-claude-opus": "working", "hrtest-codex": "working", "hrtest-gemini": "working"})
        tabs = self.herdr.calls_named("tab_create")
        self.assertEqual([t[3] for t in tabs], ["rv-hrtest: claude-opus", "rv-hrtest: codex", "rv-hrtest: gemini"])
        self.assertEqual(tabs[0][4], {"HERDR_REVIEW_RUN": str(run_dir), "TOKEN": "s3cret"})
        self.assertEqual(tabs[1][4], {"HERDR_REVIEW_RUN": str(run_dir)})
        starts = self.herdr.calls_named("agent_start")
        self.assertEqual(starts[1], ("agent_start", "hrtest-codex", "codex", "w1:p3", ["-m", "gpt-5.5"]))
        prompts = self.herdr.calls_named("agent_prompt")
        self.assertEqual(prompts[1][2], f"Read {run_dir}/prompts/codex.md and follow it exactly.")
        self.assertEqual(prompts[1][3:], ("working", 30000))
        self.assertIn(("tab_rename", "w1:t3", "rv-hrtest: codex ⏳"), self.herdr.calls)
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["phase"], "reviewing")
        self.assertEqual(len(status["tree_hash_before"]), 64)
        a = status["agents"]["hrtest-codex"]
        self.assertEqual((a["role"], a["profile"], a["tab"], a["pane"], a["prompted"], a["retries"]), ("reviewer", "codex", "w1:t3", "w1:p3", True, 0))
        self.assertEqual(a["result_file"], str(run_dir / "reviews" / "codex.md"))
        self.assertNotIn("s3cret", (run_dir / "runner.log").read_text())

    def test_grid_layout_splits_orchestrator_pane(self):
        run_dir = make_run(self.root, self.repo, layout="grid")
        out = self.runner(run_dir).start_reviewers()
        splits = self.herdr.calls_named("pane_split")
        self.assertEqual([(s[1], s[2], s[3]) for s in splits], [("w1:p1", "right", 0.35), ("w1:p2", "down", 0.5), ("w1:p2", "right", 0.5)])
        self.assertEqual(splits[0][5]["TOKEN"], "s3cret")          # row1 belongs to claude-opus
        self.assertNotIn("TOKEN", splits[1][5])                    # row2 belongs to gemini
        self.assertEqual(self.herdr.calls_named("tab_create"), [])
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["agents"]["hrtest-claude-opus"]["pane"], "w1:p2")
        self.assertEqual(status["agents"]["hrtest-codex"]["pane"], "w1:p4")
        self.assertEqual(status["agents"]["hrtest-gemini"]["pane"], "w1:p3")
        self.assertIn(("pane_rename", "w1:p4", "rv-hrtest: codex ⏳"), self.herdr.calls)
        self.assertEqual(out["agents"]["hrtest-gemini"]["tab"], "w1:t1")

    def test_start_failures_are_recorded_not_raised(self):
        run_dir = make_run(self.root, self.repo)
        self.herdr.start_errors["hrtest-codex"] = ("agent_start_failed", "codex: command not found")
        self.herdr.start_errors["hrtest-gemini"] = ("agent_not_ready", "blocked during startup")
        self.herdr.pane_screens["w1:p3"] = "bash: codex: command not found\n"
        out = self.runner(run_dir).start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "failed")
        self.assertIn("command not found", out["agents"]["hrtest-codex"]["reason"])
        self.assertEqual(out["agents"]["hrtest-gemini"]["state"], "blocked-start")
        status = json.loads((run_dir / "status.json").read_text())
        self.assertIn("command not found", status["agents"]["hrtest-codex"]["last_screen"])
        self.assertFalse(status["agents"]["hrtest-gemini"]["prompted"])
        self.assertIn(("tab_rename", "w1:t3", "rv-hrtest: codex ✗"), self.herdr.calls)
        self.assertIn(("tab_rename", "w1:t4", "rv-hrtest: gemini ❓"), self.herdr.calls)
        self.assertEqual([c for c in self.herdr.calls_named("agent_prompt") if c[1] == "hrtest-gemini"], [])

    def test_trust_dialog_resolved_then_prompted(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.herdr.start_errors["hrtest-codex"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-codex"] = "❯ No, exit\n  Yes, I trust this folder\n"
        out = self.runner(run_dir).start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertIn(("agent_send_keys", "hrtest-codex", ("down", "enter")), self.herdr.calls)

    def test_prompt_stalled_and_blocked_prompt_results(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex", "gemini"))
        self.herdr.prompt_errors["hrtest-codex"] = ("agent_prompt_stalled", "no activity")
        self.herdr.prompt_errors["hrtest-gemini"] = ("agent_blocked", "dialog open")
        out = self.runner(run_dir).start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "prompt_stalled")
        self.assertEqual(out["agents"]["hrtest-gemini"]["state"], "blocked")
        status = json.loads((run_dir / "status.json").read_text())
        self.assertTrue(status["agents"]["hrtest-codex"]["prompted"])

    def test_tab_create_failure_is_runner_error(self):
        run_dir = make_run(self.root, self.repo)
        self.herdr.tab_create = lambda *a, **k: HerdrResult(False, 1, error_code="server_error", message="boom")
        with self.assertRaises(RunnerError):
            self.runner(run_dir).start_reviewers()

    def test_partial_tab_create_rolls_back_and_allows_retry(self):
        run_dir = make_run(self.root, self.repo, reviewers=("claude-opus", "codex"))
        orig = self.herdr.tab_create
        n = {"i": 0}

        def flaky(workspace, cwd, label, env, focus=False):
            n["i"] += 1
            if n["i"] == 2:
                return HerdrResult(False, 1, error_code="server_error", message="boom")
            return orig(workspace, cwd, label, env, focus)

        self.herdr.tab_create = flaky
        r = self.runner(run_dir)
        with self.assertRaises(RunnerError):
            r.start_reviewers()
        self.assertEqual(self.herdr.calls_named("tab_close"), [("tab_close", "w1:t2")])
        self.assertEqual(r.status.agents_by_role("reviewer"), {})
        self.herdr.tab_create = orig
        out = r.start_reviewers()
        self.assertEqual(sorted(out["agents"]), ["hrtest-claude-opus", "hrtest-codex"])

    def test_start_reviewers_twice_is_runner_error(self):
        run_dir = make_run(self.root, self.repo)
        r = self.runner(run_dir)
        r.start_reviewers()
        with self.assertRaises(RunnerError) as ctx:
            r.start_reviewers()
        self.assertIn("already started", str(ctx.exception))

    def test_tab_create_without_ids_is_runner_error(self):
        run_dir = make_run(self.root, self.repo)
        self.herdr.tab_create = lambda *a, **k: HerdrResult(True, 0, result={"type": "tab_created"})
        with self.assertRaises(RunnerError) as ctx:
            self.runner(run_dir).start_reviewers()
        self.assertIn("unexpected", str(ctx.exception).lower())

    def test_missing_status_is_runner_error(self):
        with self.assertRaises(RunnerError):
            Runner(self.root / "nope", herdr=self.herdr)

    def test_unavailable_config_fails_when_profile_has_env_keys(self):
        from herdr_review.config import ConfigError
        run_dir = make_run(self.root, self.repo, reviewers=("claude-opus",))
        with mock.patch("herdr_review.runner.load_config", side_effect=ConfigError(["nope"])):
            r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
            with self.assertRaises(RunnerError) as ctx:
                r.start_reviewers()
        self.assertIn("claude-opus", str(ctx.exception))
        self.assertIn("env", str(ctx.exception).lower())
        self.assertEqual(self.herdr.calls_named("tab_create"), [])

    def test_unavailable_config_ok_when_profile_has_no_env(self):
        from herdr_review.config import ConfigError
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        with mock.patch("herdr_review.runner.load_config", side_effect=ConfigError(["nope"])):
            r = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None)
            out = r.start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")

    def test_mcp_dialog_fails_the_reviewer_with_the_reason(self):
        run_dir = make_run(self.root, self.repo, reviewers=("claude-opus",))
        self.herdr.start_errors["hrtest-claude-opus"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-claude-opus"] = "2 new MCP servers found in this project\n❯ [✔] one\n  [✔] two\n"
        self.herdr.pane_screens["w1:p2"] = "2 new MCP servers found in this project\n"
        out = self.runner(run_dir).start_reviewers()
        a = out["agents"]["hrtest-claude-opus"]
        self.assertEqual(a["state"], "failed")
        self.assertIn("enableAllProjectMcpServers", a["reason"])
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        status = json.loads((run_dir / "status.json").read_text())
        self.assertIn("MCP servers found", status["agents"]["hrtest-claude-opus"]["last_screen"])
        self.assertIn(("tab_rename", "w1:t2", "rv-hrtest: claude-opus ✗"), self.herdr.calls)

    def test_codex_trust_dialog_resolved_then_prompted(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.herdr.start_errors["hrtest-codex"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-codex"] = "Trust this folder?\n› 1. Trust and continue\n  2. Quit\n"
        out = self.runner(run_dir).start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertIn(("agent_send_keys", "hrtest-codex", ("enter",)), self.herdr.calls)


class PromptFailFixerTest(RunnerBase):
    def setUp(self):
        super().setUp()
        self.run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.r = self.runner(self.run_dir)
        self.r.start_reviewers()

    def test_prompt_retry_counts_and_file_prompt(self):
        out = self.r.prompt("hrtest-codex", retry=True)
        self.assertEqual(out["state"], "working")
        self.assertEqual(self.r.status.agent("hrtest-codex")["retries"], 1)
        self.assertEqual(self.r.status.agent("hrtest-codex")["collect_retries"], 0)
        self.assertEqual(self.herdr.calls_named("agent_prompt")[-1][2], f"Read {(self.run_dir / 'prompts' / 'codex.md').resolve()} and follow it exactly.")
        extra = self.run_dir / "extra.md"
        extra.write_text("task\n")
        self.r.prompt("hrtest-codex", file=str(extra))
        self.assertEqual(self.herdr.calls_named("agent_prompt")[-1][2], f"Read {extra.resolve()} and follow it exactly.")
        with self.assertRaises(RunnerError):
            self.r.prompt("hrtest-nobody")
        with self.assertRaises(RunnerError) as ctx:
            self.r.prompt("hrtest-codex", file=str(self.run_dir / "missing.md"))
        self.assertIn("not found", str(ctx.exception))

    def test_prompt_refuses_a_collected_reviewer(self):
        self.r.status.set_agent_state("hrtest-codex", "collected")
        before = len(self.herdr.calls_named("agent_prompt"))
        for kwargs in ({}, {"retry": True}):
            with self.assertRaises(RunnerError) as ctx:
                self.r.prompt("hrtest-codex", **kwargs)
            self.assertIn("would discard that review", str(ctx.exception))
        a = self.r.status.agent("hrtest-codex")
        self.assertEqual((a["state"], a["retries"]), ("collected", 0))
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), before)

    def test_prompt_revives_a_failed_agent_and_logs_it(self):
        self.r.fail("hrtest-codex", "quota exceeded")
        out = self.r.prompt("hrtest-codex")
        self.assertEqual(out["state"], "working")
        self.assertIn("hrtest-codex: prompted while failed (manual revival)", (self.run_dir / "runner.log").read_text())

    def test_fail_records_reason_and_screen(self):
        self.herdr.pane_screens["w1:p2"] = "Error: quota exceeded\n"
        out = self.r.fail("hrtest-codex", "quota exceeded")
        self.assertEqual(out, {"name": "hrtest-codex", "state": "failed"})
        a = self.r.status.agent("hrtest-codex")
        self.assertEqual(a["reason"], "quota exceeded")
        self.assertIn("quota", a["last_screen"])
        self.assertIn(("tab_rename", "w1:t2", "rv-hrtest: codex ✗"), self.herdr.calls)

    def test_start_fixer_tabs_layout(self):
        out = self.r.start_fixer()
        self.assertEqual(out["name"], "hrtest-fixer")
        self.assertEqual(out["state"], "idle")
        self.assertEqual(self.herdr.calls_named("tab_create")[-1][3], "rv-hrtest: fixer")
        self.assertEqual(self.herdr.calls_named("agent_start")[-1][1:], ("hrtest-fixer", "codex", "w1:p3", ["-m", "gpt-5.5"]))
        self.assertEqual(self.r.status.data["phase"], "fixing")
        self.assertEqual(self.r.status.agent("hrtest-fixer")["role"], "fixer")
        with self.assertRaises(RunnerError):
            self.r.prompt("hrtest-fixer")          # the fixer needs --file
        task = self.run_dir / "fix-auto.md"
        task.write_text("fix\n")
        self.r.prompt("hrtest-fixer", file=str(task))
        self.assertEqual(self.r.status.agent("hrtest-fixer")["state"], "working")
        with self.assertRaises(RunnerError):
            self.r.start_fixer()                   # already started

    def test_fixer_tab_is_marked_only_after_it_finishes_a_task(self):
        self.r.start_fixer()
        tab = self.r.status.agent("hrtest-fixer")["tab"]
        self.assertNotIn(("tab_rename", tab, "rv-hrtest: fixer ✓"), self.herdr.calls)
        task = self.run_dir / "fix-auto.md"
        task.write_text("fix\n")
        self.r.prompt("hrtest-fixer", file=str(task))
        self.assertIn(("tab_rename", tab, "rv-hrtest: fixer ⏳"), self.herdr.calls)
        self.r._set_state("hrtest-fixer", "done")
        self.assertIn(("tab_rename", tab, "rv-hrtest: fixer ✓"), self.herdr.calls)

    def test_reviewer_in_done_stays_unmarked_until_collect(self):
        tab = self.r.status.agent("hrtest-codex")["tab"]
        self.r._set_state("hrtest-codex", "done")
        self.assertIn(("tab_rename", tab, "rv-hrtest: codex"), self.herdr.calls)

    def test_start_fixer_grid_layout(self):
        run_dir = make_run(self.root / "g", self.repo, layout="grid", reviewers=("codex",))
        r = Runner(run_dir, herdr=FakeHerdr(), poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        out = r.start_fixer()
        self.assertEqual(r.herdr.calls_named("pane_split")[-1][1:4], ("w1:p1", "down", 0.5))
        self.assertEqual(out["pane"], "w1:p3")
        self.assertIn(("pane_rename", "w1:p3", "rv-hrtest: fixer ⏳"), r.herdr.calls)

    def test_start_fixer_malformed_tab_response_raises_runner_error(self):
        self.herdr.tab_create = lambda *a, **k: HerdrResult(True, 0, result={"tab": {}})
        with self.assertRaises(RunnerError) as ctx:
            self.r.start_fixer()
        self.assertIn("unexpected `tab create` response", str(ctx.exception))

    def test_start_fixer_malformed_pane_response_raises_runner_error(self):
        run_dir = make_run(self.root / "g", self.repo, layout="grid", reviewers=("codex",))
        r = Runner(run_dir, herdr=FakeHerdr(), poll_sec=0, sleep=lambda s: None)
        r.start_reviewers()
        r.herdr.pane_split = lambda *a, **k: HerdrResult(True, 0, result={"pane": {}})
        with self.assertRaises(RunnerError) as ctx:
            r.start_fixer()
        self.assertIn("unexpected `pane split` response", str(ctx.exception))

    def test_notify_and_phase(self):
        out = self.r.notify("herdr-review: нужен ответ", body="1/2: x", sound="request")
        self.assertEqual(out, {"shown": True, "reason": "shown"})
        self.assertTrue(self.r.status.data["waiting_for_user"])
        self.assertIn(("notification_show", "herdr-review: нужен ответ", "1/2: x", "request"), self.herdr.calls)
        self.assertIn(("tab_rename", "w1:t1", "rv-hrtest: orch ❓"), self.herdr.calls)
        out = self.r.phase("disputed")
        self.assertEqual(out, {"phase": "disputed"})
        self.assertFalse(self.r.status.data["waiting_for_user"])
        self.assertIn(("tab_rename", "w1:t1", "rv-hrtest: orch"), self.herdr.calls)
        with self.assertRaises(RunnerError):
            self.r.phase("bogus")
        with self.assertRaises(RunnerError):
            self.r.phase("finished")
        with self.assertRaises(RunnerError) as ctx:
            self.r.phase("aborted")
        self.assertIn("set by launch", str(ctx.exception))

    def test_autodecide_switches_the_run_and_clears_the_question(self):
        self.r.notify("herdr-review: нужен ответ", body="1/2: x", sound="request")
        out = self.r.autodecide()
        self.assertTrue(out["autodecide"])
        self.assertFalse(out["was"])
        self.assertTrue(out["switched_at"])
        self.assertTrue(self.r.status.data["autodecide"])
        self.assertEqual(self.r.status.data["autodecide_switched_at"], out["switched_at"])
        self.assertFalse(self.r.status.data["waiting_for_user"])
        self.assertIn(("tab_rename", "w1:t1", "rv-hrtest: orch"), self.herdr.calls)

    def test_autodecide_from_another_process_survives_a_later_save(self):
        holder = self.r                                    # loaded its snapshot in setUp, as `run wait` does
        switcher = self.runner(self.run_dir)               # the user's own `run autodecide`
        switched = switcher.autodecide()
        holder.phase("aggregating")                        # an unrelated change, saved afterwards
        on_disk = json.loads((self.run_dir / "status.json").read_text())
        self.assertTrue(on_disk["autodecide"])
        self.assertEqual(on_disk["autodecide_switched_at"], switched["switched_at"])
        self.assertEqual(on_disk["phase"], "aggregating")
        self.assertIn("hrtest-codex", on_disk["agents"])
        self.assertTrue(holder.status.data["autodecide"])  # and it now sees the switch

    def test_autodecide_twice_keeps_the_first_switch(self):
        first = self.r.autodecide()
        second = self.r.autodecide()
        self.assertFalse(first["was"])
        self.assertTrue(second["was"])
        self.assertEqual(second["switched_at"], first["switched_at"])


class CheckReviewFileTest(unittest.TestCase):
    def test_check_review_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.md"
            self.assertEqual(check_review_file(p), (False, "file missing"))
            p.write_text("  \n")
            self.assertEqual(check_review_file(p), (False, "file empty"))
            p.write_text("### Strengths\n### Critical Issues\n### Important Issues\n")
            ok, why = check_review_file(p)
            self.assertFalse(ok)
            self.assertIn("### Minor Issues", why)
            self.assertIn("### Assessment", why)
            p.write_text("### Strengths\nx\n### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\nReady: Yes\n")
            self.assertEqual(check_review_file(p), (True, ""))

    def test_a_review_without_strengths_is_still_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.md"
            p.write_text("### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\n**Ready to merge:** Yes\n")
            self.assertEqual(check_review_file(p), (True, ""))

    def test_a_verbatim_copy_of_the_output_format_block_is_not_a_review(self):
        template = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
        block = "### Strengths" + template.split("### Strengths", 1)[1].split("\n## Rules", 1)[0]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.md"
            p.write_text(block)
            ok, why = check_review_file(p)
            self.assertFalse(ok)
            self.assertIn("### Assessment", why)

    def test_an_assessment_of_blank_lines_is_not_a_review(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.md"
            p.write_text("### Strengths\nx\n### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\n\n   \n")
            ok, why = check_review_file(p)
            self.assertFalse(ok)
            self.assertIn("### Assessment", why)

    def test_check_review_file_ignores_heading_mentioned_mid_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.md"
            p.write_text(
                "### Strengths\nx\n"
                "some text mentions ### Critical Issues inline, not as a heading\n"
                "### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\nReady: Yes\n"
            )
            ok, why = check_review_file(p)
            self.assertFalse(ok)
            self.assertIn("### Critical Issues", why)


if __name__ == "__main__":
    unittest.main()
