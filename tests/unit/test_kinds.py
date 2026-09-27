import json
import unittest
from pathlib import Path

from herdr_review.kinds import CLAUDE_SESSION_SETTINGS, OPENCODE_CONFIG_NAME, opencode_config, startup_args, startup_env


class StartupArgsTest(unittest.TestCase):
    def test_claude_starts_with_the_session_settings(self):
        args = ["--model", "opus"]
        self.assertEqual(startup_args("claude", args), ["--settings", CLAUDE_SESSION_SETTINGS, "--model", "opus"])
        self.assertEqual(args, ["--model", "opus"])          # the profile's own list stays as it was
        self.assertEqual(json.loads(CLAUDE_SESSION_SETTINGS), {"enableAllProjectMcpServers": True, "attribution": {"commit": ""}})

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        for args in (["--settings", "/x.json"], ["--model", "opus", "--settings=/x.json"]):
            with self.subTest(args=args):
                self.assertEqual(startup_args("claude", args), args)

    def test_opencode_starts_with_a_private_server(self):
        args = ["--auto"]
        self.assertEqual(startup_args("opencode", args), ["--standalone", "--auto"])
        self.assertEqual(args, ["--auto"])
        self.assertEqual(startup_args("opencode", []), ["--standalone"])

    def test_an_opencode_profile_that_chose_its_server_is_left_alone(self):
        for args in (
            ["--standalone"],
            ["--auto", "--standalone"],
            ["--server", "http://127.0.0.1:4096"],
            ["--auto", "--server=http://127.0.0.1:4096"],
        ):
            with self.subTest(args=args):
                self.assertEqual(startup_args("opencode", args), args)

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(startup_args("codex", ["-m", "gpt-5.5"]), ["-m", "gpt-5.5"])
        self.assertEqual(startup_args("grok", []), [])
        self.assertEqual(startup_args("codex", ["--server=x"]), ["--server=x"])
        # OpenCode's flags mean nothing to claude: it still gets its settings
        self.assertEqual(startup_args("claude", ["--standalone"]), ["--settings", CLAUDE_SESSION_SETTINGS, "--standalone"])


class StartupEnvTest(unittest.TestCase):
    def test_an_opencode_agent_reads_the_runs_session_config(self):
        self.assertEqual(startup_env("opencode", Path("/runs/p/r1")), {"OPENCODE_CONFIG": "/runs/p/r1/opencode.json"})
        self.assertEqual(startup_env("opencode", "/runs/p/r1"), {"OPENCODE_CONFIG": "/runs/p/r1/opencode.json"})
        self.assertEqual(OPENCODE_CONFIG_NAME, "opencode.json")

    def test_other_kinds_get_nothing(self):
        for kind in ("claude", "codex", "grok", "gemini"):
            with self.subTest(kind=kind):
                self.assertEqual(startup_env(kind, Path("/runs/p/r1")), {})


class OpencodeConfigTest(unittest.TestCase):
    def test_the_run_directory_is_allowed_and_the_question_tool_denied(self):
        cfg = opencode_config(Path("/home/u/.local/state/herdr-review/runs/app-0a1b2c/20260927-120000-r1"))
        self.assertEqual(cfg, {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "external_directory": {"/home/u/.local/state/herdr-review/runs/app-0a1b2c/20260927-120000-r1/*": "allow"},
                "question": "deny",
            },
        })
        self.assertEqual(json.loads(json.dumps(cfg)), cfg)


if __name__ == "__main__":
    unittest.main()
