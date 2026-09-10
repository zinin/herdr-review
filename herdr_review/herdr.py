"""Client for the herdr CLI (herdr 0.9.0)."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable

LOG_CLIP = 4096


@dataclass
class HerdrResult:
    ok: bool
    returncode: int
    result: dict | None = None
    error_code: str | None = None
    message: str | None = None
    text: str = ""


class Herdr:
    def __init__(self, binary: str | None = None, log: Callable[[str], None] | None = None, mask_values: Iterable[str] = ()):
        self.binary = binary or os.environ.get("HERDR_BIN") or "herdr"
        self.log = log or (lambda line: None)
        self.mask_values = [v for v in mask_values if v]

    # ----- low level
    def mask(self, s: str) -> str:
        for v in self.mask_values:
            s = s.replace(v, "***")
        return s

    @staticmethod
    def _parse_json(s: str) -> dict | None:
        s = s.strip()
        if not s.startswith("{"):
            return None
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def run(self, *args: str, text_output: bool = False, timeout: float | None = None) -> HerdrResult:
        argv = [self.binary, *args]
        self.log("herdr " + " ".join(self.mask(a) for a in args))
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError:
            self.log("  herdr binary not found")
            return HerdrResult(False, 127, error_code="herdr_not_found", message=f"{self.binary} not found in PATH")
        except subprocess.TimeoutExpired:
            self.log("  herdr call timed out")
            return HerdrResult(False, 124, error_code="timeout", message="herdr call timed out")
        out, err = p.stdout or "", p.stderr or ""
        self.log(f"  rc={p.returncode} out={self.mask(out)[:LOG_CLIP]!r} err={self.mask(err)[:LOG_CLIP]!r}")
        if text_output and p.returncode == 0:
            return HerdrResult(True, 0, text=out)
        payload = self._parse_json(out) or self._parse_json(err)
        if payload is not None and "error" in payload:
            e = payload["error"] if isinstance(payload["error"], dict) else {}
            return HerdrResult(False, p.returncode, error_code=str(e.get("code", "error")), message=str(e.get("message", "")), text=out or err)
        if p.returncode == 0:
            result = payload.get("result") if payload is not None else None
            return HerdrResult(True, 0, result=result if isinstance(result, dict) else None, text=out)
        return HerdrResult(False, p.returncode, error_code="herdr_failed", message=(err or out).strip()[:500], text=out)

    # ----- typed helpers
    def status_ok(self) -> bool:
        r = self.run("status", "server", text_output=True, timeout=30)
        return r.ok and "status: running" in r.text

    @staticmethod
    def _env_args(env: dict[str, str]) -> list[str]:
        out: list[str] = []
        for k, v in env.items():
            out += ["--env", f"{k}={v}"]
        return out

    def tab_create(self, workspace: str, cwd: str, label: str, env: dict[str, str], focus: bool = False) -> HerdrResult:
        args = ["tab", "create", "--workspace", workspace, "--cwd", str(cwd), "--label", label, *self._env_args(env), "--focus" if focus else "--no-focus"]
        return self.run(*args, timeout=60)

    def pane_split(self, pane: str, direction: str, ratio: float, cwd: str, env: dict[str, str]) -> HerdrResult:
        args = ["pane", "split", "--pane", pane, "--direction", direction, "--ratio", f"{ratio:.3g}", "--cwd", str(cwd), *self._env_args(env), "--no-focus"]
        return self.run(*args, timeout=60)

    def agent_start(self, name: str, kind: str, pane: str, args: list[str], timeout_ms: int = 300000) -> HerdrResult:
        argv = ["agent", "start", name, "--kind", kind, "--pane", pane, "--timeout", str(timeout_ms)]
        if args:
            argv += ["--", *args]
        return self.run(*argv, timeout=timeout_ms / 1000 + 30)

    def agent_prompt(self, name: str, text: str, until: str | None = None, timeout_ms: int | None = None) -> HerdrResult:
        argv = ["agent", "prompt", name, text, "--wait"]
        if until:
            argv += ["--until", until]
        if timeout_ms:
            argv += ["--timeout", str(timeout_ms)]
        return self.run(*argv, timeout=(timeout_ms / 1000 + 30) if timeout_ms else None)

    def agent_get(self, name: str) -> HerdrResult:
        return self.run("agent", "get", name, timeout=30)

    def agent_wait(self, name: str, until: str | None = None, timeout_ms: int | None = None) -> HerdrResult:
        argv = ["agent", "wait", name]
        if until:
            argv += ["--until", until]
        if timeout_ms:
            argv += ["--timeout", str(timeout_ms)]
        return self.run(*argv, timeout=(timeout_ms / 1000 + 30) if timeout_ms else None)

    def agent_read(self, name: str, source: str = "visible", lines: int = 60) -> str | None:
        r = self.run("agent", "read", name, "--source", source, "--lines", str(lines), text_output=True, timeout=30)
        return r.text if r.ok else None

    def pane_read(self, pane: str, source: str = "recent-unwrapped", lines: int = 40) -> str | None:
        r = self.run("pane", "read", pane, "--source", source, "--lines", str(lines), text_output=True, timeout=30)
        return r.text if r.ok else None

    def agent_send_keys(self, name: str, *keys: str) -> HerdrResult:
        return self.run("agent", "send-keys", name, *keys, timeout=30)

    def tab_rename(self, tab: str, label: str) -> HerdrResult:
        return self.run("tab", "rename", tab, label, timeout=30)

    def pane_rename(self, pane: str, label: str) -> HerdrResult:
        return self.run("pane", "rename", pane, label, timeout=30)

    def tab_close(self, tab: str) -> HerdrResult:
        return self.run("tab", "close", tab, timeout=30)

    def pane_close(self, pane: str) -> HerdrResult:
        return self.run("pane", "close", pane, timeout=30)

    def tab_focus(self, tab: str) -> HerdrResult:
        return self.run("tab", "focus", tab, timeout=30)

    def notification_show(self, title: str, body: str | None = None, sound: str | None = None) -> HerdrResult:
        argv = ["notification", "show", title]
        if body:
            argv += ["--body", body]
        if sound:
            argv += ["--sound", sound]
        return self.run(*argv, timeout=30)
