import json
import tempfile
import unittest
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

    def test_now_iso_format(self):
        self.assertRegex(now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


if __name__ == "__main__":
    unittest.main()
