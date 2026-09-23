import unittest

from herdr_review.dialogs import CLAUDE_MCP_SETTINGS, MCP_REFUSAL, DialogOutcome, recognize, resolve_startup_dialog, startup_args
from tests.unit.fakeherdr import FakeHerdr

CLAUDE_TRUST_ON_NO = "Quick safety check … ❯ No, exit\n  Yes, I trust this folder\nEnter to confirm · Esc to cancel\n"
CLAUDE_TRUST_ON_YES = "Quick safety check … ❯ Yes, I trust this folder\n  No, exit\n"
MCP_ONE = (
    "New MCP server found in this project: dummy\n"
    "  Use this MCP server\n  Use this and all future MCP servers in this project\n"
    "❯ Continue without using this MCP server\nEnter to confirm · Esc to cancel\n"
)
MCP_MANY = (
    "2 new MCP servers found in this project\nSelect any you wish to enable.\n"
    "❯ [✔] one\n  [✔] two\n  Enable selected\nSpace to select · Esc to reject all\n"
)
CODEX_TRUST_ON_TRUST = "Trust this folder? Codex can read, edit, and run files here.\n› 1. Trust and continue\n  2. Quit\n  enter continue · esc quit\n"
CODEX_TRUST_ON_QUIT = "Trust this folder? Codex can read, edit, and run files here.\n  1. Trust and continue\n› 2. Quit\n"
GROK_TRUST = "Do you trust the contents of this directory?\n  Yes, proceed   y\n  No, quit   n\n"


class StartupDialogTest(unittest.TestCase):
    def test_trust_dialog_is_confirmed(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_NO
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=True))
        self.assertIn(("agent_send_keys", "hr1-orch", ("down", "enter")), h.calls)
        self.assertIn(("agent_wait", "hr1-orch", "idle", 30000), h.calls)

    def test_cursor_on_yes_sends_enter_only(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        self.assertTrue(resolve_startup_dialog(h, "hr1-orch").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-orch", ("enter",))])

    def test_unknown_dialog_is_left_alone(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Please log in to continue\n"
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_trust_dialog_without_cursor_is_left_to_orchestrator(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Yes, I trust this folder\nNo, exit\n"
        self.assertFalse(resolve_startup_dialog(h, "hr1-orch").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_wait_failure_is_not_resolved(self):
        h = FakeHerdr()
        h.wait_ok = False
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 3)       # MAX_DIALOGS tries, then it gives up

    def test_dialog_still_on_screen_after_wait_is_not_resolved(self):
        h = FakeHerdr()
        h.keep_screen_on_wait = True
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_NO
        self.assertFalse(resolve_startup_dialog(h, "hr1-orch").resolved)

    def test_mcp_dialog_is_refused_without_a_key(self):
        for screen in (MCP_ONE, MCP_MANY):
            with self.subTest(screen=screen.splitlines()[0]):
                h = FakeHerdr()
                h.screens["hr1-rv"] = screen
                self.assertEqual(resolve_startup_dialog(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))
                self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_mcp_dialog_after_the_trust_dialog_is_refused(self):
        h = FakeHerdr()
        h.wait_ok = False                                   # an agent held by a dialog never turns idle
        h.screens["hr1-rv"] = CLAUDE_TRUST_ON_NO
        h.screens_after_wait["hr1-rv"] = [MCP_MANY]
        self.assertEqual(resolve_startup_dialog(h, "hr1-rv").refusal, MCP_REFUSAL)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-rv", ("down", "enter"))])

    def test_codex_trust_dialog(self):
        for screen, keys in ((CODEX_TRUST_ON_TRUST, ("enter",)), (CODEX_TRUST_ON_QUIT, ("up", "enter"))):
            with self.subTest(keys=keys):
                h = FakeHerdr()
                h.screens["hr1-codex"] = screen
                self.assertTrue(resolve_startup_dialog(h, "hr1-codex").resolved)
                self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-codex", keys)])

    def test_grok_trust_dialog(self):
        h = FakeHerdr()
        h.screens["hr1-grok"] = GROK_TRUST
        self.assertTrue(resolve_startup_dialog(h, "hr1-grok").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-grok", ("y",))])


class RecognizeTest(unittest.TestCase):
    def test_known_dialogs(self):
        self.assertEqual(recognize(MCP_ONE), ("claude-mcp", None))
        self.assertEqual(recognize(MCP_MANY), ("claude-mcp", None))
        self.assertEqual(recognize(CLAUDE_TRUST_ON_NO), ("claude-trust", ("down", "enter")))
        self.assertEqual(recognize(CODEX_TRUST_ON_TRUST), ("codex-trust", ("enter",)))
        self.assertEqual(recognize(GROK_TRUST), ("grok-trust", ("y",)))

    def test_an_idle_agent_is_not_a_dialog(self):
        self.assertIsNone(recognize("❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"))
        self.assertIsNone(recognize("› Ask Codex to do anything\n"))


class StartupArgsTest(unittest.TestCase):
    def test_claude_starts_with_the_session_mcp_setting(self):
        args = ["--model", "opus"]
        self.assertEqual(startup_args("claude", args), ["--settings", CLAUDE_MCP_SETTINGS, "--model", "opus"])
        self.assertEqual(args, ["--model", "opus"])          # the profile's own list stays as it was

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        for args in (["--settings", "/x.json"], ["--model", "opus", "--settings=/x.json"]):
            with self.subTest(args=args):
                self.assertEqual(startup_args("claude", args), args)

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(startup_args("codex", ["-m", "gpt-5.5"]), ["-m", "gpt-5.5"])
        self.assertEqual(startup_args("grok", []), [])


if __name__ == "__main__":
    unittest.main()
