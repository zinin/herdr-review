import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from herdr_review.status import RunStatus, StatusError, now_iso


class RunStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        (self.run_dir / "run.json").write_text(json.dumps({"run_id": "hrtest"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_writes_skeleton(self):
        s = RunStatus.create(self.run_dir, run_id="hrtest", repo="/r")
        data = json.loads((self.run_dir / "status.json").read_text())
        self.assertEqual(data["phase"], "starting")
        self.assertEqual(data["run_id"], "hrtest")
        self.assertEqual(data["agents"], {})
        self.assertFalse(data["drift"])
        self.assertFalse(data["waiting_for_user"])
        self.assertIsNone(data["finished_at"])
        self.assertTrue(data["started_at"].endswith("+00:00"))
        self.assertEqual(s.run_json()["run_id"], "hrtest")

    def test_load_missing_or_invalid_raises(self):
        with self.assertRaises(StatusError):
            RunStatus.load(self.run_dir)
        (self.run_dir / "status.json").write_text("{not json")
        with self.assertRaises(StatusError):
            RunStatus.load(self.run_dir)

    def test_agent_lifecycle(self):
        s = RunStatus.create(self.run_dir)
        s.add_agent("hrtest-codex", role="reviewer", profile="codex", kind="codex", state="starting", tab="w1:t2", pane="w1:p3")
        a = s.agent("hrtest-codex")
        self.assertEqual(a["state"], "starting")
        first_since = a["state_since"]
        s.set_agent_state("hrtest-codex", "starting")
        self.assertEqual(s.agent("hrtest-codex")["state_since"], first_since)
        later = (datetime.fromisoformat(first_since) + timedelta(seconds=2)).isoformat(timespec="seconds")
        with mock.patch("herdr_review.status.now_iso", return_value=later):
            s.set_agent_state("hrtest-codex", "failed", reason="boom", last_screen="screen")
        a = s.agent("hrtest-codex")
        self.assertEqual(a["state"], "failed")
        self.assertNotEqual(a["state_since"], first_since)
        self.assertEqual(a["reason"], "boom")
        self.assertEqual(a["last_screen"], "screen")
        s.set_agent_state("hrtest-codex", "failed")
        self.assertEqual(s.agent("hrtest-codex")["reason"], "boom")
        self.assertGreaterEqual(s.since_sec("hrtest-codex"), 0)
        self.assertEqual(list(s.agents_by_role("reviewer")), ["hrtest-codex"])
        self.assertEqual(s.agents_by_role("fixer"), {})
        with self.assertRaises(StatusError):
            s.agent("nope")
        with self.assertRaises(StatusError):
            s.set_agent_state("hrtest-codex", "not-a-state")

    def test_save_is_atomic_and_reloadable(self):
        s = RunStatus.create(self.run_dir)
        s.set_phase("reviewing")
        self.assertFalse((self.run_dir / "status.json.tmp").exists())
        again = RunStatus.load(self.run_dir)
        self.assertEqual(again.data["phase"], "reviewing")
        with self.assertRaises(StatusError):
            s.set_phase("bogus")

    def test_save_keeps_an_agent_another_process_added(self):
        first = RunStatus.create(self.run_dir, run_id="hrtest")
        other = RunStatus.load(self.run_dir)
        other.add_agent("hrtest-codex", role="reviewer", profile="codex", kind="codex", state="starting")
        first.set_phase("reviewing")                       # launch's last save, on a stale snapshot
        on_disk = json.loads((self.run_dir / "status.json").read_text())
        self.assertIn("hrtest-codex", on_disk["agents"])
        self.assertEqual(on_disk["phase"], "reviewing")
        self.assertIn("hrtest-codex", first.data["agents"])

    def test_save_merges_a_field_another_process_changed(self):
        first = RunStatus.create(self.run_dir, run_id="hrtest")
        other = RunStatus.load(self.run_dir)
        other.set("autodecide", True)
        other.set("autodecide_switched_at", "2026-09-10T16:33:00+00:00")
        other.save()
        first.set_phase("aggregating")
        on_disk = json.loads((self.run_dir / "status.json").read_text())
        self.assertTrue(on_disk["autodecide"])
        self.assertEqual(on_disk["autodecide_switched_at"], "2026-09-10T16:33:00+00:00")
        self.assertEqual(on_disk["phase"], "aggregating")
        self.assertTrue(first.data["autodecide"])          # the saver continues from fresh state

    def test_removed_agent_stays_removed_after_a_merge(self):
        s = RunStatus.create(self.run_dir, run_id="hrtest")
        s.add_agent("hrtest-codex", role="reviewer", profile="codex", kind="codex", state="starting")
        other = RunStatus.load(self.run_dir)               # the file still lists the agent
        other.set("drift", True)
        other.save()
        s.remove_agent("hrtest-codex")
        s.save()
        on_disk = json.loads((self.run_dir / "status.json").read_text())
        self.assertNotIn("hrtest-codex", on_disk["agents"])
        self.assertTrue(on_disk["drift"])
        self.assertNotIn("hrtest-codex", s.data["agents"])

    def test_a_stale_agent_save_cannot_restore_the_codex_update_budget(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", update_restarts=0)
        stale = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex")["update_restarts"] = 1
        owner.mark_agent("hrtest-codex")
        owner.save()
        stale.agent("hrtest-codex")["screen_hash"] = "older-observation"
        stale.mark_agent("hrtest-codex")
        stale.save()
        on_disk = json.loads((self.run_dir / "status.json").read_text())
        self.assertEqual(on_disk["agents"]["hrtest-codex"]["update_restarts"], 1)

    def test_a_stale_save_cannot_reopen_a_confirmed_codex_session(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_startup_closed=False,
                        codex_startup_pending=True, codex_update_pending=True)
        stale = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex").update(codex_startup_closed=True, codex_startup_pending=False,
                                           codex_update_pending=False)
        owner.mark_agent("hrtest-codex")
        owner.save()
        stale.agent("hrtest-codex")["screen_hash"] = "older-observation"
        stale.mark_agent("hrtest-codex")
        stale.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertTrue(a["codex_startup_closed"])
        self.assertFalse(a["codex_startup_pending"])
        self.assertFalse(a["codex_update_pending"])

    def test_a_stale_startup_save_preserves_the_closed_session_lifecycle(self):
        for local_closed in (False, True):
            with self.subTest(local_closed=local_closed):
                owner = RunStatus.create(self.run_dir)
                owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="prompt_stalled",
                                codex_startup_pending=True, codex_startup_closed=False,
                                codex_update_seen=True, codex_update_pending=True,
                                codex_update_deadline=1_800_000_600.0, retries=1)
                stale = RunStatus.load(self.run_dir)
                expected = {
                    "state": "working", "state_since": "2026-10-04T14:00:00+00:00",
                    "prompted": True, "reason": None, "last_screen": None,
                    "screen_hash": "current session hash", "codex_launch_generation": 0,
                    "codex_startup_closed": True, "codex_startup_pending": False,
                    "codex_update_seen": False, "codex_update_pending": False,
                    "codex_update_deadline": None,
                }
                owner.agent("hrtest-codex").update(expected, retries=2, collect_retries=1)
                owner.mark_agent("hrtest-codex")
                owner.save()
                stale.agent("hrtest-codex").update(codex_startup_closed=local_closed,
                                                  reason="old installer", last_screen="old screen",
                                                  screen_hash="old installer hash", retries=3)
                stale.mark_agent("hrtest-codex")
                stale.save()
                a = RunStatus.load(self.run_dir).agent("hrtest-codex")
                self.assertEqual({key: a[key] for key in expected}, expected)
                self.assertEqual((a["retries"], a["collect_retries"]), (4, 1))
                self.assertEqual(stale.agent("hrtest-codex"), a)

    def test_a_stale_menu_save_preserves_a_running_installer(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="idle",
                        codex_startup_pending=True, codex_update_seen=True, codex_update_pending=False)
        stale = RunStatus.load(self.run_dir)
        expected = {"state": "prompt_stalled", "prompted": False, "reason": "Codex startup update is running",
                    "codex_update_pending": True, "codex_update_seen": True, "codex_startup_pending": True,
                    "codex_update_deadline": 1_800_000_600.0, "screen_hash": "installer hash"}
        owner.agent("hrtest-codex").update(expected)
        owner.mark_agent("hrtest-codex")
        owner.save()
        stale.agent("hrtest-codex")["screen_hash"] = "menu hash"
        stale.mark_agent("hrtest-codex")
        stale.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual({key: a[key] for key in expected}, expected)
        self.assertEqual(stale.agent("hrtest-codex"), a)

    def test_overlapping_update_observations_keep_the_earliest_deadline(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_startup_pending=True,
                        codex_update_seen=True, codex_update_pending=False)
        earlier = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex").update(codex_update_pending=True, codex_update_deadline=1_800_000_700.0)
        owner.mark_agent("hrtest-codex")
        owner.save()
        earlier.agent("hrtest-codex").update(codex_update_pending=True, codex_update_deadline=1_800_000_600.0)
        earlier.mark_agent("hrtest-codex")
        earlier.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertTrue(a["codex_update_pending"])
        self.assertEqual(a["codex_update_deadline"], 1_800_000_600.0)

    def test_a_stale_installer_save_preserves_a_terminal_startup_result(self):
        for state in ("failed", "gone"):
            with self.subTest(state=state):
                owner = RunStatus.create(self.run_dir)
                owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="prompt_stalled",
                                codex_startup_pending=True, codex_update_seen=True, codex_update_pending=True)
                stale = RunStatus.load(self.run_dir)
                owner.agent("hrtest-codex")["codex_update_pending"] = False
                owner.set_agent_state("hrtest-codex", state, reason="installation failed", last_screen="installer error")
                stale.agent("hrtest-codex")["screen_hash"] = "old installer hash"
                stale.mark_agent("hrtest-codex")
                stale.save()
                a = RunStatus.load(self.run_dir).agent("hrtest-codex")
                self.assertEqual(a["state"], state)
                self.assertFalse(a["codex_update_pending"])
                self.assertEqual(a["reason"], "installation failed")
                self.assertEqual(a["last_screen"], "installer error")

    def test_a_stale_menu_save_cannot_revive_a_failed_installer(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="idle",
                        codex_startup_pending=True, codex_update_seen=True, codex_update_pending=False)
        stale = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex")["codex_update_pending"] = True
        owner.set_agent_state("hrtest-codex", "prompt_stalled")
        owner.agent("hrtest-codex")["codex_update_pending"] = False
        owner.set_agent_state("hrtest-codex", "failed", reason="installation failed", last_screen="installer error")
        stale.agent("hrtest-codex")["screen_hash"] = "menu hash"
        stale.mark_agent("hrtest-codex")
        stale.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual(a["state"], "failed")
        self.assertFalse(a["codex_update_pending"])
        self.assertEqual(a["reason"], "installation failed")
        self.assertEqual(a["last_screen"], "installer error")

    def test_a_stale_startup_save_keeps_same_phase_prompt_delivery(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="prompt_stalled",
                        codex_startup_pending=True, codex_update_seen=True, codex_update_pending=False)
        stale = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex")["prompted"] = True
        owner.mark_agent("hrtest-codex")
        owner.save()
        stale.agent("hrtest-codex")["screen_hash"] = "menu hash"
        stale.mark_agent("hrtest-codex")
        stale.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual(a["state"], "prompt_stalled")
        self.assertTrue(a["prompted"])

    def test_generation_check_adopts_same_generation_startup_progress(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                owner = RunStatus.create(self.run_dir)
                owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="idle",
                                codex_startup_pending=True, codex_update_seen=True, codex_update_pending=False)
                stale = RunStatus.load(self.run_dir)
                owner.agent("hrtest-codex").update(state="prompt_stalled", codex_update_pending=True,
                                                 codex_update_deadline=1_800_000_600.0, retries=1, collect_retries=1)
                owner.mark_agent("hrtest-codex")
                owner.save()
                if owned:
                    stale.agent("hrtest-codex")["retries"] += 1
                    stale.mark_agent("hrtest-codex")
                self.assertTrue(stale.codex_generation_current("hrtest-codex", 0))
                a = stale.agent("hrtest-codex")
                self.assertEqual(a["state"], "prompt_stalled")
                self.assertTrue(a["codex_update_pending"])
                self.assertEqual(a["codex_update_deadline"], 1_800_000_600.0)
                self.assertEqual((a["retries"], a["collect_retries"]), (2 if owned else 1, 1))
                self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex"), a)

    def test_a_state_write_reports_a_concurrent_startup_closure(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", state="prompt_stalled",
                        codex_startup_pending=True, codex_startup_closed=False)
        stale = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex").update(codex_startup_closed=True, prompted=True, state="working")
        owner.mark_agent("hrtest-codex")
        owner.save()
        self.assertFalse(stale.set_agent_state("hrtest-codex", "failed", reason="old installer", last_screen="old screen"))
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual(a["state"], "working")
        self.assertIsNone(a["reason"])
        self.assertIsNone(a["last_screen"])

    def test_a_closed_session_accepts_subsequent_lifecycle_writes(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_startup_closed=True,
                        prompted=True, state="working")
        current = RunStatus.load(self.run_dir)
        self.assertTrue(current.set_agent_state("hrtest-codex", "idle"))
        current.agent("hrtest-codex")["result_ok"] = True
        self.assertTrue(current.set_agent_state("hrtest-codex", "collected"))
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual(a["state"], "collected")
        self.assertTrue(a["result_ok"])
        self.assertTrue(a["prompted"])

    def test_only_one_stale_snapshot_can_claim_a_codex_restart(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", update_restarts=0)
        stale = RunStatus.load(self.run_dir)
        claim = getattr(owner, "claim_codex_update_restart", None)
        self.assertIsNotNone(claim, "atomic restart claim is missing")
        self.assertTrue(claim("hrtest-codex"))
        self.assertFalse(stale.claim_codex_update_restart("hrtest-codex"))
        self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex")["update_restarts"], 1)

    def test_concurrent_claims_serialize_the_budget_read_and_write(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", update_restarts=0)
        other = RunStatus.load(self.run_dir)
        claim = getattr(owner, "claim_codex_update_restart", None)
        self.assertIsNotNone(claim, "atomic restart claim is missing")
        writing, release, second_started = Event(), Event(), Event()
        original_write = owner._write

        def paused_write(document):
            writing.set()
            if not release.wait(5):
                raise AssertionError("claim writer was not released")
            original_write(document)

        def second_claim():
            second_started.set()
            return other.claim_codex_update_restart("hrtest-codex")

        owner._write = paused_write
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(claim, "hrtest-codex")
            try:
                self.assertTrue(writing.wait(5))
                second = pool.submit(second_claim)
                self.assertTrue(second_started.wait(5))
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5))
            self.assertFalse(second.result(timeout=5))
        self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex")["update_restarts"], 1)
        self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex").get("codex_launch_generation", 0), 1)

    def test_a_restart_claim_advances_a_legacy_generation_atomically(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", update_restarts=0, result_ok=True)
        owner.agent("hrtest-codex").pop("codex_launch_generation", None)
        owner.mark_agent("hrtest-codex")
        owner.save()
        self.assertTrue(owner.claim_codex_update_restart("hrtest-codex"))
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual((a.get("codex_launch_generation", 0), a["update_restarts"]), (1, 1))
        self.assertEqual(a["state"], "starting")
        self.assertFalse(a["prompted"])
        self.assertFalse(a["result_ok"])
        self.assertFalse(owner.claim_codex_update_restart("hrtest-codex"))
        self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex")["codex_launch_generation"], 1)

    def test_same_generation_update_save_keeps_a_spent_prompt_retry(self):
        seed = RunStatus.create(self.run_dir)
        seed.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_update_pending=False)
        prompt, waiting = RunStatus.load(self.run_dir), RunStatus.load(self.run_dir)
        prompt.agent("hrtest-codex")["retries"] = 1
        prompt.mark_agent("hrtest-codex")
        prompt.save()
        waiting.agent("hrtest-codex")["codex_update_pending"] = True
        waiting.mark_agent("hrtest-codex")
        waiting.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual((a["codex_launch_generation"], a["retries"], a["collect_retries"]), (0, 1, 0))
        self.assertTrue(a["codex_update_pending"])
        self.assertEqual(waiting.agent("hrtest-codex")["retries"], 1)

    def test_same_generation_update_save_keeps_a_spent_collect_retry(self):
        seed = RunStatus.create(self.run_dir)
        seed.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_update_pending=False)
        collecting, waiting = RunStatus.load(self.run_dir), RunStatus.load(self.run_dir)
        collecting.agent("hrtest-codex")["collect_retries"] = 1
        collecting.mark_agent("hrtest-codex")
        collecting.save()
        waiting.agent("hrtest-codex")["codex_update_pending"] = True
        waiting.mark_agent("hrtest-codex")
        waiting.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual((a["codex_launch_generation"], a["retries"], a["collect_retries"]), (0, 0, 1))
        self.assertTrue(a["codex_update_pending"])
        self.assertEqual(waiting.agent("hrtest-codex")["collect_retries"], 1)

    def test_same_generation_saves_keep_both_owned_prompt_retry_increments(self):
        seed = RunStatus.create(self.run_dir)
        seed.add_agent("hrtest-codex", role="reviewer", kind="codex")
        first, second = RunStatus.load(self.run_dir), RunStatus.load(self.run_dir)
        first.agent("hrtest-codex")["retries"] += 1
        first.mark_agent("hrtest-codex")
        first.save()
        second.agent("hrtest-codex")["retries"] += 1
        second.mark_agent("hrtest-codex")
        second.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual((a["codex_launch_generation"], a["retries"]), (0, 2))
        second.mark_agent("hrtest-codex")
        second.save()
        self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex")["retries"], 2)

    def test_a_stale_generation_save_preserves_the_new_lifecycle_and_owned_retry_bookkeeping(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                owner = RunStatus.create(self.run_dir)
                owner.add_agent("hrtest-codex", role="reviewer", kind="codex", update_restarts=0,
                                codex_launch_generation=0, codex_startup_pending=True,
                                codex_startup_closed=False, codex_update_seen=True, codex_update_pending=True,
                                retries=1, collect_retries=0)
                stale = RunStatus.load(self.run_dir)
                self.assertTrue(owner.claim_codex_update_restart("hrtest-codex"))
                expected = {
                    "state": "working", "state_since": "2026-10-03T20:00:00+00:00",
                    "codex_launch_generation": 1, "update_restarts": 1,
                    "codex_startup_pending": False, "codex_startup_closed": True,
                    "codex_update_seen": False, "codex_update_pending": False,
                    "prompted": True, "result_ok": True, "result_file": "new-review.md",
                    "reason": "new generation diagnostic", "last_screen": "new session screen",
                    "screen_hash": "new generation hash",
                    "codex_pane_baseline": "new generation pane baseline\n",
                    "codex_pane_baseline_generation": 1,
                }
                owner.agent("hrtest-codex").update(expected, retries=2, collect_retries=1)
                owner.mark_agent("hrtest-codex")
                owner.save()
                old = stale.agent("hrtest-codex")
                old.update(state="gone", state_since="2026-10-03T19:00:00+00:00",
                           codex_startup_pending=True, codex_startup_closed=False,
                           codex_update_seen=True, codex_update_pending=True,
                           prompted=False, result_ok=False, result_file="old-review.md",
                           reason="old agent exited", last_screen="old shell screen", screen_hash="old hash",
                           codex_pane_baseline="old pane baseline\n", codex_pane_baseline_generation=0)
                if owned:
                    old.update(retries=3, collect_retries=1)
                stale.mark_agent("hrtest-codex")
                stale.save()
                a = RunStatus.load(self.run_dir).agent("hrtest-codex")
                self.assertEqual({key: a[key] for key in expected}, expected)
                self.assertEqual((a["retries"], a["collect_retries"]), (4 if owned else 2, 1))
                self.assertEqual(stale.agent("hrtest-codex"), a)

    def test_a_stale_generation_cannot_remove_a_restarted_agent(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_launch_generation=0)
        stale = RunStatus.load(self.run_dir)
        self.assertTrue(owner.claim_codex_update_restart("hrtest-codex"))
        owner.set_agent_state("hrtest-codex", "working")
        stale.remove_agent("hrtest-codex")
        stale.save()
        disk = RunStatus.load(self.run_dir)
        self.assertIn("hrtest-codex", disk.data["agents"])
        self.assertEqual(disk.agent("hrtest-codex")["state"], "working")
        self.assertEqual(disk.agent("hrtest-codex")["codex_launch_generation"], 1)

    def test_same_generation_saves_keep_the_earliest_valid_update_deadline(self):
        for replacement in ("omit", None, 1_800_001_200.0, float("nan"), float("inf"), "later", True):
            with self.subTest(replacement=replacement):
                owner = RunStatus.create(self.run_dir)
                owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_startup_pending=True,
                                codex_update_pending=True)
                stale = RunStatus.load(self.run_dir)
                owner.agent("hrtest-codex")["codex_update_deadline"] = 1_800_000_600.0
                owner.mark_agent("hrtest-codex")
                owner.save()
                if replacement != "omit":
                    stale.agent("hrtest-codex")["codex_update_deadline"] = replacement
                stale.mark_agent("hrtest-codex")
                stale.save()
                self.assertEqual(stale.agent("hrtest-codex")["codex_update_deadline"], 1_800_000_600.0)
                stale.agent("hrtest-codex")["codex_update_deadline"] = 1_800_000_500.0
                stale.mark_agent("hrtest-codex")
                stale.save()
                self.assertEqual(RunStatus.load(self.run_dir).agent("hrtest-codex")["codex_update_deadline"], 1_800_000_500.0)

    def test_startup_closure_clears_the_deadline_and_stale_saves_keep_it_closed(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_startup_pending=True,
                        codex_update_pending=True, codex_update_deadline=1_800_000_600.0)
        stale = RunStatus.load(self.run_dir)
        owner.agent("hrtest-codex")["codex_startup_closed"] = True
        owner.mark_agent("hrtest-codex")
        owner.save()
        self.assertIsNone(owner.agent("hrtest-codex").get("codex_update_deadline"))
        stale.agent("hrtest-codex")["codex_update_deadline"] = 1_800_001_200.0
        stale.mark_agent("hrtest-codex")
        stale.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertTrue(a["codex_startup_closed"])
        self.assertFalse(a["codex_update_pending"])
        self.assertIsNone(a.get("codex_update_deadline"))

    def test_restart_resets_the_timer_and_an_old_generation_cannot_replace_the_new_timer(self):
        owner = RunStatus.create(self.run_dir)
        owner.add_agent("hrtest-codex", role="reviewer", kind="codex", codex_startup_pending=True,
                        codex_update_pending=True, codex_update_deadline=1_800_000_600.0)
        stale = RunStatus.load(self.run_dir)
        self.assertTrue(owner.claim_codex_update_restart("hrtest-codex"))
        self.assertIsNone(owner.agent("hrtest-codex").get("codex_update_deadline"))
        owner.agent("hrtest-codex").update(codex_update_pending=True, codex_update_deadline=1_800_001_000.0)
        owner.mark_agent("hrtest-codex")
        owner.save()
        stale.agent("hrtest-codex")["codex_update_deadline"] = None
        stale.mark_agent("hrtest-codex")
        stale.save()
        a = RunStatus.load(self.run_dir).agent("hrtest-codex")
        self.assertEqual(a["codex_launch_generation"], 1)
        self.assertEqual(a["codex_update_deadline"], 1_800_001_000.0)

    def test_now_iso_format(self):
        self.assertRegex(now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


if __name__ == "__main__":
    unittest.main()
