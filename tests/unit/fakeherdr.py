"""In-memory stand-in for herdr_review.herdr.Herdr."""
from __future__ import annotations

from herdr_review.herdr import HerdrResult

OK = HerdrResult(True, 0, result={"type": "ok"})


class FakeHerdr:
    def __init__(self):
        self.calls: list[tuple] = []
        self.log = lambda line: None
        self.mask_values: list[str] = []
        self.tab_counter = 1   # w1:t1 / w1:p1 are the caller's own tab and pane
        self.pane_counter = 1
        self.agent_status: dict[str, list[str]] = {}
        self.start_errors: dict[str, tuple[str, str]] = {}
        self.prompt_errors: dict[str, tuple[str, str]] = {}
        self.wait_ok = True
        self.close_errors: dict[str, tuple[str, str]] = {}
        self.keep_screen_on_wait = False
        self.screens: dict[str, str] = {}
        self.pane_screens: dict[str, str] = {}
        self.server_running = True

    def mask(self, s: str) -> str:
        for v in self.mask_values:
            s = s.replace(v, "***")
        return s

    def status_ok(self) -> bool:
        self.calls.append(("status",))
        return self.server_running

    def tab_create(self, workspace, cwd, label, env, focus=False):
        self.tab_counter += 1
        self.pane_counter += 1
        self.calls.append(("tab_create", workspace, str(cwd), label, dict(env), focus))
        return HerdrResult(True, 0, result={"type": "tab_created", "tab": {"tab_id": f"w1:t{self.tab_counter}", "label": label}, "root_pane": {"pane_id": f"w1:p{self.pane_counter}"}})

    def pane_split(self, pane, direction, ratio, cwd, env):
        self.pane_counter += 1
        self.calls.append(("pane_split", pane, direction, round(ratio, 3), str(cwd), dict(env)))
        return HerdrResult(True, 0, result={"type": "pane_info", "pane": {"pane_id": f"w1:p{self.pane_counter}"}})

    def agent_start(self, name, kind, pane, args, timeout_ms=300000):
        self.calls.append(("agent_start", name, kind, pane, list(args)))
        if name in self.start_errors:
            code, msg = self.start_errors[name]
            self.agent_status.setdefault(name, ["blocked"])
            return HerdrResult(False, 1, error_code=code, message=msg)
        self.agent_status.setdefault(name, ["idle"])
        return HerdrResult(True, 0, result={"type": "agent_started", "agent": {"name": name, "agent_status": "idle", "pane_id": pane}})

    def agent_prompt(self, name, text, until=None, timeout_ms=None):
        self.calls.append(("agent_prompt", name, text, until, timeout_ms))
        if name in self.prompt_errors:
            code, msg = self.prompt_errors.pop(name)
            return HerdrResult(False, 1, error_code=code, message=msg)
        return HerdrResult(True, 0, result={"type": "agent_prompted", "agent": {"name": name, "agent_status": until or "done"}})

    def agent_wait(self, name, until=None, timeout_ms=None):
        self.calls.append(("agent_wait", name, until, timeout_ms))
        if not self.wait_ok:
            return HerdrResult(False, 1, error_code="timeout", message="wait timed out")
        if not self.keep_screen_on_wait:
            self.screens[name] = "idle\n"
        return HerdrResult(True, 0, result={"type": "agent_info", "agent": {"name": name, "agent_status": until or "idle"}})

    def agent_get(self, name):
        self.calls.append(("agent_get", name))
        seq = self.agent_status.get(name)
        if not seq:
            return HerdrResult(False, 1, error_code="agent_not_found", message=f"agent target {name} not found")
        status = seq.pop(0) if len(seq) > 1 else seq[0]
        if status == "gone":
            self.agent_status[name] = ["gone"]
            return HerdrResult(False, 1, error_code="agent_not_found", message=f"agent target {name} not found")
        return HerdrResult(True, 0, result={"type": "agent_info", "agent": {"name": name, "agent_status": status, "launch_pending": None}})

    def agent_read(self, name, source="visible", lines=60):
        self.calls.append(("agent_read", name, source, lines))
        return self.screens.get(name, f"screen of {name}\n")

    def pane_read(self, pane, source="recent-unwrapped", lines=40):
        self.calls.append(("pane_read", pane, source, lines))
        return self.pane_screens.get(pane, "user@host:~$ \n")

    def agent_send_keys(self, name, *keys):
        self.calls.append(("agent_send_keys", name, tuple(keys)))
        return OK

    def tab_rename(self, tab, label):
        self.calls.append(("tab_rename", tab, label))
        return OK

    def pane_rename(self, pane, label):
        self.calls.append(("pane_rename", pane, label))
        return OK

    def tab_close(self, tab):
        self.calls.append(("tab_close", tab))
        if tab in self.close_errors:
            code, msg = self.close_errors[tab]
            return HerdrResult(False, 1, error_code=code, message=msg)
        return OK

    def pane_close(self, pane):
        self.calls.append(("pane_close", pane))
        if pane in self.close_errors:
            code, msg = self.close_errors[pane]
            return HerdrResult(False, 1, error_code=code, message=msg)
        return OK

    def tab_focus(self, tab):
        self.calls.append(("tab_focus", tab))
        return OK

    def notification_show(self, title, body=None, sound=None):
        self.calls.append(("notification_show", title, body, sound))
        return HerdrResult(True, 0, result={"type": "notification_show", "shown": True, "reason": "shown"})

    # test helpers
    def calls_named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]
