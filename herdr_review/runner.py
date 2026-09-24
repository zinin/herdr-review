"""The `run …` subcommands the orchestrator agent calls."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Callable, Mapping

from . import PROMPTS_DIR, gitutil
from .config import ConfigError, is_secretish, load_config
from .dialogs import SCREEN_LINES, mcp_refusal, resolve_startup_dialog
from .herdr import Herdr, HerdrResult
from .layout import fixer_split, plan_grid
from .render import render_file
from .status import PHASES, RunStatus, StatusError, now_iso

REQUIRED_HEADINGS = ("### Critical Issues", "### Important Issues", "### Minor Issues", "### Assessment")
HEADING_LINE = re.compile(r"^\s*#{1,6}\s")
BRACKETED = re.compile(r"\[[^\]]+\]")
STATE_CLASS = {
    "starting": "working",
    "prompt_stalled": "stalled",
    "working": "working",
    "idle": "settled",
    "done": "settled",
    "blocked": "blocked",
    "blocked-start": "blocked",
    "unknown": "unknown",
    "gone": "gone",
    "failed": "failed",
    "collected": "collected",
}
TERMINAL_STATES = {"failed", "gone", "collected"}
# `prompt` refuses these: the state holds a result that re-prompting would destroy.
# The other terminal states (`failed`, `gone`) are a manual rescue path and stay allowed.
PROMPT_REFUSED_STATES = {"collected"}
PENDING_CLASSES = ("working", "stalled", "blocked", "unknown")
LABEL_SUFFIX = {
    "starting": " ⏳", "working": " ⏳", "prompt_stalled": " ⏳",
    "collected": " ✓", "failed": " ✗", "gone": " ✗",
    "blocked": " ❓", "blocked-start": " ❓",
}
FIXER_DONE_STATES = {"idle", "done"}
PROMPT_TIMEOUT_MS = 30000
LAST_SCREEN_LINES = 40
LIVE_STATUSES = ("idle", "working", "blocked", "done", "unknown")
# Codes the herdr client raises when it could not reach herdr at all. They say nothing about
# the agent, so they must never take one out of the run.
HERDR_ERROR_CODES = ("herdr_not_found", "herdr_failed", "timeout")
OBSERVE_ATTEMPTS = 3
# close --force reads status.json again after every round of closes, for the tabs a live orchestrator opened
# meanwhile; past this many rounds, a tab that still appears is left open for another close --force.
CLOSE_ROUNDS = 3
# drift_status when the tree changed but no `git status --short` line is new or gone since launch.
DRIFT_NOTHING_NEW_OR_GONE = (
    "no line of `git status --short` is new or gone since launch: the content of a file that was already uncommitted"
    " at launch changed — an edit or a revert, by an agent or by the user — or a commit landed"
)
# How drift_status marks a line of the launch that no longer shows: uncommitted work gone from the tree.
DRIFT_GONE = "gone since launch: "


class RunnerError(Exception):
    """The runner itself cannot work — exit 1 for the orchestrator."""


def retry_text(path: str, why: str, prompt: str | None = None) -> str:
    hint = f" Read {prompt} and follow it exactly." if prompt else ""
    return f"You have not written a valid review to {path} ({why}).{hint} Write your complete review there now, in the required format, and reply DONE."


def not_found(r: HerdrResult) -> bool:
    """herdr answered that the tab, pane or agent does not exist; `herdr_not_found` is the binary missing."""
    return bool(r.error_code) and r.error_code.endswith("not_found") and r.error_code not in HERDR_ERROR_CODES


def response_id(result: object, *keys: str) -> str | None:
    """Dig an identifier out of a herdr response that failed to parse. None when it is not there."""
    for key in keys:
        if not isinstance(result, dict):
            return None
        result = result.get(key)
    return result if isinstance(result, str) and result else None


def template_filler_lines() -> set[str]:
    """The reviewer template's own bracketed lines, so a verbatim copy of it is not a review.

    Empty when the template cannot be read: a missing template must never block a valid review.
    """
    try:
        text = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if BRACKETED.search(line)}


def section_body(text: str, heading: str) -> list[str]:
    """The lines under <heading>, up to the next heading of any level."""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if HEADING_LINE.match(line):
            if inside:
                break
            inside = line.strip().startswith(heading)
            continue
        if inside:
            body.append(line)
    return body


def same_commits(passed: list[str], computed: list[str]) -> bool:
    """Compare two hash lists that may be abbreviated to different lengths."""
    width = min((len(h) for h in [*passed, *computed]), default=0)
    if not width:
        return not passed and not computed
    return {h[:width] for h in passed} == {h[:width] for h in computed}


def check_review_file(path: Path) -> tuple[bool, str]:
    path = Path(path)
    if not path.exists():
        return False, "file missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return False, "file empty"
    missing = [h for h in REQUIRED_HEADINGS if not re.search(r"^\s*" + re.escape(h), text, re.MULTILINE)]
    if missing:
        return False, "missing sections: " + ", ".join(missing)
    filler = template_filler_lines()
    if not any(line.strip() and line.strip() not in filler for line in section_body(text, "### Assessment")):
        return False, "### Assessment has no content"
    return True, ""


class Runner:
    def __init__(self, run_dir: Path, herdr: Herdr | None = None, poll_sec: float = 5.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.run_dir = Path(run_dir)
        try:
            self.status = RunStatus.load(self.run_dir)
            self.run = self.status.run_json()
        except StatusError as e:
            raise RunnerError(str(e)) from e
        self.log_path = self.run_dir / "runner.log"
        self._config = None
        self._config_error: str | None = None
        self.herdr = herdr if herdr is not None else Herdr()
        self.herdr.log = self.log
        self.herdr.mask_values = self._secret_values()
        self.poll_sec = poll_sec
        self.clock = clock
        self.sleep = sleep

    # ----- infrastructure
    def log(self, line: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {line}\n")

    def _reload(self) -> None:
        """Read status.json again: another process of the run may have written it since."""
        try:
            self.status = RunStatus.load(self.run_dir)
        except StatusError as e:
            raise RunnerError(str(e)) from e

    def _load_config(self):
        if self._config is None and self._config_error is None:
            try:
                self._config = load_config()
            except ConfigError as e:
                self._config_error = str(e)
                self.log(f"config unavailable, profile env not applied: {e}")
        return self._config

    def _spec_env_keys(self, profile: str) -> list[str]:
        for spec in self.run.get("reviewers") or []:
            if spec.get("profile") == profile:
                return list(spec.get("env_keys") or [])
        for role in ("orchestrator", "fixer"):
            spec = self.run.get(role) or {}
            if spec.get("profile") == profile:
                return list(spec.get("env_keys") or [])
        return []

    def _profile_env(self, profile: str) -> dict[str, str]:
        cfg = self._load_config()
        if cfg is not None and profile in cfg.profiles:
            return dict(cfg.profiles[profile].env)
        if self._spec_env_keys(profile):
            raise RunnerError(
                f"config unavailable, cannot apply env for profile '{profile}': {self._config_error}"
            )
        return {}

    def _secret_values(self) -> list[str]:
        cfg = self._load_config()
        if cfg is None:
            return []
        return [v for p in cfg.profiles.values() for k, v in p.env.items() if is_secretish(k, v)]

    @property
    def run_id(self) -> str:
        return self.run["run_id"]

    @property
    def repo(self) -> str:
        return self.run["repo"]

    @property
    def layout(self) -> str:
        return self.run.get("layout", "tabs")

    def _base_env(self) -> dict[str, str]:
        return {"HERDR_REVIEW_RUN": str(self.run_dir)}

    def _agent(self, name: str) -> dict:
        try:
            return self.status.agent(name)
        except StatusError as e:
            raise RunnerError(str(e)) from e

    def _summary(self, name: str) -> dict:
        a = self.status.agent(name)
        return {"state": a["state"], "tab": a.get("tab"), "pane": a.get("pane"), "reason": a.get("reason")}

    def _prompt_file(self, file: str | Path) -> Path:
        p = Path(file).expanduser()
        if not p.is_absolute():
            p = Path(self.repo) / p
        p = p.resolve()
        if not p.is_file():
            raise RunnerError(f"prompt file not found: {p}")
        return p

    def _terminal_text(self, file: Path) -> str:
        return render_file(PROMPTS_DIR / "terminal.md", {"FILE": str(file)}).strip()

    # ----- labels
    def _label(self, name: str, state: str) -> str:
        a = self.status.agent(name)
        fixer = a.get("role") == "fixer"
        who = "fixer" if fixer else a["profile"]
        suffix = LABEL_SUFFIX.get(state, "")
        # A reviewer in idle/done is waiting for `collect` to judge it, so it stays unmarked.
        # The fixer has no such step: once it has been given a task, idle/done means that task
        # is finished. Before its first prompt it is idle too, and that is not a finished batch.
        if not suffix and fixer and state in FIXER_DONE_STATES and a.get("prompted"):
            suffix = " ✓"
        return f"rv-{self.run_id}: {who}" + suffix

    def _relabel(self, name: str, state: str) -> None:
        a = self.status.agent(name)
        label = self._label(name, state)
        if self.layout == "tabs" and a.get("tab"):
            self.herdr.tab_rename(a["tab"], label)
        elif a.get("pane"):
            self.herdr.pane_rename(a["pane"], label)

    def _relabel_orch(self, suffix: str) -> None:
        o = self.status.data.get("orchestrator") or {}
        if o.get("tab"):
            self.herdr.tab_rename(o["tab"], f"rv-{self.run_id}: orch{suffix}")

    def _set_state(self, name: str, state: str, reason: str | None = None, last_screen: str | None = None) -> None:
        self.status.set_agent_state(name, state, reason=reason, last_screen=last_screen)
        self._relabel(name, state)

    def _set_reason(self, name: str, reason: str) -> None:
        """Record why herdr could not be asked, without touching the agent's own state."""
        self.status.agent(name)["reason"] = reason
        self.status.mark_agent(name)
        self.status.save()

    def _pane_screen(self, pane: str) -> str | None:
        """A pane dump for status.json — masked, because it can hold an echoed secret."""
        text = self.herdr.pane_read(pane, lines=LAST_SCREEN_LINES)
        return self.herdr.mask(text) if text is not None else None

    def _last_screen(self, name: str) -> str | None:
        a = self.status.agent(name)
        return self._pane_screen(a["pane"]) if a.get("pane") else None

    # ----- placement
    def _orch_pane(self) -> tuple[str | None, str]:
        o = self.status.data.get("orchestrator") or {}
        if not o.get("pane"):
            raise RunnerError("orchestrator pane is unknown in status.json")
        return o.get("tab"), o["pane"]

    def _place_tabs(self, specs: list[dict], label_of: Callable[[dict], str]) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        created: list[tuple[str, str, str]] = []
        try:
            for s in specs:
                env = {**self._base_env(), **self._profile_env(s["profile"])}
                r = self.herdr.tab_create(self.run["workspace_id"], self.repo, f"rv-{self.run_id}: {label_of(s)}", env)
                if not r.ok or not r.result:
                    raise RunnerError(f"herdr tab create failed: {r.error_code}: {r.message}")
                try:
                    tab, pane = r.result["tab"]["tab_id"], r.result["root_pane"]["pane_id"]
                except (KeyError, TypeError) as e:
                    raise RunnerError(f"unexpected `tab create` response: {(r.text or '')[:300]}") from e
                out[s["name"]] = (tab, pane)
                self.status.add_agent(
                    s["name"], role="reviewer", profile=s["profile"], kind=s["kind"], state="starting",
                    tab=tab, pane=pane, result_file=str(self.run_dir / "reviews" / f"{s['profile']}.md"),
                )
                self._relabel(s["name"], "starting")
                created.append((s["name"], tab, pane))
        except RunnerError:
            self._rollback_reviewers(created)
            raise
        return out

    def _place_grid(self, specs: list[dict]) -> dict[str, tuple[str | None, str]]:
        tab, orch_pane = self._orch_pane()
        steps, assign = plan_grid(len(specs))
        ids = {"orch": orch_pane}
        owner = {sym: specs[i] for i, sym in enumerate(assign)}
        created_panes: list[str] = []
        try:
            for step in steps:
                spec = owner.get(step.result)
                env = {**self._base_env(), **(self._profile_env(spec["profile"]) if spec else {})}
                r = self.herdr.pane_split(ids[step.target], step.direction, step.ratio, self.repo, env)
                if not r.ok or not r.result:
                    raise RunnerError(f"herdr pane split failed: {r.error_code}: {r.message}")
                try:
                    pane_id = r.result["pane"]["pane_id"]
                except (KeyError, TypeError) as e:
                    raise RunnerError(f"unexpected `pane split` response: {(r.text or '')[:300]}") from e
                ids[step.result] = pane_id
                created_panes.append(pane_id)
        except RunnerError:
            for pane in reversed(created_panes):
                self.herdr.pane_close(pane)
            raise
        return {s["name"]: (tab, ids[assign[i]]) for i, s in enumerate(specs)}

    def _rollback_reviewers(self, created: list[tuple[str, str | None, str]]) -> None:
        for name, tab, pane in reversed(created):
            if self.layout == "tabs" and tab:
                self.herdr.tab_close(tab)
            elif pane:
                self.herdr.pane_close(pane)
            self.status.remove_agent(name)
        if created:
            self.status.save()

    # ----- agent control
    def _start_agent(self, name: str, spec: dict, pane: str) -> None:
        r = self.herdr.agent_start(name, spec["kind"], pane, spec["args"])
        if r.ok:
            # herdr 0.9.0 takes Claude Code's MCP dialog with several servers for an idle agent:
            # look before a prompt is typed into it.
            refusal = mcp_refusal(self.herdr, name)
            if refusal is None:
                self._set_state(name, "idle")
                return
        elif r.error_code == "agent_not_ready":
            outcome = resolve_startup_dialog(self.herdr, name)
            if outcome.resolved:
                self.log(f"{name}: startup dialog resolved automatically")
                self._set_state(name, "idle")
                return
            if not outcome.refusal:
                self._set_state(name, "blocked-start", reason=r.message)
                return
            refusal = outcome.refusal
        else:
            self._set_state(name, "failed", reason=f"{r.error_code}: {r.message}", last_screen=self._pane_screen(pane))
            return
        self.log(f"{name}: startup dialog refused: {refusal}")
        self._set_state(name, "failed", reason=refusal, last_screen=self._pane_screen(pane))

    def _apply_prompt_result(self, name: str, r: HerdrResult) -> None:
        a = self.status.agent(name)
        if r.ok:
            a["prompted"] = True
            self._set_state(name, "working")
            return
        code = r.error_code
        if code in ("agent_prompt_stalled", "timeout"):
            a["prompted"] = True
            self._set_state(name, "prompt_stalled", reason=r.message)
        elif code == "agent_blocked":
            self._set_state(name, "blocked", reason=r.message)
        elif code == "agent_not_found":
            self._set_state(name, "gone", reason="agent exited", last_screen=self._last_screen(name))
        elif code in HERDR_ERROR_CODES:
            self.log(f"{name}: prompt not delivered, herdr error {code}: {r.message}")
            self._set_reason(name, f"{code}: {r.message}")
        else:
            self._set_state(name, "failed", reason=f"{code}: {r.message}", last_screen=self._last_screen(name))

    def _send_review_prompt(self, name: str) -> None:
        a = self.status.agent(name)
        text = self._terminal_text(self.run_dir / "prompts" / f"{a['profile']}.md")
        self._apply_prompt_result(name, self.herdr.agent_prompt(name, text, until="working", timeout_ms=PROMPT_TIMEOUT_MS))

    # ----- subcommands
    def start_reviewers(self) -> dict:
        specs = self.run.get("reviewers") or []
        if not specs:
            raise RunnerError("run.json lists no reviewers")
        if self.status.agents_by_role("reviewer"):
            raise RunnerError("reviewers already started")
        try:
            self.status.set("tree_hash_before", gitutil.tree_hash(self.repo))
        except gitutil.GitError as e:
            raise RunnerError(str(e)) from e
        self.status.save()
        self.log(f"start-reviewers layout={self.layout} n={len(specs)}")
        if self.layout == "tabs":
            placement = self._place_tabs(specs, lambda s: s["profile"])
        else:
            placement = self._place_grid(specs)
            for s in specs:
                tab, pane = placement[s["name"]]
                self.status.add_agent(
                    s["name"], role="reviewer", profile=s["profile"], kind=s["kind"], state="starting",
                    tab=tab, pane=pane, result_file=str(self.run_dir / "reviews" / f"{s['profile']}.md"),
                )
                self._relabel(s["name"], "starting")
        for s in specs:
            self._start_agent(s["name"], s, placement[s["name"]][1])
            if self.status.agent(s["name"])["state"] == "idle":
                self._send_review_prompt(s["name"])
        self.status.set_phase("reviewing")
        return {"agents": {s["name"]: self._summary(s["name"]) for s in specs}}

    # ----- observation
    def _screen_hash(self, name: str) -> str | None:
        text = self.herdr.agent_read(name, source="visible", lines=SCREEN_LINES)
        if text is None:
            return None
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    def _observe(self, name: str) -> bool:
        """Refresh one agent's state from herdr. False when herdr could not be asked at all."""
        before = self.status.agent(name)["state"]
        r = self.herdr.agent_get(name)
        attempts = 1
        while not r.ok and r.error_code != "agent_not_found" and attempts < OBSERVE_ATTEMPTS:
            self.log(f"{name}: agent get failed ({r.error_code}: {r.message}), retrying")
            self.sleep(self.poll_sec)
            r = self.herdr.agent_get(name)
            attempts += 1
        if not r.ok:
            if r.error_code == "agent_not_found":
                self._set_state(name, "gone", reason="agent exited", last_screen=self._last_screen(name))
                return True
            # The agent may well be working; only herdr is unreachable. Leave the state alone.
            self.log(f"{name}: could not observe after {attempts} attempts: {r.error_code}: {r.message}")
            self._set_reason(name, f"{r.error_code}: {r.message}")
            return False
        live = ((r.result or {}).get("agent") or {}).get("agent_status")
        if live not in LIVE_STATUSES:
            if before != "unknown":
                self._set_state(name, "unknown", reason=f"unexpected herdr status {live!r}")
            return True
        new = "blocked-start" if (before == "blocked-start" and live == "blocked") else live
        if new != before:
            self._set_state(name, new)
        return True

    def _pending(self, names: list[str]) -> list[str]:
        return [n for n in names if STATE_CLASS.get(self.status.agent(n)["state"], "unknown") in PENDING_CLASSES]

    def wait(self, agent: str | None = None) -> dict:
        if agent:
            self._agent(agent)
            names = [agent]
        else:
            names = list(self.status.agents_by_role("reviewer"))
        if not names:
            raise RunnerError("no agents to wait for")
        baseline = {n: self.status.agent(n).get("screen_hash") for n in names}
        checkin = float(self.run.get("checkin_sec") or 300)
        start = self.clock()
        reason = "settled"
        while True:
            live = [n for n in names if self.status.agent(n)["state"] not in TERMINAL_STATES]
            if not live:
                reason = "settled"
                break
            changed = []
            unobserved = []
            for n in live:
                before = self.status.agent(n)["state"]
                if not self._observe(n):
                    unobserved.append(n)
                    continue
                if STATE_CLASS.get(self.status.agent(n)["state"], "unknown") != STATE_CLASS.get(before, "unknown"):
                    changed.append(n)
            if len(unobserved) == len(live):
                why = self.status.agent(unobserved[0]).get("reason") or "no reason reported"
                raise RunnerError(f"herdr is not answering: could not observe any agent ({why})")
            if changed:
                reason = "state_change"
                break
            pending = self._pending(names)
            if any(STATE_CLASS.get(self.status.agent(n)["state"], "unknown") == "blocked" for n in pending):
                reason = "blocked"
                break
            if not pending:
                reason = "settled"
                break
            if self.clock() - start >= checkin:
                reason = "checkin"
                break
            self.sleep(self.poll_sec)
        agents = {}
        for n in names:
            a = self.status.agent(n)
            current = a.get("screen_hash") if a["state"] in TERMINAL_STATES else self._screen_hash(n)
            agents[n] = {
                "state": a["state"],
                "since_sec": self.status.since_sec(n),
                "result_ok": bool(a.get("result_ok")),
                "screen_changed": current is not None and current != baseline[n],
                "reason": a.get("reason"),
            }
            if current is not None:
                a["screen_hash"] = current
                self.status.mark_agent(n)
        self.status.save()
        pending = self._pending(names)
        return {"reason": reason, "agents": agents, "pending": pending, "settled": not pending}

    def prompt(self, name: str, file: str | None = None, retry: bool = False) -> dict:
        a = self._agent(name)
        state = a["state"]
        if state in PROMPT_REFUSED_STATES:
            raise RunnerError(
                f"{name} is collected ({a['result_file']}); re-prompting would discard that review"
                " — use run fail if it must leave the run"
            )
        if state in TERMINAL_STATES:
            self.log(f"{name}: prompted while {state} (manual revival)")
        if retry:
            a["retries"] = int(a.get("retries", 0)) + 1
        if file:
            text = self._terminal_text(self._prompt_file(file))
        elif a.get("role") == "fixer":
            raise RunnerError("the fixer takes its task from a file: run prompt <fixer> --file <task.md>")
        else:
            text = self._terminal_text(self._prompt_file(self.run_dir / "prompts" / f"{a['profile']}.md"))
        self._apply_prompt_result(name, self.herdr.agent_prompt(name, text, until="working", timeout_ms=PROMPT_TIMEOUT_MS))
        return {"name": name, **self._summary(name)}

    def fail(self, name: str, reason: str) -> dict:
        self._agent(name)
        self._set_state(name, "failed", reason=reason, last_screen=self._last_screen(name))
        return {"name": name, "state": "failed"}

    def start_fixer(self) -> dict:
        fx = self.run["fixer"]
        name = fx["name"]
        if name in self.status.data["agents"]:
            raise RunnerError(f"fixer {name} is already started")
        env = {**self._base_env(), **self._profile_env(fx["profile"])}
        self.log(f"start-fixer {name} layout={self.layout}")
        if self.layout == "tabs":
            r = self.herdr.tab_create(self.run["workspace_id"], self.repo, f"rv-{self.run_id}: fixer", env)
            if not r.ok or not r.result:
                raise RunnerError(f"herdr tab create failed: {r.error_code}: {r.message}")
            try:
                tab, pane = r.result["tab"]["tab_id"], r.result["root_pane"]["pane_id"]
            except (KeyError, TypeError) as e:
                ident = response_id(r.result, "tab", "tab_id")
                if ident:
                    self.herdr.tab_close(ident)
                raise RunnerError(f"unexpected `tab create` response: {(r.text or '')[:300]}") from e
        else:
            tab, orch_pane = self._orch_pane()
            step = fixer_split()
            r = self.herdr.pane_split(orch_pane, step.direction, step.ratio, self.repo, env)
            if not r.ok or not r.result:
                raise RunnerError(f"herdr pane split failed: {r.error_code}: {r.message}")
            try:
                pane = r.result["pane"]["pane_id"]
            except (KeyError, TypeError) as e:
                ident = response_id(r.result, "pane", "pane_id")
                if ident:
                    self.herdr.pane_close(ident)
                raise RunnerError(f"unexpected `pane split` response: {(r.text or '')[:300]}") from e
        self.status.add_agent(name, role="fixer", profile=fx["profile"], kind=fx["kind"], state="starting", tab=tab, pane=pane, result_file=None)
        self._relabel(name, "starting")
        self._start_agent(name, fx, pane)
        self.status.set_phase("fixing")
        return {"name": name, **self._summary(name)}

    def notify(self, title: str, body: str | None = None, sound: str | None = None) -> dict:
        r = self.herdr.notification_show(title, body, sound)
        # A missing notification backend must not hide the fact that the run waits for a human.
        if sound == "request":
            self.status.set("waiting_for_user", True)
            self._relabel_orch(" ❓")
        self.status.save()
        if not r.ok:
            return {"shown": False, "reason": r.error_code}
        result = r.result or {}
        return {"shown": bool(result.get("shown")), "reason": result.get("reason")}

    def phase(self, name: str) -> dict:
        if name == "finished":
            raise RunnerError("use run finish, not run phase finished")
        if name == "aborted":
            raise RunnerError("'aborted' is set by launch when a run cannot start, not by run phase")
        if name not in PHASES:
            raise RunnerError(f"unknown phase '{name}' (allowed: {', '.join(PHASES)})")
        self.status.set("waiting_for_user", False)
        self.status.set_phase(name)
        self._relabel_orch("")
        return {"phase": name}

    def autodecide(self) -> dict:
        """Switch the run to automatic decisions. One-way, idempotent."""
        data = self.status.data
        was = bool(data.get("autodecide"))
        if not was:
            self.status.set("autodecide", True)
            self.status.set("autodecide_switched_at", now_iso())
        self.status.set("waiting_for_user", False)
        self._relabel_orch("")
        self.status.save()
        return {"autodecide": True, "was": was, "switched_at": data.get("autodecide_switched_at")}

    # ----- collect
    def _drift_status(self) -> str:
        """The `git status --short` lines that were not there at launch, then, marked, the lines of the launch
        that are gone: what was uncommitted then is the owner's own work, listed in uncommitted.txt, and a line
        of it that no longer shows is that work reverted or stashed. A run from before `uncommitted` was
        recorded gets the whole status."""
        before = self.run.get("uncommitted")
        if not isinstance(before, list):
            return gitutil.status_short(self.repo)
        now = gitutil.status_lines(self.repo)
        known, current = set(before), set(now)
        lines = [line for line in now if line not in known]
        lines += [DRIFT_GONE + line for line in before if line not in current]
        return "".join(f"{line}\n" for line in lines) if lines else DRIFT_NOTHING_NEW_OR_GONE + "\n"

    def _check_drift(self) -> bool:
        data = self.status.data
        if data.get("phase") != "reviewing" or not data.get("tree_hash_before"):
            return bool(data.get("drift"))
        try:
            current = gitutil.tree_hash(self.repo)
            if current != data["tree_hash_before"]:
                self.status.set("drift", True)
                self.status.set("drift_status", self._drift_status())
        except gitutil.GitError as e:
            raise RunnerError(str(e)) from e
        return bool(data.get("drift"))

    def collect(self) -> dict:
        collected: list[str] = []
        pending: list[str] = []
        failed: dict[str, str] = {}
        for n, a in self.status.agents_by_role("reviewer").items():
            st = a["state"]
            if st == "collected":
                collected.append(n)
                continue
            if st in ("failed", "gone"):
                ok, why = check_review_file(Path(a["result_file"])) if a.get("result_file") else (False, st)
                if ok:
                    a["result_ok"] = True
                    self._set_state(n, "collected")
                    collected.append(n)
                    continue
                failed[n] = a.get("reason") or st
                continue
            if st in ("idle", "done", "unknown"):
                ok, why = check_review_file(Path(a["result_file"]))
                if ok:
                    a["result_ok"] = True
                    self._set_state(n, "collected")
                    collected.append(n)
                    continue
                if st == "unknown":
                    pending.append(n)
                    continue
                if not a.get("prompted"):
                    self._send_review_prompt(n)
                    pending.append(n)
                    continue
                # collect's own quota: `prompt --retry` spends `retries`, not this one.
                if int(a.get("collect_retries", 0)) == 0:
                    a["collect_retries"] = 1
                    prompt = str(self.run_dir / "prompts" / f"{a['profile']}.md")
                    r = self.herdr.agent_prompt(n, retry_text(a["result_file"], why, prompt), until="working", timeout_ms=PROMPT_TIMEOUT_MS)
                    self._apply_prompt_result(n, r)
                    if self.status.agent(n)["state"] in TERMINAL_STATES:
                        failed[n] = self.status.agent(n).get("reason") or self.status.agent(n)["state"]
                    else:
                        pending.append(n)
                    continue
                reason = f"no valid review file: {why}"
                self._set_state(n, "failed", reason=reason, last_screen=self._last_screen(n))
                failed[n] = reason
                continue
            pending.append(n)
        drift = self._check_drift()
        self.status.save()
        return {"collected": collected, "pending": pending, "failed": failed, "drift": drift, "drift_status": self.status.data.get("drift_status", "")}

    # ----- finish
    def _log_commits(self) -> list[str] | None:
        """The hashes of the commits made since the run was launched. None when git could not be asked."""
        since = self.run.get("head") or self.run["merge_base"]      # a run made before `head` existed
        try:
            out = gitutil.log_oneline(self.repo, f"{since}..HEAD")
        except gitutil.GitError as e:
            self.log(f"finish: cannot read the commit list from git: {e}")
            return None
        return [line.split(None, 1)[0] for line in out.splitlines() if line.strip()]

    def _remove_scratch(self) -> None:
        """The reviewers' experiments may hold a whole copy of the repository: they end with the run."""
        scratch = self.run_dir / "scratch"
        if not scratch.exists():
            return
        try:
            if scratch.is_symlink():                      # os.walk would follow it into a target that is not ours
                scratch.unlink()
                self.log("finish: scratch/ was a symlink; removed the link, not its target")
                return
            for root, dirs, _ in os.walk(scratch):
                for d in dirs:
                    path = os.path.join(root, d)
                    if not os.path.islink(path):          # never chmod through a link out of scratch/
                        try:
                            os.chmod(path, stat.S_IRWXU)
                        except OSError as e:
                            self.log(f"finish: cannot make {path} writable: {e}")
            shutil.rmtree(scratch)
            self.log("finish: removed scratch/")
        except OSError as e:
            self.log(f"finish: cannot remove {scratch}: {e}")

    def finish(self, commits: list[str]) -> dict:
        data = self.status.data
        passed = [c.strip() for c in commits if c and c.strip()]
        # `--commits` is the orchestrator's recollection; git knows what actually landed.
        computed = self._log_commits()
        recorded = passed if computed is None else computed
        if computed is not None and passed and not same_commits(passed, computed):
            self.log(f"finish: --commits {passed} does not match {computed} from git; recording git's list")
        self.status.set("commits", recorded)
        self.status.set("waiting_for_user", False)
        self.status.set_phase("finished")
        self._relabel_orch(" ✓")
        reviews = sum(1 for a in self.status.agents_by_role("reviewer").values() if a["state"] == "collected")
        self.herdr.notification_show("herdr-review: готово", body=f"{self.run_id}: отзывов {reviews}, коммитов {len(data['commits'])}", sound="done")
        closed: list[str] = []
        if self.run.get("close_agents_on_finish"):
            closed, _, _ = self._close_all(self._agent_targets(), "finish")
        self._remove_scratch()
        self.status.save()
        return {"phase": "finished", "commits": data["commits"], "closed": closed}

    # ----- close
    def _agent_targets(self) -> list[tuple[str, bool]]:
        """(id, is_tab) for every agent's own tab (layout tabs) or pane (layout grid)."""
        targets: list[tuple[str, bool]] = []
        for a in self.status.data["agents"].values():
            if self.layout == "tabs" and a.get("tab"):
                targets.append((a["tab"], True))
            elif a.get("pane"):
                targets.append((a["pane"], False))
        return targets

    def _check_tab(self, tab: str, who: str) -> str:
        """What herdr says of <tab>: "ours" while it still shows the tab under this run's label; "gone" when it
        no longer has it or the ID now names someone else's tab (a restarted server reissues IDs); otherwise
        why herdr could not tell."""
        r = self.herdr.tab_get(tab)
        if not r.ok:
            return "gone" if not_found(r) else f"{r.error_code}: {r.message}"
        info = (r.result or {}).get("tab")
        if not isinstance(info, dict):
            return f"unexpected `tab get` response: {(r.text or '')[:300]}"
        label = info.get("label")
        if isinstance(label, str) and label.startswith(f"rv-{self.run_id}:"):
            return "ours"
        self.log(f"{who}: {tab} is now labelled {label!r}, not a tab of this run; left open")
        return "gone"

    def _close_all(self, targets: list[tuple[str, bool]], who: str) -> tuple[list[str], list[str], dict[str, str]]:
        """Close each target: (closed, already closed, failed with the reason). A tab is closed only while
        it is still this run's; a pane of the grid layout lives inside the orchestrator's tab."""
        closed: list[str] = []
        gone: list[str] = []
        failed: dict[str, str] = {}
        for ident, is_tab in targets:
            if is_tab:
                verdict = self._check_tab(ident, who)
                if verdict == "gone":
                    gone.append(ident)
                    continue
                if verdict != "ours":
                    failed[ident] = verdict
                    self.log(f"{who}: cannot check {ident}: {verdict}")
                    continue
            r = self.herdr.tab_close(ident) if is_tab else self.herdr.pane_close(ident)
            if r.ok:
                closed.append(ident)
            elif not_found(r):
                gone.append(ident)
            else:
                failed[ident] = f"{r.error_code}: {r.message}"
                self.log(f"{who}: close {ident} failed: {r.error_code}: {r.message}")
        return closed, gone, failed

    def _check_session(self, environ: Mapping[str, str]) -> None:
        """Refuse a caller outside the herdr session the run was launched in: tab IDs such as `w1:t2` are
        per server, so from there they name that session's tabs. A caller that names no session could be
        talking to any of them. A run launched before the session was recorded is not checked."""
        socket, session = self.run.get("herdr_socket_path"), self.run.get("herdr_session")
        if socket:
            same = environ.get("HERDR_SOCKET_PATH") == socket
        elif session:
            same = environ.get("HERDR_SESSION") == session
        else:
            return
        if not same:
            raise RunnerError(f"run {self.run_id} was started in herdr session '{session or socket}'; run close from a pane of that session")

    def close(self, force: bool = False, environ: Mapping[str, str] | None = None) -> dict:
        """Close every tab and pane the run opened. A run in progress is refused unless forced.
        <environ> is the caller's environment: its herdr session must be the run's."""
        environ = {} if environ is None else environ
        self._check_session(environ)
        phase = self.status.data.get("phase")
        if phase not in ("finished", "aborted") and not force:
            raise RunnerError(f"run {self.run_id} is still in phase {phase}; closing its tabs stops its agents — pass --force")
        closed: list[str] = []
        gone: list[str] = []
        failed: dict[str, str] = {}

        def close_these(targets: list[tuple[str, bool]]) -> None:
            c, g, f = self._close_all(targets, "close")
            closed.extend(c)
            gone.extend(g)
            failed.update(f)

        # Typed in one of the run's own tabs (the orchestrator's has HERDR_REVIEW_RUN set), close ends in
        # that tab's closing: herdr kills the caller with it. So that tab goes last, once the rest is saved.
        own_tab = environ.get("HERDR_TAB_ID")
        own: list[tuple[str, bool]] = []
        # The orchestrator's tab first: while the others close, half a second each, a live orchestrator
        # could still finish the run or start the fixer, whose tab no list taken before would hold.
        orch = (self.status.data.get("orchestrator") or {}).get("tab")
        first = [(orch, True)] if orch else []
        own += [t for t in first if t[0] == own_tab]
        first = [t for t in first if t[0] != own_tab]
        close_these(first)
        handled = {ident for ident, _ in first + own}
        rounds = 0
        while True:
            # What it did meanwhile is on disk: a fixer it started is among the agents there, and so is a phase
            # it set. It can start the fixer while any tab closes, so every round of closes ends in a read.
            self._reload()
            # In the grid layout every agent is a pane of the orchestrator's tab: closing that tab closes them all.
            agents = self._agent_targets() if self.layout == "tabs" else []
            new = [t for t in agents if t[0] not in handled]
            if not new:
                break
            handled.update(ident for ident, _ in new)
            own += [t for t in new if t[0] == own_tab]
            others = [t for t in new if t[0] != own_tab]
            if rounds == CLOSE_ROUNDS:
                failed.update((ident, "opened while close ran; not closed") for ident, _ in others)
                break
            rounds += 1
            close_these(others)
        # The last read decides: a `run finish` that landed while a tab closed stays finished.
        phase = self.status.data.get("phase")
        if force and phase not in ("finished", "aborted"):
            if failed:
                # An agent whose tab did not close may still be working: the run keeps its phase and its
                # scratch/, so a later launch still warns about it and another close --force can finish the job.
                self.log(f"close --force: {len(failed)} of {len(closed) + len(gone) + len(failed)} did not close; the run stays in phase {phase}")
            else:
                # Its agents are gone: the run ends here, and no later launch may count it as unfinished.
                # The caller's own tab does not count: the user is at a shell there, not an agent.
                self.status.set("abort_reason", "closed with --force")
                self.status.set("waiting_for_user", False)
                self.status.set_phase("aborted")
                self._remove_scratch()
        self.status.set("closed_at", now_iso())
        self.status.save()
        close_these(own)
        return {"closed": closed, "already_closed": gone, "failed": failed}
