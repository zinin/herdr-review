"""herdr-review exclusive: heavy commands take turns, one at a time on this machine.

A heavy command — a build, tests, a dependency install, a server — runs only while its wrapper holds an
exclusive flock on <runs_dir>/exclusive.lock. The lock lives exactly as long as the wrapper: the kernel drops it
when the wrapper exits or dies, and the command never inherits its descriptor. <runs_dir>/exclusive.json names
the holder for messages and `status`; it is a hint, never proof.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple

from .config import ConfigError, load_config
from .status import now_iso

LOCK_NAME = "exclusive.lock"
HOLDER_NAME = "exclusive.json"
RUN_ENV = "HERDR_REVIEW_RUN"
AGENT_ENV = "HERDR_REVIEW_AGENT"
NESTED_ENV = "HERDR_REVIEW_EXCLUSIVE"
PREFIX = "herdr-review exclusive:"
DEFAULT_WAIT_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 1800.0
POLL_SEC = 1.0
NOTICE_SEC = 15.0
GRACE_SEC = 10.0
STOP_WAIT_SEC = 15.0
EXIT_BUSY = 75
EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127
COMMAND_CHARS = 200


class ExclusiveError(Exception):
    """The wrapper itself cannot work: exit 1."""


@dataclass
class Where:
    """The queue a wrapper takes its turn in, and the run it belongs to (None outside a review)."""
    runs_dir: Path
    run_dir: Path | None = None
    run_id: str | None = None


def _run_json(run_dir: Path) -> dict:
    try:
        data = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runs_dir_of(run_dir: Path) -> Path:
    """A run's runs directory: as its run.json records it; for a run launched before run.json recorded it, the
    run directory's grandparent (<runs_dir>/<project>/<run>)."""
    recorded = _run_json(run_dir).get("runs_dir")
    return Path(recorded) if isinstance(recorded, str) and recorded else Path(run_dir).parent.parent


def locate(environ: Mapping[str, str], cwd: Path) -> Where:
    """The queue of the run HERDR_REVIEW_RUN names; outside a review, the queue in the config's runs_dir, resolved
    against <cwd> as launch resolves it."""
    run = environ.get(RUN_ENV)
    if run:
        run_dir = Path(run).expanduser()
        if not (run_dir / "run.json").is_file():
            raise ExclusiveError(f"{RUN_ENV}={run} names no run directory: it has no run.json")
        run_id = _run_json(run_dir).get("run_id")
        return Where(runs_dir_of(run_dir), run_dir, run_id if isinstance(run_id, str) and run_id else None)
    try:
        cfg = load_config(environ=environ)
    except ConfigError as e:
        raise ExclusiveError(str(e)) from e
    return Where((Path(cwd) / cfg.settings.runs_dir).resolve())


def command_text(command: list[str]) -> str:
    """The command as a shell would read it, cut to COMMAND_CHARS."""
    text = shlex.join(command)
    return text if len(text) <= COMMAND_CHARS else text[:COMMAND_CHARS - 1] + "…"


def format_duration(sec: float) -> str:
    s = max(0, int(sec))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def since_sec(started_at: object) -> int | None:
    """Whole seconds since a timestamp in the format of status.now_iso(); None without a usable one."""
    if not isinstance(started_at, str):
        return None
    try:
        start = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if start.tzinfo is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - start).total_seconds()))


