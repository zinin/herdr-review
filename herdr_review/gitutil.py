"""Small git helpers used by launch and the runner."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    pass


GIT_TIMEOUT_SEC = 30
# Above this total, the untracked files that do not fit keep the old size+mtime form:
# tree_hash runs on every `collect` and must stay fast.
UNTRACKED_HASH_BUDGET_BYTES = 64 * 1024 * 1024
# How much of an untracked file tree_hash reads at a time.
HASH_CHUNK_BYTES = 1024 * 1024
# An untracked file above this size is listed for the reviewers but not read.
UNTRACKED_READ_LIMIT_BYTES = 256 * 1024
BINARY_SNIFF_BYTES = 8192
# The skip mark of an untracked nested repository: git lists it as one `dir/` entry.
NESTED_REPO = "nested git repository"
# A path in `git status --short`: in double quotes, with C escapes inside, when it holds a space, a quote, a
# backslash or a control character; bare otherwise. A rename or copy entry reads `<old> -> <new>`.
STATUS_PATH = r'"(?:[^"\\]|\\.)*"|[^ ]+'
STATUS_RENAME = re.compile(rf"({STATUS_PATH}) -> ({STATUS_PATH})")
# Inside a path quote_path quotes: the characters with an escape of their own. Every other control character,
# and every byte that is not UTF-8, is written as three-digit octal.
PATH_ESCAPES = {"\t": "\\t", "\n": "\\n", "\r": "\\r", '"': '\\"', "\\": "\\\\"}


@dataclass(frozen=True)
class UntrackedFile:
    path: str
    size: int
    skip: str | None = None     # why a reviewer should not read it; None: read it


def _git_env() -> dict[str, str]:
    """An inherited GIT_DIR would outrank `git -C <repo>`; the runner must also never take index.lock."""
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_git_env(), timeout=GIT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SEC}s") from e


def _out(repo: Path | str, *args: str) -> str:
    p = _run(repo, *args)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def _out_bytes(repo: Path | str, *args: str) -> bytes:
    """`_out` for output that names files the filesystem must find again: git's bytes as they are. Read as text,
    a name that is not UTF-8 would come back with U+FFFD in it, and a CR in a name as a newline."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=_git_env(), timeout=GIT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SEC}s") from e
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def repo_root(cwd: Path | str) -> Path:
    p = _run(cwd, "rev-parse", "--show-toplevel")
    if p.returncode != 0:
        raise GitError(f"not inside a git repository: {cwd}")
    return Path(p.stdout.strip())


