import json
import unittest

from herdr_review.dialogs import CLAUDE_SESSION_SETTINGS, MCP_REFUSAL, DialogOutcome, mcp_check, recognize, resolve_startup_dialog, startup_args
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
CLAUDE_IDLE = "❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
# A narrow pane of the grid layout wraps a dialog's phrase over two lines, inside the dialog's box.
MCP_ONE_NARROW = (
    "╭──────────────────────────────╮\n"
    "│ New MCP server found in this │\n"
    "│ project: dummy               │\n"
    "│   Use this MCP server        │\n"
    "│ ❯ Continue without using     │\n"
    "│   this MCP server            │\n"
    "╰──────────────────────────────╯\n"
)
MCP_MANY_NARROW = (
    "╭─────────────────────────╮\n"
    "│ 2 new MCP servers found │\n"
    "│ in this project         │\n"
    "│ Select any you wish to  │\n"
    "│ enable.                 │\n"
    "│ ❯ [✔] one               │\n"
    "│   [✔] two               │\n"
    "│   Enable selected       │\n"
    "╰─────────────────────────╯\n"
)
GROK_TRUST_NARROW = (
    "╭───────────────────────────╮\n"
    "│ Do you trust the contents │\n"
    "│ of this directory?        │\n"
    "│   Yes, proceed   y        │\n"
    "│   No, quit   n            │\n"
    "╰───────────────────────────╯\n"
)
CLAUDE_TRUST_NARROW_ON_YES = (                          # the wrap leaves the cursor line without its option's words
    "╭──────────────╮\n"
    "│ Quick safety │\n"
    "│ check …      │\n"
    "│ ❯ Yes, I     │\n"
    "│   trust this │\n"
    "│   folder     │\n"
    "│   No, exit   │\n"
    "╰──────────────╯\n"
)


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

    def test_the_same_dialog_after_a_timed_out_wait_gets_no_second_key(self):
        h = FakeHerdr()
        h.wait_ok = False
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 1)       # its text lingers or it is stuck: never a second key

    def test_the_mcp_dialog_after_a_timed_out_wait_is_refused(self):
        h = FakeHerdr()
        h.wait_results["hr1-rv"] = [False]                  # the wait after the trust answer times out
        h.screens["hr1-rv"] = CLAUDE_TRUST_ON_YES
        h.screens_after_wait["hr1-rv"] = [MCP_ONE]
        self.assertEqual(resolve_startup_dialog(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 1)

    def test_an_agent_slow_to_turn_idle_after_the_answer_is_resolved(self):
        h = FakeHerdr()
        h.wait_results["hr1-orch"] = [False, True]           # the dialog is gone, but idle comes after the first wait
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        h.screens_after_wait["hr1-orch"] = [CLAUDE_IDLE]
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=True))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 1)
        self.assertEqual(len(h.calls_named("agent_wait")), 2)

    def test_an_agent_that_never_turns_idle_after_the_answer_is_not_resolved(self):
        h = FakeHerdr()
        h.wait_results["hr1-orch"] = [False, False]
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        h.screens_after_wait["hr1-orch"] = [CLAUDE_IDLE]
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 1)
        self.assertEqual(len(h.calls_named("agent_wait")), 2)

    def test_the_same_dialog_after_a_failed_or_blank_read_gets_no_second_key(self):
        for gap in (None, ""):                              # a read that failed, a blank screen
            with self.subTest(gap=gap):
                h = FakeHerdr()
                h.wait_ok = False                           # the dialog holds the agent: every wait times out
                h.screens["hr1-grok"] = GROK_TRUST
                h.reads["hr1-grok"] = [GROK_TRUST, gap]     # the first look, then the look after the wait
                self.assertEqual(resolve_startup_dialog(h, "hr1-grok"), DialogOutcome(resolved=False))
                self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-grok", ("y",))])

    def test_a_failed_read_after_an_idle_wait_is_not_a_checked_screen(self):
        for waits, reads in (
            ([True], [CLAUDE_TRUST_ON_YES, None]),                        # idle, then the look fails
            ([False, True], [CLAUDE_TRUST_ON_YES, CLAUDE_IDLE, None]),    # a clean look; the extra wait sees idle, its look fails
        ):
            with self.subTest(waits=waits):
                h = FakeHerdr()
                h.wait_results["hr1-orch"] = list(waits)
                h.reads["hr1-orch"] = list(reads)
                self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
                self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-orch", ("enter",))])

    def test_after_the_extra_wait_only_its_own_look_counts(self):
        h = FakeHerdr()
        h.wait_results["hr1-orch"] = [False, True]
        h.reads["hr1-orch"] = [CLAUDE_TRUST_ON_YES, None, CLAUDE_IDLE]    # the look after the timed-out wait fails, the extra one reads
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=True))
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-orch", ("enter",))])
        self.assertEqual(len(h.calls_named("agent_read")), 3)

    def test_an_mcp_dialog_after_the_extra_wait_is_refused(self):
        h = FakeHerdr()
        h.wait_results["hr1-rv"] = [False, True]            # the answer's wait times out; the extra one sees idle
        h.screens["hr1-rv"] = CLAUDE_TRUST_ON_NO
        h.screens_after_wait["hr1-rv"] = [CLAUDE_IDLE, MCP_MANY]   # the dialog herdr calls idle, up after the extra wait
        self.assertEqual(resolve_startup_dialog(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-rv", ("down", "enter"))])

    def test_a_failed_wait_other_than_a_timeout_sends_no_second_key(self):
        h = FakeHerdr()
        h.wait_ok = False
        h.wait_error = ("server_error", "boom")
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 1)

    def test_dialog_still_on_screen_after_wait_is_not_resolved(self):
        h = FakeHerdr()
        h.keep_screen_on_wait = True
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_NO
        self.assertFalse(resolve_startup_dialog(h, "hr1-orch").resolved)
        self.assertEqual(len(h.calls_named("agent_send_keys")), 1)       # idle, yet the same dialog: never a second key

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

    def test_mcp_dialog_after_an_idle_wait_is_refused(self):
        h = FakeHerdr()
        h.screens["hr1-rv"] = CLAUDE_TRUST_ON_NO
        h.screens_after_wait["hr1-rv"] = [MCP_ONE]
        self.assertEqual(resolve_startup_dialog(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))
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

    def test_a_dialog_a_narrow_pane_wraps_is_refused_or_answered_as_usual(self):
        h = FakeHerdr()
        h.screens["hr1-rv"] = MCP_ONE_NARROW
        self.assertEqual(resolve_startup_dialog(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))
        self.assertEqual(h.calls_named("agent_send_keys"), [])
        h.screens["hr1-grok"] = GROK_TRUST_NARROW
        self.assertTrue(resolve_startup_dialog(h, "hr1-grok").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-grok", ("y",))])

    def test_a_cursor_line_the_wrap_left_without_its_option_gets_no_key(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = CLAUDE_TRUST_NARROW_ON_YES
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))   # blocked-start, as for any unknown layout
        self.assertEqual(h.calls_named("agent_send_keys"), [])


class MCPCheckTest(unittest.TestCase):
    def test_the_screen_after_a_start_is_read_once_more_and_never_passed_unread(self):
        for reads, outcome in (
            ([CLAUDE_IDLE], DialogOutcome(resolved=True)),
            ([""], DialogOutcome(resolved=True)),                             # blank, but read: only a failed read is no look
            ([MCP_MANY], DialogOutcome(resolved=False, refusal=MCP_REFUSAL)),
            ([None, CLAUDE_IDLE], DialogOutcome(resolved=True)),              # one failed read is read again
            ([None, MCP_ONE], DialogOutcome(resolved=False, refusal=MCP_REFUSAL)),
            ([None, None], DialogOutcome(resolved=False)),                    # nothing was checked: neither resolved nor refused
        ):
            with self.subTest(reads=reads):
                h = FakeHerdr()
                h.reads["hr1-rv"] = list(reads)
                self.assertEqual(mcp_check(h, "hr1-rv"), outcome)
                self.assertEqual(len(h.calls_named("agent_read")), len(reads))
                self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_an_mcp_dialog_a_narrow_pane_wraps_is_refused(self):
        h = FakeHerdr()
        h.screens["hr1-rv"] = MCP_MANY_NARROW
        self.assertEqual(mcp_check(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))


class RecognizeTest(unittest.TestCase):
    def test_known_dialogs(self):
        self.assertEqual(recognize(MCP_ONE), ("claude-mcp", None))
        self.assertEqual(recognize(MCP_MANY), ("claude-mcp", None))
        self.assertEqual(recognize(CLAUDE_TRUST_ON_NO), ("claude-trust", ("down", "enter")))
        self.assertEqual(recognize(CODEX_TRUST_ON_TRUST), ("codex-trust", ("enter",)))
        self.assertEqual(recognize(GROK_TRUST), ("grok-trust", ("y",)))
        self.assertEqual(recognize(CLAUDE_TRUST_ON_NO + MCP_ONE), ("claude-mcp", None))

    def test_a_phrase_a_narrow_pane_wraps_inside_the_box(self):
        self.assertEqual(recognize(MCP_ONE_NARROW), ("claude-mcp", None))
        self.assertEqual(recognize(MCP_MANY_NARROW), ("claude-mcp", None))
        self.assertEqual(recognize(GROK_TRUST_NARROW), ("grok-trust", ("y",)))
        self.assertEqual(recognize(CLAUDE_TRUST_NARROW_ON_YES), ("claude-trust", None))   # the cursor is read from the raw lines

    def test_a_cursor_glyph_on_an_unrelated_line_above_the_options(self):
        self.assertEqual(recognize("› Ask Codex to do anything\n" + CODEX_TRUST_ON_QUIT), ("codex-trust", ("up", "enter")))
        self.assertEqual(recognize("❯ ~/src/app\n" + CLAUDE_TRUST_ON_NO), ("claude-trust", ("down", "enter")))

    def test_an_idle_agent_is_not_a_dialog(self):
        self.assertIsNone(recognize("❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"))
        self.assertIsNone(recognize("› Ask Codex to do anything\n"))


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

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(startup_args("codex", ["-m", "gpt-5.5"]), ["-m", "gpt-5.5"])
        self.assertEqual(startup_args("grok", []), [])


if __name__ == "__main__":
    unittest.main()
