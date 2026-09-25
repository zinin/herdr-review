"""herdr-review exclusive: heavy commands take turns, one at a time on this machine.

A heavy command — a build, tests, a dependency install, a server — runs only while its wrapper holds an
exclusive flock on <runs_dir>/exclusive.lock. The lock lives exactly as long as the wrapper: the kernel drops it
when the wrapper exits or dies, and the command never inherits its descriptor. <runs_dir>/exclusive.json names
the holder for messages and `status`; it is a hint, never proof.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

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
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36


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


def _catch_stop_signals() -> list[int]:
    """Record SIGTERM, SIGINT and SIGHUP instead of dying of them: the wrapper stops its command and releases the
    queue itself. A signal the caller ignores stays ignored."""
    received: list[int] = []

    def record(signum: int, frame: object) -> None:
        received.append(signum)

    for sig in STOP_SIGNALS:
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, record)
    return received


def _die_with_wrapper() -> Callable[[], None] | None:
    """Linux: the command gets SIGKILL when the wrapper dies, even of SIGKILL (PR_SET_PDEATHSIG). Elsewhere
    nothing: there is no such call. prctl is looked up before the fork: preexec_fn must not import."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    wrapper = os.getpid()

    def preexec() -> None:
        prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
        if os.getppid() != wrapper:          # the wrapper died before the call took effect
            os._exit(EXIT_NOT_EXECUTABLE)

    return preexec


def _adopt_orphans() -> None:
    """Linux: the wrapper becomes the subreaper of the command's processes (PR_SET_CHILD_SUBREAPER). A process
    whose parent dies, such as the background job of an `sh -c` that Ctrl-C killed, is re-parented to the wrapper
    instead of init, so a stop still finds it under the wrapper. Elsewhere, or when the call fails, nothing: the
    wrapper works without it."""
    if not sys.platform.startswith("linux"):
        return
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _children() -> dict[int, list[int]]:
    """Parent pid → child pids: from /proc on Linux, from `ps` elsewhere; empty when neither answers."""
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if (proc / "self" / "stat").exists():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry.name))
        return children
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return children
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def descendants(pid: int) -> list[int]:
    """Every process under <pid>, as the process table shows it now."""
    tree = _children()
    found: list[int] = []
    todo = [pid]
    while todo:
        for child in tree.get(todo.pop(), []):
            if child not in found:
                found.append(child)
                todo.append(child)
    return found


def _signal_all(pids, sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid: int) -> bool:
    """Whether <pid> runs; a zombie counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _finish_off(pids: set[int], grace_sec: float, poll_sec: float) -> None:
    """The command's first process has ended: whatever of <pids> still lives after <grace_sec> gets SIGKILL, and so
    does whatever is under the wrapper by then, such as a process a TERM trap forked after the stop."""
    deadline = time.monotonic() + max(0.0, grace_sec)
    alive = {p for p in pids if _alive(p)}
    while alive and time.monotonic() < deadline:
        time.sleep(min(poll_sec, 0.2))
        alive = {p for p in alive if _alive(p)}
    _signal_all(alive | set(descendants(os.getpid())), signal.SIGKILL)


def _run_command(command: list[str], environ: Mapping[str, str], timeout_sec: float, poll_sec: float,
                 grace_sec: float, received: list[int], shown: str) -> tuple[int, float, bool]:
    """Run the command with an empty stdin, in the wrapper's process group. On a stop signal in <received>, or
    after <timeout_sec>, signal every process under the wrapper: the command, what it started, and on Linux what
    lost its parent (_adopt_orphans); SIGKILL whatever is left <grace_sec> later. A stop signal that reached the
    whole process group and ended the command's first process before the wrapper looked gets the same stop.
    (the exit code, the seconds it ran, whether it timed out)."""
    _adopt_orphans()
    started = time.monotonic()
    try:
        proc = subprocess.Popen(command, env={**environ, NESTED_ENV: "1"}, stdin=subprocess.DEVNULL,
                                preexec_fn=_die_with_wrapper())
    except FileNotFoundError:
        _say(f"command not found: {command[0]}")
        return EXIT_NOT_FOUND, 0.0, False
    except OSError as e:
        _say(f"cannot execute {command[0]}: {e.strerror or e}")
        return EXIT_NOT_EXECUTABLE, 0.0, False
    stopping = 0                 # the signal the command was stopped with
    timed_out = False
    killed = False
    targets: set[int] = set()
    stopped_at = 0.0
    while True:
        try:
            code = proc.wait(timeout=poll_sec)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if not stopping:
            if received:
                stopping = received[0]
            elif now - started >= timeout_sec:
                stopping, timed_out = signal.SIGTERM, True
            if stopping:
                targets = {proc.pid, *descendants(os.getpid())}
                _signal_all(targets, stopping)
                stopped_at = now
        elif not killed and now - stopped_at >= grace_sec:
            targets |= {proc.pid, *descendants(os.getpid())}
            _signal_all(targets, signal.SIGKILL)
            killed = True
    ran = time.monotonic() - started
    if not stopping and received:
        # A stop signal came, but the command's first process ended before the wrapper's poll, usually because the
        # signal reached the whole process group (Ctrl-C): whatever is left under the wrapper gets the same stop.
        stopping = received[0]
        targets = set(descendants(os.getpid()))
        _signal_all(targets, stopping)
        stopped_at = time.monotonic()
    if targets and not killed:
        _finish_off(targets - {proc.pid}, grace_sec - (time.monotonic() - stopped_at), poll_sec)
    if timed_out:
        _say(f'timed out after {format_duration(ran)}; stopped "{shown}"')
        return EXIT_TIMEOUT, ran, True
    if stopping:
        _say(f'{signal.Signals(stopping).name} received; stopped "{shown}"')
        return 128 + stopping, ran, False
    return (code if code >= 0 else 128 - code), ran, False


def run(command: list[str], *, wait_sec: float = DEFAULT_WAIT_SEC, timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        environ: Mapping[str, str] = os.environ, cwd: Path | None = None, poll_sec: float = POLL_SEC,
        notice_sec: float = NOTICE_SEC, grace_sec: float = GRACE_SEC) -> int:
    """Run <command> once this process holds the machine's build queue. The exit code for the CLI."""
    if environ.get(NESTED_ENV):
        return _exec_in_turn(command, environ)
    where = locate(environ, Path.cwd() if cwd is None else Path(cwd))
    who = environ.get(AGENT_ENV) or f"pid {os.getpid()}"
    shown = command_text(command)
    received = _catch_stop_signals()
    fd = _open_queue(where.runs_dir)
    try:
        turn = _take_turn(fd, where.runs_dir, wait_sec, poll_sec, notice_sec, received)
        if turn.outcome == "signal":
            name = signal.Signals(turn.signum).name
            _say(f"{name} received while waiting for a turn; nothing was run")
            _log(where, f"{who} stopped by {name} while waiting")
            return 128 + turn.signum
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
            code, ran, timed_out = _run_command(command, environ, timeout_sec, poll_sec, grace_sec, received, shown)
            _log(where, f"{who} timed out after {format_duration(ran)}" if timed_out
                 else f"{who} done after {format_duration(ran)}, exit {code}")
            return code
        finally:
            remove_holder(where.runs_dir, pid)
    finally:
        os.close(fd)
