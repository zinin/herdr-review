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
import unicodedata
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
CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
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
    """The command as a shell would read it, on one line, cut to COMMAND_CHARS. A control character becomes its
    escape — \\n, \\r, \\t, or \\xNN — so it neither splits a log record nor reaches a terminal."""
    text = "".join(CONTROL_ESCAPES.get(ch, f"\\x{ord(ch):02x}") if unicodedata.category(ch) == "Cc" else ch
                   for ch in shlex.join(command))
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
    """Atomically: a reader sees the old holder or the new one, never half of it. Readable by its owner only, as the
    queue file is: it holds a command line and directories."""
    path = Path(runs_dir) / HOLDER_NAME
    tmp = path.with_name(f"{HOLDER_NAME}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(holder, ensure_ascii=False))
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
    """Inside another wrapper's turn: become the command, under the outer turn's lock and timeout. The signals Python
    ignores at startup go back to their defaults first, as subprocess's restore_signals does for a command the
    wrapper starts: exec keeps an ignored signal ignored, and `yes | head -1` would fail with "Broken pipe"."""
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
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


def _adopt_orphans() -> bool:
    """Linux: the wrapper becomes the subreaper of the command's processes (PR_SET_CHILD_SUBREAPER). A process
    whose parent dies, such as the background job of an `sh -c` that Ctrl-C killed, is re-parented to the wrapper
    instead of init, so a stop still finds it under the wrapper; _reap_orphans collects it once it ends. Whether the
    wrapper became the subreaper: False elsewhere, or when the call fails; the wrapper works without it."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _reap_orphans(first: int | None = None) -> set[int]:
    """Collect every adopted process that has ended (_adopt_orphans), as init would: the command sees a helper it
    stopped as gone (`kill -0 <pid>` fails), not as a zombie that lasts as long as the wrapper. <first>, the command's
    first process while Popen has not collected it, is never collected here: its exit status is the wrapper's. The
    pids collected, which a stop must no longer signal: they may name new processes. Elsewhere than on Linux the
    wrapper adopts nothing, and macOS has no os.waitid: nothing to collect."""
    reaped: set[int] = set()
    while hasattr(os, "waitid"):
        try:
            ended = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)     # a look: it collects nothing
        except ChildProcessError:                                                   # no child at all
            break
        if ended is None or ended.si_pid == first:
            break
        try:
            if os.waitpid(ended.si_pid, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break
        reaped.add(ended.si_pid)
    return reaped


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


def _finish_off(pids: set[int], grace_sec: float, poll_sec: float, *, adopted: bool) -> None:
    """The command's first process has ended: wait up to <grace_sec> while a process of the stop still runs — one of
    <pids>, the processes signalled, or one under the wrapper now, such as a cleanup a TERM trap started after the
    stop — collecting the adopted processes that end (_reap_orphans). Whatever still runs then gets SIGKILL. When
    the wrapper is the subreaper (<adopted>), only the processes under it now count: they hold every live process of
    the stop, and a pid of <pids> outside them may already name an unrelated process."""
    deadline = time.monotonic() + max(0.0, grace_sec)
    signalled = set() if adopted else set(pids)
    while True:
        signalled -= _reap_orphans()
        left = {p for p in signalled | set(descendants(os.getpid())) if _alive(p)}
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_sec, 0.2))
    _signal_all(left, signal.SIGKILL)


def _run_command(command: list[str], environ: Mapping[str, str], timeout_sec: float, poll_sec: float,
                 grace_sec: float, received: list[int], shown: str) -> tuple[int, float, bool]:
    """Run the command with an empty stdin, in the wrapper's process group. On a stop signal in <received>, or
    after <timeout_sec>, signal every process under the wrapper: the command, what it started, and on Linux what
    lost its parent (_adopt_orphans); SIGCONT follows the signal, so a stopped process handles it. SIGKILL whatever
    is left <grace_sec> later. A stop signal that reached the whole process group and ended the command's first
    process before the wrapper looked gets the same stop. Every poll collects the adopted processes that have ended
    (_reap_orphans).
    (the exit code, the seconds it ran, whether it timed out)."""
    adopted = _adopt_orphans()
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
        targets -= _reap_orphans(proc.pid)       # a collected pid may name a new process: no stop signal goes to it
        now = time.monotonic()
        if not stopping:
            if received:
                stopping = received[0]
            elif now - started >= timeout_sec:
                stopping, timed_out = signal.SIGTERM, True
            if stopping:
                targets = {proc.pid, *descendants(os.getpid())}
                _signal_all(targets, stopping)
                _signal_all(targets, signal.SIGCONT)
                stopped_at = now
        elif not killed and now - stopped_at >= grace_sec:
            if adopted:
                # With the wrapper as subreaper every live process of the command is in its tree; a pid of the
                # stop-time snapshot outside it may already name an unrelated process.
                targets = {proc.pid, *descendants(os.getpid())}
            else:
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
        _signal_all(targets, signal.SIGCONT)
        stopped_at = time.monotonic()
    if targets and not killed:
        _finish_off(targets - {proc.pid}, grace_sec - (time.monotonic() - stopped_at), poll_sec, adopted=adopted)
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


def is_wrapper(pid: int) -> bool:
    """Whether <pid> runs `herdr-review exclusive`, the word `exclusive` right after a word naming herdr-review: a
    path whose last part is herdr-review, or the module herdr_review or herdr_review.cli as `python3 -m` shows it;
    a word that merely contains the name, such as herdr-reviewer, does not count. The holder file is a hint, and a
    pid is reused. Never pid 1, 0 or a negative one: os.kill would signal init, the caller's process group or every
    process."""
    if pid <= 1:
        return False
    if Path("/proc/self/cmdline").exists():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        words = [w.decode("utf-8", "replace") for w in raw.split(b"\0") if w]
    else:
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        words = out.stdout.split()
    return any((os.path.basename(word) == "herdr-review" or word in ("herdr_review", "herdr_review.cli"))
               and after == "exclusive" for word, after in zip(words, words[1:]))


class Stopped(str):
    """What stop_holder stopped, as `exclusive_stopped` gives it in JSON: "<agent>: <command>", and " (still running
    after <wait>)" when the wrapper still held the queue <wait> after SIGTERM. <what> and <still_running_after> (the
    seconds waited; None when the queue came free) let the text of `close` say it in Russian."""

    what: str
    still_running_after: float | None

    def __new__(cls, what: str, still_running_after: float | None = None) -> Stopped:
        note = "" if still_running_after is None else f" (still running after {format_duration(still_running_after)})"
        stopped = super().__new__(cls, what + note)
        stopped.what, stopped.still_running_after = what, still_running_after
        return stopped


def stop_holder(runs_dir: Path, run_id: str, agent: str | None = None, *, wait_sec: float = STOP_WAIT_SEC,
                clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                log: Callable[[str], None] = lambda line: None) -> Stopped | None:
    """Stop the wrapper that holds the queue for run <run_id> (for <agent>, when given) with SIGTERM, which it passes
    to its command before it releases the queue; SIGTERM is followed by SIGCONT, so a wrapper stopped by SIGTSTP or
    SIGSTOP handles it. A Stopped when one was stopped, noting when the queue is still held <wait_sec> later; None
    when the queue is free or held by someone else. Never SIGKILL: that would orphan the command without the lock,
    and the wrapper's --timeout still bounds it."""
    state = queue_state(runs_dir)
    if state.get("held") is not True or state.get("run_id") != run_id:
        return None
    if agent is not None and state.get("agent") != agent:
        return None
    pid = state.get("pid")
    if not isinstance(pid, int) or not is_wrapper(pid):
        log(f"exclusive: the holder file names pid {pid}, which does not run herdr-review exclusive; left alone")
        return None
    name = state.get("agent") or f"pid {pid}"
    what = f"{name}: {state.get('command')}"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError as e:
        log(f"exclusive: cannot stop {what}: {e}")
        return None
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:                   # the wrapper exited meanwhile
        pass
    deadline = clock() + wait_sec
    while clock() < deadline:
        now_state = queue_state(runs_dir)
        if now_state.get("held") is not True or now_state.get("pid") != pid:
            log(f"exclusive: stopped {what}")
            return Stopped(what)
        sleep(0.2)
    log(f"exclusive: {what} still holds the queue {format_duration(wait_sec)} after SIGTERM; left to its --timeout")
    return Stopped(what, wait_sec)
