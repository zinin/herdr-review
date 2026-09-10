"""status.json: the live state of one run."""
from __future__ import annotations

import copy
import json
import os
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
        return s

    # ----- writes this process owns
    def set(self, key: str, value) -> None:
        """Assign a top-level field and record that this process owns it."""
        self.data[key] = value
        self._dirty.add(key)

    def mark_agent(self, name: str) -> None:
        """Record that this process changed <name>'s entry in place."""
        self._dirty.add("agents")
        self._dirty_agents.add(name)

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
                if name in mine:
                    agents[name] = copy.deepcopy(mine[name])
                else:
                    agents.pop(name, None)      # this process rolled that agent back
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

    def save(self) -> None:
        merged = self._merged()
        self._write(merged)
        self._adopt(merged)
        self._dirty.clear()
        self._dirty_agents.clear()

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

    def set_agent_state(self, name: str, state: str, reason: str | None = None, last_screen: str | None = None) -> None:
        if state not in AGENT_STATES:
            raise StatusError(f"unknown agent state '{state}'")
        a = self.agent(name)
        if a["state"] != state:
            a["state"] = state
            a["state_since"] = now_iso()
        if reason is not None:
            a["reason"] = reason
        if last_screen is not None:
            a["last_screen"] = last_screen
        self.mark_agent(name)
        self.save()

    def since_sec(self, name: str, now: datetime | None = None) -> int:
        a = self.agent(name)
        now = now or datetime.now(timezone.utc)
        return max(0, int((now - _parse_iso(a["state_since"])).total_seconds()))