def current_branch(repo: Path | str) -> str:
    return _out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def ref_exists(repo: Path | str, ref: str) -> bool:
    if not ref or ref.startswith("-"):
        return False
    return _run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def detect_base(repo: Path | str) -> str:
    p = _run(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    for candidate in ("master", "main"):
        if ref_exists(repo, candidate):
            return candidate
    raise GitError("cannot detect the base branch (no origin/HEAD, master or main); pass --base")


def merge_base(repo: Path | str, base: str) -> str:
    if not base or base.startswith("-"):
        raise GitError(f"invalid base ref '{base}'")
    p = _run(repo, "merge-base", "--", base, "HEAD")
    if p.returncode != 0 or not p.stdout.strip():
        raise GitError(f"no merge-base between '{base}' and HEAD: {p.stderr.strip()}")
    return p.stdout.strip()


def head_commit(repo: Path | str) -> str:
    return _out(repo, "rev-parse", "HEAD").strip()


def committed_changes(repo: Path | str, sha: str) -> bool:
    """True when HEAD's tree differs from <sha>'s: the branch has committed changes of its own.

    The tree ids are compared: `git diff --quiet` obeys the user's diff.external with
    diff.trustExitCode, diff.ignoreSubmodules and textconv drivers, and can call a changed tree equal."""
    if not sha or sha.startswith("-"):
        raise GitError(f"invalid commit '{sha}'")
    trees = _out(repo, "rev-parse", f"{sha}^{{tree}}", "HEAD^{tree}").split()
    if len(trees) != 2:
        raise GitError(f"git rev-parse {sha}^{{tree}} HEAD^{{tree}} printed {trees!r}")
    return trees[0] != trees[1]


def _untracked(repo: Path | str) -> list[str]:
    """The untracked, non-ignored files, each name decoded by `os.fsdecode`: a byte that is not UTF-8 becomes a
    lone surrogate and a CR stays a CR, so the name still finds its file."""
    out = _out_bytes(repo, "status", "--porcelain", "--untracked-files=all", "-z")
    return sorted(os.fsdecode(entry[3:]) for entry in out.split(b"\0") if entry.startswith(b"?? "))


def _unprintable(c: str) -> bool:
    """A control character, or a byte that is not UTF-8: `os.fsdecode` makes it a surrogate, U+DC80 to U+DCFF."""
    return c < " " or c == "\x7f" or "\udc80" <= c <= "\udcff"


def quote_path(path: str) -> str:
    """<path>, a name as `os.fsdecode` gives it, quoted the way git quotes a path when the name is not UTF-8 or
    holds a control character: `"caf\\351.py"`, `"Icon\\r"`. Any other name, UTF-8 and spaces included, comes
    back as it is. A quoted name holds no surrogate, so it can be written to a UTF-8 file."""
    text = os.fsencode(path).decode("utf-8", "surrogateescape")
    if not any(_unprintable(c) for c in text):
        return text
    # `& 0xff`: a control character is its own byte, a surrogate U+DCxx stands for the byte xx.
    escaped = (PATH_ESCAPES.get(c) or (f"\\{ord(c) & 0xff:03o}" if _unprintable(c) else c) for c in text)
    return '"' + "".join(escaped) + '"'


def _blob_id(path: Path, st: os.stat_result) -> str:
    """git's blob id for the content of <path>, a regular file when `os.lstat` gave <st>. Computed here, not by
    `git hash-object --stdin-paths`, which fails the whole call on one file it cannot open, C-unquotes a name that
    starts with `"` and runs the user's clean filters; for a file without filters the id is the one git gave.
    A file gone since the lstat is `missing`, one that cannot be read is described by its metadata."""
    unreadable = f"unreadable\0{st.st_size}\0{st.st_mtime_ns}"
    try:
        # A name swapped for a link or a FIFO since the lstat is neither followed nor waited on.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return unreadable
    try:
        now = os.fstat(fd)
        if not stat.S_ISREG(now.st_mode):
            return f"{now.st_size}\0{now.st_mtime_ns}"
        blob = hashlib.sha1(b"blob %d\0" % now.st_size)
        left = now.st_size
        while left > 0:
            chunk = os.read(fd, min(left, HASH_CHUNK_BYTES))
            if not chunk:                   # shrunk since the fstat: the next call reads what is there then
                break
            blob.update(chunk)
            left -= len(chunk)
        return blob.hexdigest()
    except OSError:
        return unreadable
    finally:
        os.close(fd)


def _untracked_meta(repo: Path | str, paths: list[str]) -> str:
    """One line per untracked file: git's blob id for its content, so that a `touch` is not a change and a
    same-size rewrite is. What is not a regular file, and a file past the budget, keeps the old size+mtime form."""
    root = Path(repo)
    meta: list[str] = []
    budget = UNTRACKED_HASH_BUDGET_BYTES
    for path in paths:                       # `_untracked` sorts, so the fallback is deterministic
        try:
            st = os.lstat(root / path)
        except OSError:
            meta.append(f"{path}\0missing")  # vanished since `status`
            continue
        # Only a regular file is read: a nested repository's `dir/` entry and a link are described.
        if not stat.S_ISREG(st.st_mode) or st.st_size > budget:
            meta.append(f"{path}\0{st.st_size}\0{st.st_mtime_ns}")
            continue
        budget -= st.st_size
        meta.append(f"{path}\0{_blob_id(root / path, st)}")
    return "\n".join(meta)


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False


def untracked_files(repo: Path | str) -> list[UntrackedFile]:
    """Every untracked, non-ignored file, one entry per file, marked when a reviewer should not read it.
    An untracked nested repository is one `dir/` entry: git does not look inside it."""
    root = Path(repo)
    files: list[UntrackedFile] = []
    for path in _untracked(repo):
        try:
            st = os.lstat(root / path)
        except OSError:
            continue                          # vanished since `status`
        if stat.S_ISLNK(st.st_mode):
            skip = "symlink"
        elif stat.S_ISDIR(st.st_mode):
            skip = NESTED_REPO
        elif st.st_size > UNTRACKED_READ_LIMIT_BYTES:
            skip = f"larger than {UNTRACKED_READ_LIMIT_BYTES // 1024} KB"
        elif _looks_binary(root / path):
            skip = "binary"
        else:
            skip = None
        files.append(UntrackedFile(path, st.st_size, skip))
    return files


def tree_hash(repo: Path | str) -> str:
    head = head_commit(repo)
    status = _out(repo, "status", "--porcelain", "--untracked-files=all")
    # The user's diff.external or GIT_EXTERNAL_DIFF and textconv drivers would put their own output
    # here: git's random temp paths make every hash differ, and a slow driver hits the git timeout.
    diff = _out(repo, "diff", "--no-ext-diff", "--no-textconv", "HEAD")
    untracked_meta = _untracked_meta(repo, _untracked(repo))
    # A name that is not UTF-8 holds lone surrogates: they encode back to its own bytes, so two such names differ.
    return hashlib.sha256((head + "\n" + status + "\n" + diff + "\n" + untracked_meta).encode("utf-8", "surrogateescape")).hexdigest()


def status_short(repo: Path | str) -> str:
    """`git status --short` as a person reads it, whatever the user's git config says: no colour,
    no `## <branch>` line, UTF-8 paths unquoted, and an untracked directory as one `?? dir/` line
    even under status.showUntrackedFiles=no."""
    return _out(repo, "-c", "color.status=false", "-c", "core.quotePath=false",
                "status", "--short", "--no-branch", "--untracked-files=normal")


def status_lines(repo: Path | str) -> list[str]:
    # Not splitlines(): git prints a name holding U+2028 or U+0085 as is, and that is one entry.
    return [line for line in status_short(repo).split("\n") if line.strip()]


def status_paths(line: str) -> list[str]:
    """The paths a `status_lines` line names: its one path, or for a rename or copy the old path, then the
    new one. The quotes git puts around a path go and its escapes stay: a path reads the same in every
    line, and a path under an untracked `dir/` starts with it."""
    rest = line[3:]
    renamed = STATUS_RENAME.fullmatch(rest) if {"R", "C"} & set(line[:2]) else None
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in (renamed.groups() if renamed else (rest,))]


def log_oneline(repo: Path | str, range_: str) -> str:
    """One line per commit, colour-free even under color.ui=always and without signature lines even
    under log.showSignature=true: `finish` reads the hashes."""
    return _out(repo, "log", "--oneline", "--no-color", "--no-show-signature", range_)
