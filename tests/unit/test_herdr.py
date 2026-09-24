import json
import subprocess
import unittest
from unittest import mock

from herdr_review.herdr import Herdr, HerdrResult


def completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


class HerdrClientTest(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.client = Herdr(binary="herdr-fake", log=self.lines.append, mask_values=["s3cret"])

    def test_success_json_on_stdout(self):
        payload = {"id": "cli:tab:create", "result": {"type": "tab_created", "tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p5"}}}
        with mock.patch("subprocess.run", return_value=completed(0, json.dumps(payload))) as run:
            r = self.client.tab_create("w1", "/repo", "rv-hr1: orch", {"HERDR_REVIEW_RUN": "/run", "TOKEN": "s3cret"})
        self.assertTrue(r.ok)
        self.assertEqual(r.result["tab"]["tab_id"], "w1:t2")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["herdr-fake", "tab", "create"])
        self.assertIn("--no-focus", argv)
        self.assertIn("--env", argv)
        self.assertIn("TOKEN=s3cret", argv)
        self.assertTrue(any("herdr tab create" in l and "TOKEN=***" in l and "s3cret" not in l for l in self.lines))

    def test_error_json_on_stderr(self):
        err = json.dumps({"error": {"code": "agent_not_found", "message": "agent target x not found"}, "id": "cli:agent:get"})
        with mock.patch("subprocess.run", return_value=completed(1, "", err)):
            r = self.client.agent_get("x")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "agent_not_found")
        self.assertIn("not found", r.message)

    def test_error_json_on_stdout_is_also_understood(self):
        err = json.dumps({"error": {"code": "agent_not_ready", "message": "blocked during startup"}})
        with mock.patch("subprocess.run", return_value=completed(1, err, "")):
            r = self.client.agent_start("hr1-codex", "codex", "w1:p3", ["-m", "gpt-5.5"])
        self.assertEqual(r.error_code, "agent_not_ready")

    def test_agent_start_passes_args_after_double_dash(self):
        with mock.patch("subprocess.run", return_value=completed(0, json.dumps({"result": {"type": "agent_started"}}))) as run:
            self.client.agent_start("hr1-codex", "codex", "w1:p3", ["-m", "gpt-5.5"], timeout_ms=1000)
            argv = run.call_args.args[0]
        self.assertEqual(argv, ["herdr-fake", "agent", "start", "hr1-codex", "--kind", "codex", "--pane", "w1:p3", "--timeout", "1000", "--", "-m", "gpt-5.5"])
        with mock.patch("subprocess.run", return_value=completed(0, json.dumps({"result": {}}))) as run:
            self.client.agent_start("hr1-x", "claude", "w1:p3", [])
            self.assertNotIn("--", run.call_args.args[0])

    def test_agent_prompt_flags(self):
        with mock.patch("subprocess.run", return_value=completed(0, json.dumps({"result": {"agent": {"agent_status": "working"}}}))) as run:
            r = self.client.agent_prompt("hr1-codex", "Read /x and follow it exactly.", until="working", timeout_ms=30000)
            argv = run.call_args.args[0]
        self.assertTrue(r.ok)
        self.assertEqual(argv, ["herdr-fake", "agent", "prompt", "hr1-codex", "Read /x and follow it exactly.", "--wait", "--until", "working", "--timeout", "30000"])
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 30)

    def test_text_output_commands(self):
        with mock.patch("subprocess.run", return_value=completed(0, "line1\nline2\n")):
            self.assertEqual(self.client.agent_read("hr1-codex", "visible", 10), "line1\nline2\n")
            self.assertEqual(self.client.pane_read("w1:p3"), "line1\nline2\n")
        err = json.dumps({"error": {"code": "agent_not_found", "message": "gone"}})
        with mock.patch("subprocess.run", return_value=completed(1, "", err)):
            self.assertIsNone(self.client.agent_read("hr1-codex"))

    def test_tab_get_parses_the_tab_and_the_missing_tab(self):
        found = '{"id":"cli:tab:get","result":{"tab":{"agent_status":"working","focused":true,"label":"1","number":4,"pane_count":1,"tab_id":"w1:t4","workspace_id":"w1"},"type":"tab_info"}}'
        with mock.patch("subprocess.run", return_value=completed(0, found)) as run:
            r = self.client.tab_get("w1:t4")
        self.assertEqual(run.call_args.args[0], ["herdr-fake", "tab", "get", "w1:t4"])
        self.assertTrue(r.ok)
        self.assertEqual((r.result["tab"]["tab_id"], r.result["tab"]["label"]), ("w1:t4", "1"))
        missing = '{"error":{"code":"tab_not_found","message":"tab w9:t99 not found"},"id":"cli:tab:get"}'
        with mock.patch("subprocess.run", return_value=completed(1, "", missing)):
            r = self.client.tab_get("w9:t99")
        self.assertEqual((r.ok, r.error_code), (False, "tab_not_found"))

    def test_status_ok_reads_running_line(self):
        with mock.patch("subprocess.run", return_value=completed(0, "status: running\nversion: 0.9.0\n")):
            self.assertTrue(self.client.status_ok())
        with mock.patch("subprocess.run", return_value=completed(1, "", "no server")):
            self.assertFalse(self.client.status_ok())

    def test_missing_binary_and_timeout(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            r = self.client.run("status")
        self.assertEqual(r.error_code, "herdr_not_found")
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="herdr", timeout=1)):
            r = self.client.run("agent", "wait", "x")
        self.assertEqual(r.error_code, "timeout")

    def test_run_decodes_herdr_output_with_replace(self):
        with mock.patch("subprocess.run", return_value=completed(0, json.dumps({"result": {}}))) as run:
            self.client.run("status", "server")
        self.assertEqual(run.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(run.call_args.kwargs.get("errors"), "replace")

    def test_env_binary_override(self):
        with mock.patch.dict("os.environ", {"HERDR_BIN": "/opt/fake/herdr"}):
            self.assertEqual(Herdr().binary, "/opt/fake/herdr")

    def test_notification_and_misc_helpers(self):
        ok = json.dumps({"result": {"type": "ok"}})
        with mock.patch("subprocess.run", return_value=completed(0, ok)) as run:
            self.client.notification_show("t", body="b", sound="request")
            self.assertEqual(run.call_args.args[0], ["herdr-fake", "notification", "show", "t", "--body", "b", "--sound", "request"])
            self.client.tab_rename("w1:t2", "rv-hr1: codex ✓")
            self.assertEqual(run.call_args.args[0], ["herdr-fake", "tab", "rename", "w1:t2", "rv-hr1: codex ✓"])
            self.client.pane_split("w1:p1", "right", 0.35, "/repo", {"A": "1"})
            self.assertEqual(run.call_args.args[0], ["herdr-fake", "pane", "split", "--pane", "w1:p1", "--direction", "right", "--ratio", "0.35", "--cwd", "/repo", "--env", "A=1", "--no-focus"])
            self.client.agent_send_keys("hr1-codex", "down", "enter")
            self.assertEqual(run.call_args.args[0], ["herdr-fake", "agent", "send-keys", "hr1-codex", "down", "enter"])
            self.client.tab_close("w1:t2")
            self.client.pane_close("w1:p9")
            self.client.tab_focus("w1:t2")
            self.assertEqual(run.call_args.args[0], ["herdr-fake", "tab", "focus", "w1:t2"])
            self.client.agent_wait("hr1-codex", until="idle", timeout_ms=30000)
            self.assertEqual(run.call_args.args[0], ["herdr-fake", "agent", "wait", "hr1-codex", "--until", "idle", "--timeout", "30000"])


if __name__ == "__main__":
    unittest.main()