def read_holder(runs_dir: Path) -> dict | None:
    try:
        data = json.loads((Path(runs_dir) / HOLDER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_holder(runs_dir: Path, holder: dict) -> None:
    """Atomically: a reader sees the old holder or the new one, never half of it."""
    path = Path(runs_dir) / HOLDER_NAME
    tmp = path.with_name(f"{HOLDER_NAME}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(holder, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def remove_holder(runs_dir: Path, pid: int) -> None:
    """Remove the holder file while it names <pid>: never another holder's."""
    holder = read_holder(runs_dir)
    if holder is not None and holder.get("pid") == pid:
        (Path(runs_dir) / HOLDER_NAME).unlink(missing_ok=True)


def holder_state(holder: dict | None) -> dict:
    """The queue as held by <holder>, the holder file's content; None: there is no readable holder file."""
    h = holder or {}
    return {
        "held": True, "agent": h.get("agent"), "run_id": h.get("run_id"), "pid": h.get("pid"),
        "command": h.get("command"), "since_sec": since_sec(h.get("started_at")),
    }


def queue_state(runs_dir: Path) -> dict:
    """The build queue in <runs_dir>: holder_state() while a wrapper holds it, {"held": False} when nobody does,
    {"held": None, "error": …} when the queue file cannot be probed. The probe is a shared lock, taken and
    dropped at once: a waiter polling meanwhile just tries again."""
    try:
        fd = os.open(Path(runs_dir) / LOCK_NAME, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"held": False}
    except OSError as e:
        return {"held": None, "error": str(e)}
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return holder_state(read_holder(runs_dir))
    except OSError as e:
        return {"held": None, "error": str(e)}
    finally:
        os.close(fd)
    return {"held": False}


def holder_text(state: Mapping) -> str:
    """`<agent> (run <run_id>) has run "<command>" for <duration>`, or `pid <pid> outside a review …`."""
    pid, command = state.get("pid"), state.get("command")
    if pid is None or command is None:
        return "another process holds the queue"
    if state.get("run_id"):
        name = state.get("agent") or f"pid {pid}"
        who = f"{name} (run {state['run_id']})"
    else:
        who = f"pid {pid} outside a review"
    took = state.get("since_sec")
    return f'{who} has run "{command}"' + ("" if took is None else f" for {format_duration(took)}")


class Turn(NamedTuple):
    outcome: str                # "turn", "busy" or "signal"
    waited: float | None        # seconds spent waiting; None when the queue was free at once
    holder: dict                # the queue's holder_state() when busy
    signum: int = 0             # the stop signal that ended the wait


def _open_queue(runs_dir: Path) -> int:
    try:
        runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(runs_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise ExclusiveError(f"cannot open the build queue {runs_dir / LOCK_NAME}: {e}") from e


def _say(line: str) -> None:
    print(f"{PREFIX} {line}", file=sys.stderr, flush=True)


def _log(where: Where, line: str) -> None:
    """A line in the run's runner.log, in the runner's format; nothing outside a review."""
    if where.run_dir is None:
        return
    try:
        with open(where.run_dir / "runner.log", "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} exclusive: {line}\n")
    except OSError:
        pass


def _take_turn(fd: int, runs_dir: Path, wait_sec: float, poll_sec: float, notice_sec: float, received: list[int]) -> Turn:
    """Poll the lock until it is ours, <wait_sec> passes, or a stop signal arrives in <received>. Say who holds the
    queue at once, then every <notice_sec>."""
    start = time.monotonic()
    next_notice = start
    waited: float | None = None
    while True:
        if received:
            return Turn("signal", waited, {}, received[0])
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return Turn("turn", None if waited is None else time.monotonic() - start, {})
        except BlockingIOError:
            pass
        now = time.monotonic()
        waited = now - start
        holder = holder_state(read_holder(runs_dir))
        if waited >= wait_sec:
            return Turn("busy", waited, holder)
        if now >= next_notice:
            _say(f"waiting — {holder_text(holder)}")
            next_notice = now + notice_sec
        time.sleep(min(poll_sec, wait_sec - waited))


def _exec_in_turn(command: list[str], environ: Mapping[str, str]) -> int:
    """Inside another wrapper's turn: become the command, under the outer turn's lock and timeout."""
    try:
        os.execvpe(command[0], command, dict(environ))
    except FileNotFoundError:
        _say(f"command not found: {command[0]}")
        return EXIT_NOT_FOUND
    except OSError as e:
        _say(f"cannot execute {command[0]}: {e.strerror or e}")
        return EXIT_NOT_EXECUTABLE


def _run_command(command: list[str], environ: Mapping[str, str]) -> tuple[int, float]:
    """Run the command with an empty stdin: (its exit code, 128+N for signal N; the seconds it ran)."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(command, env={**environ, NESTED_ENV: "1"}, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        _say(f"command not found: {command[0]}")
        return EXIT_NOT_FOUND, 0.0
    except OSError as e:
        _say(f"cannot execute {command[0]}: {e.strerror or e}")
        return EXIT_NOT_EXECUTABLE, 0.0
    code = proc.wait()
    return (code if code >= 0 else 128 - code), time.monotonic() - started


def run(command: list[str], *, wait_sec: float = DEFAULT_WAIT_SEC, environ: Mapping[str, str] = os.environ,
        cwd: Path | None = None, poll_sec: float = POLL_SEC, notice_sec: float = NOTICE_SEC) -> int:
    """Run <command> once this process holds the machine's build queue. The exit code for the CLI."""
    if environ.get(NESTED_ENV):
        return _exec_in_turn(command, environ)
    where = locate(environ, Path.cwd() if cwd is None else Path(cwd))
    who = environ.get(AGENT_ENV) or f"pid {os.getpid()}"
    shown = command_text(command)
    fd = _open_queue(where.runs_dir)
    try:
        turn = _take_turn(fd, where.runs_dir, wait_sec, poll_sec, notice_sec, [])
        if turn.outcome == "busy":
            text = holder_text(turn.holder)
            _say(f"busy — {text}; nothing was run. Do other work and run the same command again later.")
            _log(where, f"{who} busy after {format_duration(turn.waited or 0)}: {text}")
            return EXIT_BUSY
        pid = os.getpid()
        try:
            write_holder(where.runs_dir, {
                "pid": pid, "agent": environ.get(AGENT_ENV) or None, "run_id": where.run_id,
                "run_dir": None if where.run_dir is None else str(where.run_dir), "command": shown,
                "cwd": os.getcwd(), "started_at": now_iso(),
            })
        except OSError as e:
            raise ExclusiveError(f"cannot write {where.runs_dir / HOLDER_NAME}: {e}") from e
        try:
            if turn.waited is not None:
                _say(f"your turn after {format_duration(turn.waited)}")
            _log(where, f'{who} running "{shown}" after {format_duration(turn.waited or 0)} of waiting')
            code, ran = _run_command(command, environ)
            _log(where, f"{who} done after {format_duration(ran)}, exit {code}")
            return code
        finally:
            remove_holder(where.runs_dir, pid)
    finally:
        os.close(fd)
