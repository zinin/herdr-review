import unittest

from herdr_review.dialogs import try_resolve_startup_dialog
from tests.unit.fakeherdr import FakeHerdr


class StartupDialogTest(unittest.TestCase):
    def test_trust_dialog_is_confirmed(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Quick safety check … ❯ No, exit\n  Yes, I trust this folder\n"
        self.assertTrue(try_resolve_startup_dialog(h, "hr1-orch"))
        self.assertIn(("agent_send_keys", "hr1-orch", ("down", "enter")), h.calls)
        self.assertIn(("agent_wait", "hr1-orch", "idle", 30000), h.calls)

    def test_unknown_dialog_is_left_alone(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Please log in to continue\n"
        self.assertFalse(try_resolve_startup_dialog(h, "hr1-orch"))
        self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_wait_failure_reports_false(self):
        h = FakeHerdr()
        h.wait_ok = False
        h.screens["hr1-orch"] = "Do you trust this folder?"
        self.assertFalse(try_resolve_startup_dialog(h, "hr1-orch"))

    def test_dialog_still_on_screen_after_wait_is_not_resolved(self):
        h = FakeHerdr()
        h.keep_screen_on_wait = True
        h.screens["hr1-orch"] = "Quick safety check … ❯ No, exit\n  Yes, I trust this folder\n"
        self.assertFalse(try_resolve_startup_dialog(h, "hr1-orch"))

    def test_cursor_on_yes_sends_enter_only(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Quick safety check … ❯ Yes, I trust this folder\n  No, exit\n"
        self.assertTrue(try_resolve_startup_dialog(h, "hr1-orch"))
        self.assertIn(("agent_send_keys", "hr1-orch", ("enter",)), h.calls)
        self.assertNotIn(("agent_send_keys", "hr1-orch", ("down", "enter")), h.calls)

    def test_trust_dialog_without_cursor_is_left_to_orchestrator(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Yes, I trust this folder\nNo, exit\n"
        self.assertFalse(try_resolve_startup_dialog(h, "hr1-orch"))
        self.assertEqual(h.calls_named("agent_send_keys"), [])


if __name__ == "__main__":
    unittest.main()
