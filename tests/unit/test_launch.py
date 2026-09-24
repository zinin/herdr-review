import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_review.config import parse_config
from herdr_review.dialogs import CLAUDE_SESSION_SETTINGS, MCP_REFUSAL, MCP_UNCHECKED
from herdr_review.herdr import Herdr
from herdr_review.launch import LaunchError, LaunchOptions, _unfinished_runs, launch, new_run_id, project_slug, resolve_selection
from tests.unit.fakeherdr import FakeHerdr
from tests.unit.test_dialogs import CLAUDE_IDLE, MCP_MANY, MCP_ONE

RAW = {
    "profiles": {
        "claude-opus": {"kind": "claude", "args": ["--model", "opus"], "env": {"ANTHROPIC_AUTH_TOKEN": "s3cret"}},
        "codex": {"kind": "codex", "args": ["-m", "gpt-5.5"]},
        "ghost": {"kind": "nosuchbinary"},
    },
    "presets": {"default": {"reviewers": ["claude-opus", "codex"], "orchestrator": "claude-opus", "fixer": "codex"}},
    "settings": {"checkin_sec": 7},
}
ENV = {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_repo(d: Path) -> Path:
    repo = d / "My Project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "switch", "-q", "-c", "feat/x")
    (repo / "a.txt").write_text("two\n")
    git(repo, "commit", "-q", "-am", "change")
    return repo


def which_ok(kind):
    return None if kind == "nosuchbinary" else f"/usr/bin/{kind}"


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root)
        self.cfg = parse_config(RAW, {})
        self.cfg.settings.runs_dir = self.root / "runs"
        self.herdr = FakeHerdr()
        self.runner = self.root / "bin" / "herdr-review"
        self.runner.parent.mkdir()
        self.runner.write_text("#!/bin/sh\nexit 0\n")

    def tearDown(self):
        self.tmp.cleanup()

    def do_launch(self, **kw):
        opts = LaunchOptions(**kw)
        return launch(opts, self.cfg, self.herdr, ENV, self.repo, self.runner, which=which_ok, run_id="hrtest")

    def test_happy_path_creates_run_and_starts_orchestrator(self):
        res = self.do_launch(description="Added the thing", plan="docs/plan.md")
        run_dir = Path(res["run_dir"])
        self.assertEqual(res["run_id"], "hrtest")
        self.assertEqual(res["orchestrator"], "hrtest-orch")
        self.assertEqual(res["reviewers"], ["claude-opus", "codex"])
        self.assertEqual(res["tab"], "w1:t2")
        self.assertTrue(run_dir.name.endswith("-hrtest"))
        self.assertRegex(run_dir.parent.name, r"^my-project-[0-9a-f]{6}$")
        self.assertEqual((run_dir.parent / "latest").resolve(), run_dir.resolve())
        run_json = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(run_json["branch"], "feat/x")
        self.assertEqual(run_json["base"], "master")
        self.assertEqual(run_json["checkin_sec"], 7)
        self.assertEqual(run_json["workspace_id"], "w1")
        self.assertEqual(run_json["reviewers"][0]["name"], "hrtest-claude-opus")
        self.assertEqual(run_json["reviewers"][0]["env_keys"], ["ANTHROPIC_AUTH_TOKEN"])
        self.assertEqual(run_json["fixer"]["name"], "hrtest-fixer")
        self.assertEqual(run_json["runner"], str(self.runner))
        prompt = (run_dir / "prompts" / "codex.md").read_text()
        self.assertIn(str(run_dir / "reviews" / "codex.md"), prompt)
        self.assertIn("Added the thing", prompt)
        self.assertIn(run_json["merge_base"], prompt)
        orch = (run_dir / "orchestrator.md").read_text()
        self.assertIn(f'"{self.runner}" run wait', orch)
        self.assertIn("hrtest-codex", orch)
        self.assertIn("autodecide: false", orch)
        self.assertIn("Scope: commits — the change is `git diff", orch)
        self.assertIn(f"HEAD at launch `{run_json['head']}`", orch)
        self.assertIn(f"scratch `{run_dir}/scratch/codex/`", orch)
        self.assertIn("(0 entries;", orch)
        for f in ("run.json", "orchestrator.md", "runner.log"):
            self.assertNotIn("s3cret", (run_dir / f).read_text())
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["phase"], "reviewing")
        self.assertEqual(status["orchestrator"]["pane"], "w1:p2")
        tab_call = self.herdr.calls_named("tab_create")[0]
        self.assertEqual(tab_call[3], "rv-hrtest: orch")
        self.assertEqual(tab_call[4]["HERDR_REVIEW_RUN"], str(run_dir))
        self.assertEqual(tab_call[4]["ANTHROPIC_AUTH_TOKEN"], "s3cret")
        self.assertEqual(self.herdr.calls_named("agent_start")[0], ("agent_start", "hrtest-orch", "claude", "w1:p2", ["--settings", CLAUDE_SESSION_SETTINGS, "--model", "opus"]))
        prompt_call = self.herdr.calls_named("agent_prompt")[0]
        self.assertEqual(prompt_call[2], f"Read {run_dir / 'orchestrator.md'} and follow it exactly. Do not stop until the run is finished.")
        self.assertEqual(prompt_call[3:], ("working", 60000))
        self.assertEqual(self.herdr.calls_named("tab_focus"), [])
        self.assertEqual(res["hints"]["focus"], "herdr agent focus hrtest-orch")
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_the_herdr_session_of_the_launch_is_recorded(self):
        socket = "/home/u/.config/herdr/sessions/ai/herdr.sock"
        environ = {**ENV, "HERDR_SESSION": "ai", "HERDR_SOCKET_PATH": socket}
        res = launch(LaunchOptions(), self.cfg, self.herdr, environ, self.repo, self.runner, which=which_ok, run_id="hrtest")
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual((run_json["herdr_session"], run_json["herdr_socket_path"]), ("ai", socket))
        res = launch(LaunchOptions(), self.cfg, FakeHerdr(), ENV, self.repo, self.runner, which=which_ok, run_id="hrbare")
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual((run_json["herdr_session"], run_json["herdr_socket_path"]), (None, None))

    def test_masks_raw_env_var_values_not_only_expanded_profile_env(self):
        raw = {
            "profiles": {
                "claude-opus": {
                    "kind": "claude",
                    "args": ["--model", "opus"],
                    "env": {"ANTHROPIC_AUTH_TOKEN": "Bearer ${ZAI_TOKEN}"},
                },
                "codex": {"kind": "codex", "args": ["-m", "gpt-5.5"]},
            },
            "presets": {"default": {"reviewers": ["claude-opus", "codex"], "orchestrator": "claude-opus", "fixer": "codex"}},
        }
        cfg = parse_config(raw, {"ZAI_TOKEN": "raw-token-xyz"})
        cfg.settings.runs_dir = self.root / "runs"
        env = {**ENV, "ZAI_TOKEN": "raw-token-xyz"}
        launch(LaunchOptions(description="d"), cfg, self.herdr, env, self.repo, self.runner, which=which_ok, run_id="hrtest")
        self.assertIn("raw-token-xyz", self.herdr.mask_values)
        self.assertIn("Bearer raw-token-xyz", self.herdr.mask_values)

    def test_a_short_plain_env_value_stays_readable_in_the_log(self):
        env = {"ANTHROPIC_AUTH_TOKEN": "sk-tiny", "ANTHROPIC_MODEL": "opus", "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}
        raw = {
            "profiles": {
                "claude-opus": {"kind": "claude", "args": ["--model", "opus"], "env": env},
                "codex": {"kind": "codex", "args": ["-m", "gpt-5.5"]},
            },
            "presets": {"default": {"reviewers": ["claude-opus", "codex"], "orchestrator": "claude-opus", "fixer": "codex"}},
        }
        cfg = parse_config(raw, {})
        cfg.settings.runs_dir = self.root / "runs"
        launch(LaunchOptions(), cfg, self.herdr, ENV, self.repo, self.runner, which=which_ok, run_id="hrtest")
        self.assertIn("sk-tiny", self.herdr.mask_values)                      # short, but the name says token
        self.assertIn("https://api.z.ai/api/anthropic", self.herdr.mask_values)   # plain name, but long
        self.assertNotIn("opus", self.herdr.mask_values)                      # short and plain: left alone
        # What that list does to the line the client writes into runner.log:
        lines = []
        Herdr(binary="herdr-fake", log=lines.append, mask_values=self.herdr.mask_values).tab_create(
            "w1", str(self.repo), "rv-hrtest: claude-opus", env)
        self.assertIn("ANTHROPIC_MODEL=opus", lines[0])
        self.assertIn("ANTHROPIC_AUTH_TOKEN=***", lines[0])
        self.assertNotIn("sk-tiny", lines[0])

    def test_a_dirty_tree_stays_outside_a_review_of_the_commits(self):
        (self.repo / "a.txt").write_text("dirty\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "todo.md").write_text("mine\n")
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertEqual(res["scope"], "commits")
        self.assertEqual(res["uncommitted"], [" M a.txt", "?? notes/"])
        self.assertIsNone(res["untracked"])
        self.assertFalse(any("uncommitted" in w for w in res["warnings"]))
        self.assertEqual((run_dir / "uncommitted.txt").read_text(), " M a.txt\n?? notes/\n")
        self.assertFalse((run_dir / "untracked.txt").exists())
        run_json = json.loads((run_dir / "run.json").read_text())
        self.assertEqual((run_json["scope"], run_json["uncommitted"]), ("commits", [" M a.txt", "?? notes/"]))
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(run_json["head"], head)
        prompt = (run_dir / "prompts" / "codex.md").read_text()
        self.assertIn(f"git diff {run_json['merge_base']} HEAD --", prompt)
        self.assertIn("?? notes/", prompt)
        self.assertNotIn("git ls-files --others", prompt)

    def test_nothing_committed_reviews_the_working_tree(self):
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "switch", "-q", "-c", "wip")
        (self.repo / "new.py").write_text("print(1)\n")
        (self.repo / "blob.bin").write_bytes(b"\0\1\2")
        res = self.do_launch()
        self.assertEqual(res["scope"], "worktree")
        self.assertEqual(res["untracked"], {"files": 2, "skipped": 1})
        prompt = (Path(res["run_dir"]) / "prompts" / "codex.md").read_text()
        self.assertIn("- `new.py` (9 B)", prompt)
        self.assertIn("- `blob.bin` (3 B) — skip: binary", prompt)

    def test_the_worktree_scope_lists_every_untracked_file_with_its_marks_on_disk(self):
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "switch", "-q", "-c", "wip")
        for i in range(105):
            (self.repo / f"new{i:03}.py").write_text("print(1)\n")
        (self.repo / "zz.bin").write_bytes(b"\0\1\2")               # sorted last: past the inline cap
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        listing = (run_dir / "untracked.txt").read_text()
        self.assertEqual(len(listing.splitlines()), 106)
        self.assertTrue(listing.startswith("- `new000.py` (9 B)\n"))
        self.assertTrue(listing.endswith("- `zz.bin` (3 B) — skip: binary\n"))
        prompt = (run_dir / "prompts" / "codex.md").read_text()
        self.assertNotIn("zz.bin", prompt)
        self.assertIn(f"…and 6 more: `{run_dir / 'untracked.txt'}` lists them all with the same marks.", prompt)

    def test_the_scope_flag_overrides_the_setting(self):
        (self.repo / "new.py").write_text("x\n")
        self.cfg.settings.scope = "worktree"
        self.assertEqual(self.do_launch()["scope"], "worktree")
        res = launch(LaunchOptions(scope="commits"), self.cfg, FakeHerdr(), ENV, self.repo, self.runner, which=which_ok, run_id="hrcommits")
        self.assertEqual(res["scope"], "commits")

    def test_the_fixer_skeletons_carry_the_commit_rule_of_the_scope(self):
        (self.repo / "new.py").write_text("x\n")
        worktree = launch(LaunchOptions(scope="worktree"), self.cfg, self.herdr, ENV, self.repo, self.runner, which=which_ok, run_id="hrwork")
        orch = (Path(worktree["run_dir"]) / "orchestrator.md").read_text()
        self.assertEqual(orch.count("Commit nothing. The change under review is uncommitted work"), 2)   # both skeletons
        self.assertNotIn("git commit --only", orch)
        commits = launch(LaunchOptions(scope="commits"), self.cfg, FakeHerdr(), ENV, self.repo, self.runner, which=which_ok, run_id="hrcommits")
        orch = (Path(commits["run_dir"]) / "orchestrator.md").read_text()
        self.assertNotIn("Commit nothing.", orch)
        self.assertEqual(orch.count("git commit --only -F"), 2)

    def test_commits_scope_without_commits_is_refused(self):
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "switch", "-q", "-c", "wip")
        (self.repo / "new.py").write_text("x\n")
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch(scope="commits")
        self.assertIn("nothing committed on this branch since master", str(ctx.exception))
        self.assertFalse((self.root / "runs").exists())          # refused before any run directory

    def test_every_reviewer_gets_a_scratch_directory(self):
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertEqual(sorted(p.name for p in (run_dir / "scratch").iterdir()), ["claude-opus", "codex"])
        self.assertIn(str(run_dir / "scratch" / "codex"), (run_dir / "prompts" / "codex.md").read_text())

    def test_a_long_list_of_uncommitted_files_is_capped_in_the_prompt_and_complete_on_disk(self):
        for i in range(60):
            (self.repo / f"junk{i:02}.txt").write_text("x\n")
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertEqual(len((run_dir / "uncommitted.txt").read_text().splitlines()), 60)
        prompt = (run_dir / "prompts" / "codex.md").read_text()
        self.assertIn("?? junk49.txt", prompt)
        self.assertNotIn("?? junk50.txt", prompt)
        self.assertIn(f"…and 10 more: {run_dir / 'uncommitted.txt'} lists them all.", prompt)

    def test_focus_switches_to_the_tab(self):
        self.do_launch(focus=True)
        self.assertEqual(self.herdr.calls_named("tab_focus"), [("tab_focus", "w1:t2")])

    def test_missing_reviewer_binary_is_skipped_with_warning(self):
        res = self.do_launch(reviewers=["claude-opus", "ghost"])
        self.assertEqual(res["reviewers"], ["claude-opus"])
        self.assertEqual(res["skipped"], ["ghost"])
        self.assertTrue(any("ghost" in w for w in res["warnings"]))
        with self.assertRaises(LaunchError):
            self.do_launch(reviewers=["ghost"])
        with self.assertRaises(LaunchError):
            self.do_launch(orchestrator="ghost")

    def test_preflight_failures(self):
        with self.assertRaises(LaunchError):
            launch(LaunchOptions(), self.cfg, self.herdr, {}, self.repo, self.runner, which=which_ok)
        self.herdr.server_running = False
        with self.assertRaises(LaunchError):
            self.do_launch()
        self.herdr.server_running = True
        with self.assertRaises(LaunchError):
            launch(LaunchOptions(), self.cfg, self.herdr, ENV, self.root, self.runner, which=which_ok)
        with self.assertRaises(LaunchError):
            self.do_launch(preset="nope")
        with self.assertRaises(LaunchError):
            self.do_launch(base="no-such-branch")
        git(self.repo, "switch", "-q", "master")
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch()
        self.assertIn("nothing to review", str(ctx.exception))

    def test_selection_resolution(self):
        self.assertEqual(resolve_selection(self.cfg, LaunchOptions()), (["claude-opus", "codex"], "claude-opus", "codex"))
        self.assertEqual(resolve_selection(self.cfg, LaunchOptions(reviewers=["codex"], fixer="claude-opus")), (["codex"], "claude-opus", "claude-opus"))
        with self.assertRaises(LaunchError):
            resolve_selection(self.cfg, LaunchOptions(reviewers=["codex", "codex"]))
        with self.assertRaises(LaunchError):
            resolve_selection(self.cfg, LaunchOptions(reviewers=["zzz"]))
        self.cfg.presets.clear()
        with self.assertRaises(LaunchError):
            resolve_selection(self.cfg, LaunchOptions())
        self.assertEqual(resolve_selection(self.cfg, LaunchOptions(reviewers=["codex"], orchestrator="codex", fixer="codex")), (["codex"], "codex", "codex"))

    def test_layout_and_autodecide_overrides(self):
        res = self.do_launch(layout="grid", autodecide=True)
        self.assertEqual(res["layout"], "grid")
        self.assertTrue(res["autodecide"])
        self.assertIn("autodecide: true", (Path(res["run_dir"]) / "orchestrator.md").read_text())

    def test_stalled_prompt_is_retried_once_then_fails(self):
        self.herdr.prompt_errors["hrtest-orch"] = ("agent_prompt_stalled", "no activity")
        res = self.do_launch()
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 2)
        self.assertEqual(res["run_id"], "hrtest")
        herdr2 = FakeHerdr()
        herdr2.prompt_errors["hrtest2-orch"] = ("agent_prompt_stalled", "no activity")
        original = herdr2.agent_prompt

        def always_stalled(name, text, until=None, timeout_ms=None):
            herdr2.prompt_errors[name] = ("agent_prompt_stalled", "no activity")
            return original(name, text, until, timeout_ms)

        herdr2.agent_prompt = always_stalled
        with self.assertRaises(LaunchError) as ctx:
            launch(LaunchOptions(), self.cfg, herdr2, ENV, self.repo, self.runner, which=which_ok, run_id="hrtest2")
        self.assertIn("herdr agent prompt hrtest2-orch", str(ctx.exception))

    def test_orchestrator_start_failure_mentions_tab_and_screen(self):
        self.herdr.start_errors["hrtest-orch"] = ("agent_start_failed", "kind not installed")
        self.herdr.pane_screens["w1:p2"] = "bash: claude: command not found\n"
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch()
        self.assertIn("w1:t2", str(ctx.exception))
        self.assertIn("command not found", str(ctx.exception))

    def test_failure_after_the_run_directory_exists_marks_the_run_aborted(self):
        self.herdr.start_errors["hrtest-orch"] = ("agent_start_failed", "kind not installed")
        self.herdr.pane_screens["w1:p2"] = "bash: claude: command not found\n"
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch()
        message = str(ctx.exception)
        self.assertIn("Tab w1:t2 is left open for inspection", message)   # the message is untouched
        self.assertIn("Run directory:", message)
        run_dir = next((self.root / "runs").glob("*/*-hrtest"))
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual(status["phase"], "aborted")
        self.assertEqual(status["abort_reason"], message)

    def test_a_failed_write_into_the_run_directory_is_a_launch_error(self):
        real = Path.write_text

        def no_space_for_run_json(path, *args, **kwargs):
            if path.name == "run.json":
                raise OSError(28, "No space left on device")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", no_space_for_run_json):
            with self.assertRaises(LaunchError) as ctx:
                self.do_launch()
        run_dir = next((self.root / "runs").glob("*/*-hrtest"))
        self.assertIn(f"cannot write the run directory {run_dir}", str(ctx.exception))
        self.assertIn("No space left on device", str(ctx.exception))

    def test_unfinished_runs_skips_both_terminal_phases(self):
        project = self.root / "runs" / "some-project"
        for name, phase in (("a", "finished"), ("b", "aborted"), ("c", "starting"), ("d", "reviewing"), ("e", "disputed")):
            d = project / name
            d.mkdir(parents=True)
            (d / "status.json").write_text(json.dumps({"agents": {}, "phase": phase}))
        self.assertEqual(_unfinished_runs(project), ["c (starting)", "d (reviewing)", "e (disputed)"])

    def test_trust_dialog_is_resolved_automatically(self):
        self.herdr.start_errors["hrtest-orch"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-orch"] = "❯ No, exit\n  Yes, I trust this folder\n"
        res = self.do_launch()
        self.assertEqual(res["orchestrator"], "hrtest-orch")
        self.assertIn(("agent_send_keys", "hrtest-orch", ("down", "enter")), self.herdr.calls)
        self.assertEqual(len(self.herdr.calls_named("agent_prompt")), 1)

    def test_tab_create_failure_leaves_latest_untouched_and_names_run_dir(self):
        first = self.do_launch()
        latest = Path(first["run_dir"]).parent / "latest"
        before = latest.resolve()
        herdr2 = FakeHerdr()

        def failing_tab_create(*a, **kw):
            from herdr_review.herdr import HerdrResult
            return HerdrResult(False, 1, error_code="herdr_failed", message="no space")

        herdr2.tab_create = failing_tab_create
        with self.assertRaises(LaunchError) as ctx:
            launch(LaunchOptions(), self.cfg, herdr2, ENV, self.repo, self.runner, which=which_ok, run_id="hrsecond")
        self.assertEqual(latest.resolve(), before)
        self.assertIn("hrsecond", str(ctx.exception))
        self.assertIn("Run directory:", str(ctx.exception))

    def test_unfinished_run_warning(self):
        first = self.do_launch()
        herdr2 = FakeHerdr()
        res = launch(LaunchOptions(), self.cfg, herdr2, ENV, self.repo, self.runner, which=which_ok, run_id="hrsecond")
        self.assertTrue(any("not finished" in w and "hrtest" in w for w in res["warnings"]))
        self.assertNotEqual(res["run_dir"], first["run_dir"])

    def test_run_id_and_slug(self):
        rid = new_run_id()
        self.assertRegex(rid, r"^hr[a-z0-9]{4}$")
        self.assertRegex(project_slug(Path("/x/My Project.Name")), r"^my-project-name-[0-9a-f]{6}$")
        self.assertEqual(project_slug(Path("/x/app")), project_slug(Path("/x/app")))        # stable per path
        self.assertNotEqual(project_slug(Path("/one/app")), project_slug(Path("/two/app")))  # unique per path

    def test_orch_tab_gets_config_path_and_only_the_selected_profiles_env_vars(self):
        raw = {
            "profiles": {
                "claude-opus": {"kind": "claude", "args": ["--model", "opus"], "env": {"ANTHROPIC_AUTH_TOKEN": "s3cret"}},
                "codex": {"kind": "codex", "args": ["-m", "gpt-5.5"], "env": {"OPENAI_API_KEY": "${CODEX_TOKEN}"}},
                "glm": {"kind": "claude", "args": ["--model", "glm-5"], "env": {"ANTHROPIC_AUTH_TOKEN": "${ZAI_TOKEN}"}},
            },
            "presets": {"default": {"reviewers": ["claude-opus", "codex"], "orchestrator": "claude-opus", "fixer": "codex"}},
            "settings": {"checkin_sec": 7},
        }
        environ = {
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_REVIEW_CONFIG": "/custom/herdr-review.yaml",
            "XDG_CONFIG_HOME": "/custom/xdg",
            "CODEX_TOKEN": "codex-secret",
            "ZAI_TOKEN": "zai-secret",
        }
        cfg = parse_config(raw, environ)
        cfg.settings.runs_dir = self.root / "runs"
        res = launch(LaunchOptions(), cfg, self.herdr, environ, self.repo, self.runner, which=which_ok, run_id="hrtest")
        tab_env = self.herdr.calls_named("tab_create")[0][4]
        self.assertEqual(tab_env["HERDR_REVIEW_CONFIG"], "/custom/herdr-review.yaml")
        self.assertEqual(tab_env["XDG_CONFIG_HOME"], "/custom/xdg")
        self.assertEqual(tab_env["CODEX_TOKEN"], "codex-secret")   # a reviewer of this run refers to it
        self.assertNotIn("ZAI_TOKEN", tab_env)                     # glm takes no part in this run
        self.assertEqual(tab_env["ANTHROPIC_AUTH_TOKEN"], "s3cret")
        self.assertEqual(tab_env["HERDR_REVIEW_RUN"], res["run_dir"])

    def test_relative_runs_dir_is_resolved_against_the_launch_cwd(self):
        sub = self.repo / "sub"
        sub.mkdir()
        self.cfg.settings.runs_dir = Path(".review-runs")
        res = launch(LaunchOptions(), self.cfg, self.herdr, ENV, sub, self.runner, which=which_ok, run_id="hrrel")
        run_dir = Path(res["run_dir"])
        self.assertTrue(run_dir.is_absolute())
        self.assertEqual(run_dir.parent.parent, (sub / ".review-runs").resolve())
        run_json = json.loads((run_dir / "run.json").read_text())
        self.assertTrue(Path(run_json["run_dir"]).is_dir())
        self.assertEqual(run_json["run_dir"], str(run_dir))
        self.assertIn(str(run_dir), (run_dir / "orchestrator.md").read_text())

    def test_claude_profiles_start_with_the_session_mcp_setting(self):
        res = self.do_launch()
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual(run_json["reviewers"][0]["args"], ["--settings", CLAUDE_SESSION_SETTINGS, "--model", "opus"])
        self.assertEqual(run_json["reviewers"][1]["args"], ["-m", "gpt-5.5"])            # codex: untouched
        self.assertEqual(run_json["orchestrator"]["args"][:2], ["--settings", CLAUDE_SESSION_SETTINGS])

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        self.cfg.profiles["claude-opus"].args = ["--model", "opus", "--settings=/home/me/claude.json"]
        res = self.do_launch()
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual(run_json["orchestrator"]["args"], ["--model", "opus", "--settings=/home/me/claude.json"])

    def test_mcp_dialog_at_the_orchestrator_aborts_the_launch_with_the_reason(self):
        self.herdr.start_errors["hrtest-orch"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-orch"] = "New MCP server found in this project: dummy\n❯ Continue without using this MCP server\n"
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch()
        message = str(ctx.exception)
        self.assertIn("enableAllProjectMcpServers", message)
        self.assertIn("Tab w1:t2 is left open", message)
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        run_dir = next((self.root / "runs").glob("*/*-hrtest"))
        self.assertEqual(json.loads((run_dir / "status.json").read_text())["phase"], "aborted")

    def test_an_mcp_dialog_herdr_calls_idle_aborts_the_launch_without_a_key(self):
        for i, screen in enumerate((MCP_ONE, MCP_MANY)):
            with self.subTest(screen=screen.splitlines()[0]):
                herdr = FakeHerdr()                                  # agent start succeeds
                herdr.screens[f"hrmcp{i}-orch"] = screen
                with self.assertRaises(LaunchError) as ctx:
                    launch(LaunchOptions(), self.cfg, herdr, ENV, self.repo, self.runner, which=which_ok, run_id=f"hrmcp{i}")
                message = str(ctx.exception)
                self.assertIn(f"failed to start: {MCP_REFUSAL}", message)
                self.assertIn("Tab w1:t2 is left open for inspection", message)
                self.assertIn("Run directory:", message)
                self.assertEqual(herdr.calls_named("agent_send_keys"), [])
                self.assertEqual(herdr.calls_named("agent_prompt"), [])
                self.assertEqual(herdr.calls_named("tab_close"), [])
                run_dir = next((self.root / "runs").glob(f"*/*-hrmcp{i}"))
                self.assertEqual(json.loads((run_dir / "status.json").read_text())["phase"], "aborted")

    def test_a_screen_herdr_cannot_read_after_the_start_aborts_the_launch_without_a_prompt(self):
        self.herdr.reads["hrtest-orch"] = [None, None]                        # agent start succeeds, both looks fail
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch()
        message = str(ctx.exception)
        self.assertIn(f"orchestrator 'claude-opus' failed to start: {MCP_UNCHECKED}", message)
        self.assertIn("Tab w1:t2 is left open for inspection", message)
        self.assertIn("Run directory:", message)
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        self.assertEqual(self.herdr.calls_named("tab_close"), [])
        run_dir = next((self.root / "runs").glob("*/*-hrtest"))
        status = json.loads((run_dir / "status.json").read_text())
        self.assertEqual((status["phase"], status["abort_reason"]), ("aborted", message))

    def test_one_failed_read_after_the_start_is_read_again(self):
        self.herdr.reads["hrtest-orch"] = [None, CLAUDE_IDLE]
        res = self.do_launch()
        self.assertEqual(len(self.herdr.calls_named("agent_read")), 2)
        self.assertEqual(res["orchestrator"], "hrtest-orch")
        self.assertEqual([c[1] for c in self.herdr.calls_named("agent_prompt")], ["hrtest-orch"])


if __name__ == "__main__":
    unittest.main()
