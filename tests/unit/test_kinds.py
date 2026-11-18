import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_review.kinds import OPENCODE_CONFIG_NAME, left_out_add_dirs, opencode_config, startup_args, startup_env

RUNS = "/home/u/.local/state/herdr-review/runs"
REPO = "/home/u/src/app"
SESSION_SETTINGS = {"enableAllProjectMcpServers": True, "attribution": {"commit": ""}, "permissions": {"additionalDirectories": [RUNS]}}


class StartupArgsTest(unittest.TestCase):
    def claude(self, args, runs_dir=RUNS, agent_dir=REPO):
        """startup_args for a claude profile: its session settings, parsed, and the args after them."""
        out = startup_args("claude", args, Path(runs_dir), Path(agent_dir))
        self.assertEqual(out[0], "--settings")
        return json.loads(out[1]), out[2:]

    def test_claude_starts_with_the_session_settings(self):
        args = ["--model", "opus"]
        self.assertEqual(self.claude(args), (SESSION_SETTINGS, ["--model", "opus"]))
        self.assertEqual(args, ["--model", "opus"])          # the profile's own list stays as it was

    def test_the_runs_directory_needs_no_add_dir_of_the_profile(self):
        # Claude Code asks at startup whether to trust a directory passed with --add-dir; the session settings grant
        # the runs directory without that dialog, so the flag that names it, or a directory inside it, goes.
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            for args, rest in (
                (["--model", "opus", "--add-dir", "~/.local/state/herdr-review/runs"], ["--model", "opus"]),
                (["--add-dir", RUNS, "--model", "opus"], ["--model", "opus"]),
                (["--add-dir=" + RUNS, "--model", "opus"], ["--model", "opus"]),
                (["--add-dir", RUNS + "/"], []),
                (["--add-dir", RUNS + "/app-0a1b2c/20261118-120856-hriq9i"], []),
                # --add-dir takes several directories: only the runs directory leaves the list
                (["--add-dir", "/srv/data", RUNS, "--model", "opus"], ["--add-dir", "/srv/data", "--model", "opus"]),
                (["--add-dir", "/srv/data"], ["--add-dir", "/srv/data"]),
                (["--add-dir=/srv/data"], ["--add-dir=/srv/data"]),
                (["--add-dir", "/home/u/.local/state"], ["--add-dir", "/home/u/.local/state"]),       # a parent grants more
                (["--add-dir", RUNS + "-old"], ["--add-dir", RUNS + "-old"]),                         # a sibling, not inside
            ):
                with self.subTest(args=args):
                    self.assertEqual(self.claude(args), (SESSION_SETTINGS, rest))

    def test_an_add_dir_through_a_symlink_to_the_runs_directory_goes_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp, "state", "runs")
            runs.mkdir(parents=True)
            Path(tmp, "link").symlink_to(Path(tmp, "state"))
            settings, rest = self.claude(["--add-dir", f"{tmp}/link/runs"], runs_dir=runs.resolve())
            self.assertEqual(rest, [])

    def test_a_runs_directory_given_with_a_tilde_is_granted_as_an_absolute_path(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            self.assertEqual(self.claude(["--add-dir", "~/.local/state/herdr-review/runs"],
                                         runs_dir="~/.local/state/herdr-review/runs"), (SESSION_SETTINGS, []))

    def test_a_relative_add_dir_counts_from_the_agents_directory(self):
        # Claude Code counts a relative value from the directory the agent starts in
        for args, rest in (
            (["--add-dir", "../review-runs"], []),
            (["--add-dir", "../review-runs/x"], []),
            (["--add-dir", "../other"], ["--add-dir", "../other"]),
            (["--add-dir", "review-runs"], ["--add-dir", "review-runs"]),        # it names /home/u/src/app/review-runs
        ):
            with self.subTest(args=args):
                settings, after = self.claude(args, runs_dir="/home/u/src/review-runs", agent_dir="/home/u/src/app")
                self.assertEqual(after, rest)
        # an unknown ~user does not break the start
        self.assertEqual(self.claude(["--add-dir", "~hr-no-such-user/runs"])[1], ["--add-dir", "~hr-no-such-user/runs"])

    def test_left_out_add_dirs_names_what_startup_args_leaves_out(self):
        args = ["--add-dir", "/srv/data", RUNS, "--add-dir=" + RUNS + "/x", "--model", "opus"]
        self.assertEqual(left_out_add_dirs("claude", args, Path(RUNS), Path(REPO)), [RUNS, RUNS + "/x"])
        self.assertEqual(left_out_add_dirs("claude", ["--model", "opus"], Path(RUNS), Path(REPO)), [])
        self.assertEqual(left_out_add_dirs("claude", ["--settings", "/x.json", "--add-dir", RUNS], Path(RUNS), Path(REPO)), [])
        self.assertEqual(left_out_add_dirs("codex", ["--add-dir", RUNS], Path(RUNS), Path(REPO)), [])

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        # its settings file replaces the session settings, so its --add-dir is the only grant it has
        for args in (["--settings", "/x.json"], ["--model", "opus", "--settings=/x.json"], ["--settings", "/x.json", "--add-dir", RUNS]):
            with self.subTest(args=args):
                self.assertEqual(startup_args("claude", args, Path(RUNS), Path(REPO)), args)

    def test_opencode_starts_with_a_private_server(self):
        args = ["--auto"]
        self.assertEqual(startup_args("opencode", args, Path(RUNS), Path(REPO)), ["--standalone", "--auto"])
        self.assertEqual(args, ["--auto"])
        self.assertEqual(startup_args("opencode", [], Path(RUNS), Path(REPO)), ["--standalone"])

    def test_an_opencode_profile_that_chose_its_server_is_left_alone(self):
        for args in (
            ["--standalone"],
            ["--auto", "--standalone"],
            ["--server", "http://127.0.0.1:4096"],
            ["--auto", "--server=http://127.0.0.1:4096"],
        ):
            with self.subTest(args=args):
                self.assertEqual(startup_args("opencode", args, Path(RUNS), Path(REPO)), args)

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(startup_args("codex", ["-m", "gpt-5.5"], Path(RUNS), Path(REPO)), ["-m", "gpt-5.5"])
        self.assertEqual(startup_args("grok", [], Path(RUNS), Path(REPO)), [])
        self.assertEqual(startup_args("codex", ["--server=x"], Path(RUNS), Path(REPO)), ["--server=x"])
        # the codex sandbox needs its --add-dir for the runs directory
        self.assertEqual(startup_args("codex", ["--add-dir", RUNS], Path(RUNS), Path(REPO)), ["--add-dir", RUNS])
        # OpenCode's flags mean nothing to claude: it still gets its settings
        self.assertEqual(self.claude(["--standalone"]), (SESSION_SETTINGS, ["--standalone"]))


class StartupEnvTest(unittest.TestCase):
    def test_an_opencode_agent_reads_the_runs_session_config_and_none_of_the_repositorys(self):
        expected = {"OPENCODE_CONFIG": "/runs/p/r1/opencode.json", "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
        self.assertEqual(startup_env("opencode", Path("/runs/p/r1")), expected)
        self.assertEqual(startup_env("opencode", "/runs/p/r1"), expected)
        self.assertEqual(OPENCODE_CONFIG_NAME, "opencode.json")

    def test_other_kinds_get_nothing(self):
        for kind in ("claude", "codex", "grok", "gemini"):
            with self.subTest(kind=kind):
                self.assertEqual(startup_env(kind, Path("/runs/p/r1")), {})


class OpencodeConfigTest(unittest.TestCase):
    def test_the_run_directory_is_allowed_and_the_question_tool_and_env_files_denied(self):
        cfg = opencode_config(Path("/home/u/.local/state/herdr-review/runs/app-0a1b2c/20260927-120000-r1"))
        self.assertEqual(cfg, {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "external_directory": {"/home/u/.local/state/herdr-review/runs/app-0a1b2c/20260927-120000-r1/*": "allow"},
                "read": {"*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
                "question": "deny",
            },
        })
        # OpenCode takes the last rule that matches: the examples' allow must come after the denials
        self.assertEqual(list(cfg["permission"]["read"]), ["*.env", "*.env.*", "*.env.example"])
        self.assertEqual(json.loads(json.dumps(cfg)), cfg)


if __name__ == "__main__":
    unittest.main()
