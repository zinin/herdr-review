"""status.json: the live state of one run."""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PHASES = ("starting", "reviewing", "aggregating", "fixing", "disputed", "finished", "aborted")
AGENT_STATES = ("starting", "prompt_stalled", "working", "idle", "done", "blocked", "blocked-start", "unknown", "gone", "failed", "collected")


class StatusError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def valid_codex_update_deadline(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        deadline = float(value)
    except OverflowError:
        return None
    return deadline if math.isfinite(deadline) and deadline >= 0 else None


class RunStatus:
    """One run's status document.

    Every `run …` subcommand is a separate process and they overlap in time: a `run wait` holds
    its snapshot for a whole check-in window while the user's `run autodecide` writes the file.
    A save is therefore a merge — only the fields this process actually changed are laid over
    whatever is on disk, and everything else is taken from the file.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "status.json"
        self.data: dict = {}
        self._dirty: set[str] = set()          # top-level keys this process changed
        self._dirty_agents: set[str] = set()   # agents it added, changed or removed
        self._dirty_generations: dict[str, int] = {}
        self._agent_snapshot: dict[str, dict] = {}

    @classmethod
    def create(cls, run_dir: Path, **fields) -> "RunStatus":
        s = cls(run_dir)
        ts = now_iso()
        s.data = {
            "phase": "starting",
            "started_at": ts,
            "updated_at": ts,
            "finished_at": None,
            "agents": {},
            "orchestrator": None,
            "tree_hash_before": None,
            "drift": False,
            "drift_status": "",
            "commits": [],
            "waiting_for_user": False,
        }
        s.data.update(fields)
        s._write(s.data)   # this call establishes the file, so there is nothing to merge with
        s._agent_snapshot = copy.deepcopy(s.data["agents"])
        return s

    @classmethod
    def load(cls, run_dir: Path) -> "RunStatus":
        s = cls(run_dir)
        if not s.path.exists():
            raise StatusError(f"status.json not found in {s.run_dir}")
        try:
            s.data = json.loads(s.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise StatusError(f"cannot read {s.path}: {e}") from e
        if not isinstance(s.data, dict) or "agents" not in s.data:
            raise StatusError(f"{s.path} is not a run status file")
        s._agent_snapshot = copy.deepcopy(s.data["agents"])
        return s

    # ----- writes this process owns
    def set(self, key: str, value) -> None:
        """Assign a top-level field and record that this process owns it."""
        self.data[key] = value
        self._dirty.add(key)

    def mark_agent(self, name: str, *, generation: int | None = None) -> None:
        """Keep the request's generation even when an intermediate save adopted a newer launch."""
        self._dirty.add("agents")
        self._dirty_agents.add(name)
        entry = self.data["agents"].get(name) or self._agent_snapshot.get(name) or {}
        token = int(entry.get("codex_launch_generation", 0)) if generation is None else generation
        self._dirty_generations[name] = min(token, self._dirty_generations.get(name, token))

    def remove_agent(self, name: str) -> None:
        self.data["agents"].pop(name, None)
        self.mark_agent(name)

    # ----- saving
    def _write(self, document: dict) -> None:
        document["updated_at"] = now_iso()
        # Per process: the runner and the user's own `run autodecide` write this file concurrently,
        # and a shared temporary name lets one of them replace status.json with a half-written copy.
        tmp = self.path.with_name(f"status.json.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _on_disk(self) -> dict | None:
        try:
            disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return disk if isinstance(disk, dict) and "agents" in disk else None

    def _merged(self) -> dict:
        """The document on disk with this process's own changes laid over it."""
        merged = self._on_disk()
        if merged is None:
            return copy.deepcopy(self.data)
        for key in self._dirty:
            if key != "agents":
                merged[key] = copy.deepcopy(self.data.get(key))
        if self._dirty_agents:
            agents = merged["agents"] if isinstance(merged.get("agents"), dict) else {}
            mine = self.data.get("agents") or {}
            for name in self._dirty_agents:
                previous = agents.get(name) or {}
                token = self._dirty_generations.get(name, 0)
                baseline = self._agent_snapshot.get(name) or {}
                # A pre-session snapshot cannot overwrite a concurrently confirmed startup.
                if (int(previous.get("codex_launch_generation", 0)) > token
                        or (previous.get("codex_startup_closed") and not baseline.get("codex_startup_closed"))):
                    entry = copy.deepcopy(previous)
                elif name in mine:
                    entry = copy.deepcopy(mine[name])
                    if "update_restarts" in entry or "update_restarts" in previous:
                        entry["update_restarts"] = max(int(entry.get("update_restarts", 0)), int(previous.get("update_restarts", 0)))
                    if int(entry.get("codex_launch_generation", 0)) == int(previous.get("codex_launch_generation", 0)):
                        deadlines = [d for value in (entry.get("codex_update_deadline"), previous.get("codex_update_deadline"))
                                     if (d := valid_codex_update_deadline(value)) is not None]
                        if deadlines:
                            entry["codex_update_deadline"] = min(deadlines)
                    if entry.get("codex_startup_closed") or previous.get("codex_startup_closed"):
                        entry.update(codex_startup_closed=True, codex_startup_pending=False,
                                     codex_update_seen=False, codex_update_pending=False, codex_update_deadline=None)
                else:
                    agents.pop(name, None)      # this process rolled that agent back
                    continue
                # Prompt retries are owned increments; collect retries consume a one-shot quota.
                local = mine.get(name) or {}
                if "retries" in local:
                    delta = int(local["retries"]) - int(baseline.get("retries", 0))
                    entry["retries"] = int(previous.get("retries", 0)) + max(delta, 0)
                if "collect_retries" in local:
                    entry["collect_retries"] = max(int(previous.get("collect_retries", 0)), int(local["collect_retries"]))
                agents[name] = entry
            merged["agents"] = agents
        return merged

    def _adopt(self, merged: dict) -> None:
        """Continue from the merged document, keeping the dicts callers already hold."""
        container = self.data.get("agents")
        container = container if isinstance(container, dict) else {}
        fresh = merged.get("agents")
        fresh = fresh if isinstance(fresh, dict) else {}
        agents: dict = {}
        for name, entry in fresh.items():
            kept = container.get(name)
            if isinstance(kept, dict) and kept is not entry:
                kept.clear()
                kept.update(entry)
                entry = kept
            agents[name] = entry
        container.clear()
        container.update(agents)
        self.data.clear()
        self.data.update(merged)
        self.data["agents"] = container
        self._agent_snapshot = copy.deepcopy(container)

    @contextmanager
    def _locked(self):
        with open(self.run_dir / "status.lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def save(self) -> None:
        with self._locked():
            merged = self._merged()
            self._write(merged)
            self._adopt(merged)
            self._dirty.clear()
            self._dirty_agents.clear()
            self._dirty_generations.clear()

    def codex_generation_current(self, name: str, generation: int) -> bool:
        """Adopt a competing launch, preserving independently owned retry bookkeeping."""
        with self._locked():
            disk = self._on_disk()
            if disk is None or name not in disk["agents"]:
                raise StatusError(f"cannot observe Codex generation for {name}: status.json is unavailable")
            if int(disk["agents"][name].get("codex_launch_generation", 0)) == generation:
                return True
            merged = self._merged()
            if self._dirty:
                self._write(merged)
            self._adopt(merged)
            self._dirty.clear()
            self._dirty_agents.clear()
            self._dirty_generations.clear()
            return False

    def claim_codex_update_restart(self, name: str) -> bool:
        """Reserve the one restart against the live file, atomically with every other status writer."""
        with self._locked():
            disk = self._on_disk()
            if disk is None or name not in disk["agents"]:
                raise StatusError(f"cannot claim Codex restart for {name}: status.json is unavailable")
            current = disk["agents"][name]
            generation = int(current.get("codex_launch_generation", 0))
            if (int(current.get("update_restarts", 0)) >= 1 or current.get("codex_startup_closed")
                    or generation != int(self.agent(name).get("codex_launch_generation", 0))):
                return False
            merged = self._merged()
            merged["agents"][name].update(update_restarts=1, codex_launch_generation=generation + 1,
                                         state="starting", state_since=now_iso(),
                                         codex_startup_pending=True, codex_update_seen=False, codex_update_pending=False,
                                         codex_update_deadline=None,
                                         prompted=False, result_ok=False, reason=None, last_screen=None, screen_hash=None)
            self._write(merged)
            self._adopt(merged)
            self._dirty.clear()
            self._dirty_agents.clear()
            self._dirty_generations.clear()
            return True

    def run_json(self) -> dict:
        p = self.run_dir / "run.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise StatusError(f"cannot read {p}: {e}") from e

    # ----- phases
    def set_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise StatusError(f"unknown phase '{phase}' (allowed: {', '.join(PHASES)})")
        self.set("phase", phase)
        if phase == "finished":
            self.set("finished_at", now_iso())
        self.save()

    # ----- agents
    def add_agent(self, name: str, **fields) -> dict:
        agent = {"state": "starting", "state_since": now_iso(), "reason": None, "last_screen": None, "retries": 0, "collect_retries": 0, "prompted": False, "result_ok": False, "screen_hash": None}
        agent.update(fields)
        if agent.get("role") == "reviewer" and agent.get("kind") == "codex":
            agent.setdefault("codex_launch_generation", 0)
            agent.setdefault("codex_update_deadline", None)
        if agent["state"] not in AGENT_STATES:
            raise StatusError(f"unknown agent state '{agent['state']}'")
        self.data["agents"][name] = agent
        self.mark_agent(name)
        self.save()
        return agent

    def agent(self, name: str) -> dict:
        try:
            return self.data["agents"][name]
        except KeyError:
            raise StatusError(f"unknown agent '{name}' in this run") from None

    def agents_by_role(self, role: str) -> dict[str, dict]:
        return {n: a for n, a in self.data["agents"].items() if a.get("role") == role}

    def set_agent_state(self, name: str, state: str, reason: str | None = None, last_screen: str | None = None, *, generation: int | None = None) -> bool:
        if state not in AGENT_STATES:
            raise StatusError(f"unknown agent state '{state}'")
        a = self.agent(name)
        token = int(a.get("codex_launch_generation", 0)) if generation is None else generation
        if a["state"] != state:
            a["state"] = state
            a["state_since"] = now_iso()
        if reason is not None:
            a["reason"] = reason
        if last_screen is not None:
            a["last_screen"] = last_screen
        self.mark_agent(name, generation=token)
        self.save()
        current = self.agent(name)
        return (int(current.get("codex_launch_generation", 0)) == token and current["state"] == state
                and (reason is None or current.get("reason") == reason)
                and (last_screen is None or current.get("last_screen") == last_screen))

    def since_sec(self, name: str, now: datetime | None = None) -> int:
        a = self.agent(name)
        now = now or datetime.now(timezone.utc)
        return max(0, int((now - _parse_iso(a["state_since"])).total_seconds()))
