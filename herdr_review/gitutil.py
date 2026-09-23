"""Small git helpers used by launch and the runner."""
from __future__ import annotations

import hashlib
import os
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
# An untracked file above this size is listed for the reviewers but not read.
UNTRACKED_READ_LIMIT_BYTES = 256 * 1024
BINARY_SNIFF_BYTES = 8192
# The skip mark of an untracked nested repository: git lists it as one `dir/` entry.
NESTED_REPO = "nested git repository"


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


def _run_stdin(repo: Path | str, stdin: str, *args: str) -> subprocess.CompletedProcess:
    """`_run` for the one subcommand that takes its input on stdin."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_git_env(), timeout=GIT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SEC}s") from e


def _out(repo: Path | str, *args: str) -> str:
    p = _run(repo, *args)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
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
    """True when HEAD's tree differs from <sha>: the branch has committed changes of its own."""
    p = _run(repo, "diff", "--quiet", sha, "HEAD", "--")
    if p.returncode == 1:
        return True
    if p.returncode != 0:
        raise GitError(f"git diff --quiet {sha} HEAD failed: {p.stderr.strip()}")
    return False


def _untracked(repo: Path | str) -> list[str]:
    out = _out(repo, "status", "--porcelain", "--untracked-files=all", "-z")
    return sorted(entry[3:] for entry in out.split("\0") if entry.startswith("?? "))


def _untracked_meta(repo: Path | str, paths: list[str]) -> str:
    """One line per untracked file: git's object id for its content, so that a `touch` is not a
    change and a same-size rewrite is. Files that cannot be hashed keep the old size+mtime form."""
    root = Path(repo)
    meta: dict[str, str] = {}
    hashable: list[str] = []
    budget = UNTRACKED_HASH_BUDGET_BYTES
    for path in paths:                       # `_untracked` sorts, so the fallback is deterministic
        try:
            st = os.lstat(root / path)
        except OSError:
            meta[path] = "missing"           # vanished since `status`; git would fail on it
            continue
        # Only a regular file is hashed: git hash-object fails on a nested repository's `dir/` entry
        # and on a link to a directory.
        if not stat.S_ISREG(st.st_mode) or "\n" in path or st.st_size > budget:
            meta[path] = f"{st.st_size}\0{st.st_mtime_ns}"
            continue
        budget -= st.st_size
        hashable.append(path)
    if hashable:
        p = _run_stdin(repo, "".join(f"{path}\n" for path in hashable), "hash-object", "--stdin-paths")
        if p.returncode != 0:
            raise GitError(f"git hash-object failed: {p.stderr.strip()}")
        ids = p.stdout.split()
        if len(ids) != len(hashable):
            raise GitError(f"git hash-object returned {len(ids)} ids for {len(hashable)} paths")
        meta.update(zip(hashable, ids))
    return "\n".join(f"{path}\0{meta[path]}" for path in paths)


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
    diff = _out(repo, "diff", "HEAD")
    untracked_meta = _untracked_meta(repo, _untracked(repo))
    return hashlib.sha256((head + "\n" + status + "\n" + diff + "\n" + untracked_meta).encode("utf-8", "replace")).hexdigest()


def status_short(repo: Path | str) -> str:
    """`git status --short` as a person reads it, whatever the user's git config says: no colour,
    no `## <branch>` line, UTF-8 paths unquoted, and an untracked directory as one `?? dir/` line
    even under status.showUntrackedFiles=no."""
    return _out(repo, "-c", "color.status=false", "-c", "core.quotePath=false",
                "status", "--short", "--no-branch", "--untracked-files=normal")


def status_lines(repo: Path | str) -> list[str]:
    # Not splitlines(): git prints a name holding U+2028 or U+0085 as is, and that is one entry.
    return [line for line in status_short(repo).split("\n") if line.strip()]


def log_oneline(repo: Path | str, range_: str) -> str:
    """One line per commit, colour-free even under color.ui=always and without signature lines even
    under log.showSignature=true: `finish` reads the hashes."""
    return _out(repo, "log", "--oneline", "--no-color", "--no-show-signature", range_)
