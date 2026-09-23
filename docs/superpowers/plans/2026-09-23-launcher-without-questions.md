# A launcher that asks no questions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/herdr-review:review` never asks the owner about uncommitted files or startup dialogs: `launch` decides the scope of the change and protects the owner's files, and claude agents start without Claude Code's MCP approval dialog.

**Architecture:** `launch` resolves the scope (`commits` or `worktree`) from git state, records the owner's uncommitted files in `<run_dir>/uncommitted.txt` and renders scope-specific reviewer steps; the fixer and orchestrator prompts protect those files. `dialogs.py` gains a table of known startup dialogs — it answers the trust dialogs of Claude Code, Codex and Grok and refuses the MCP dialog — and claude agents start with a session-only `--settings '{"enableAllProjectMcpServers": true}'`. Four agreed extras: a scratch directory per reviewer, the run's own commits in `finish`, fixer commit messages in the repository's style, and `herdr-review close`.

**Tech Stack:** Python ≥ 3.11 (stdlib + PyYAML), git, herdr 0.9.0 CLI, `unittest`, bats.

**Spec:** `docs/superpowers/specs/2026-09-23-launcher-without-questions-design.md`

## Global Constraints

- Python ≥ 3.11, stdlib plus PyYAML only; no new dependency.
- Code, comments, prompts, docs and commit messages in English. The launch summary, the `close` output and the orchestrator's report stay in Russian, as today.
- The plugin never edits the user's config file.
- `CLAUDE_MCP_SETTINGS = '{"enableAllProjectMcpServers": true}'`, added only for kind `claude` and only when the profile's args hold no `--settings` / `--settings=…`.
- Scopes: `auto` (default), `commits`, `worktree`.
- `UNTRACKED_READ_LIMIT_BYTES = 256 * 1024`, `BINARY_SNIFF_BYTES = 8192`, `UNCOMMITTED_INLINE = 50`, `UNTRACKED_INLINE = 100`, `MAX_DIALOGS = 3`, dialog wait `30000` ms.
- Run-directory files: `uncommitted.txt`, `scratch/<profile>/`, `fix-auto-commit.txt`, `fix-<n>-commit.txt`.
- Fixer commits carry no trailer and never mention the review, the reviewers or herdr-review.
- Stage files by exact path in every commit. Never `git add -A` or `git add .`: the working tree holds the owner's untracked `docs/2026-09-23-launcher-without-questions-prompt.md`, which must stay untracked.
- Unit tests run from the repository root with `python3 -m unittest …`; bats with `bats tests/bats/<file>`; everything with `tests/run.sh`. Where the environment asks for build and test commands to go through a runner agent, delegate them; the commands stay as written.
- Do not push and do not open a pull request. Before a pull request, and only when the owner asks for one: `git rm -r docs/superpowers` and commit.

## Review Focus

- A git config that changes `git status` output (`color.ui=always`, `status.showUntrackedFiles=no`, UTF-8 paths): `uncommitted.txt` and the prompts must still hold a plain, complete list. Test: Task 1, `test_status_lines_survive_the_users_git_config`.
- Claude Code shows the trust dialog and then the MCP dialog in one start: the trust dialog gets its keys, the MCP dialog gets none, the agent fails with the reason. Test: Task 3, `test_mcp_dialog_after_the_trust_dialog_is_refused`.
- A claude profile that already passes `--settings=<file>` (the `=` form): nothing is added and the owner's settings file stays in force. Test: Task 3, `test_a_profile_with_its_own_settings_is_left_alone` (unit and launch).
- More than 50 uncommitted entries: the reviewer prompt stops at 50 and points at `uncommitted.txt`, which holds them all. Test: Task 4, `test_a_long_list_of_uncommitted_files_is_capped_in_the_prompt_and_complete_on_disk`.
- `close` on a run whose tabs the owner partly closed by hand: those count as already closed, the rest close, exit 0; a real herdr failure exits 1. Tests: Task 8, `test_a_tab_closed_by_hand_is_not_an_error` and the bats `close` test.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `herdr_review/gitutil.py` | git facts: HEAD, committed changes, status lines, untracked files with skip marks | 1, 4 |
| `herdr_review/config.py` | `settings.scope` | 2 |
| `herdr_review/scope.py` (new) | what the change is: scope resolution, reviewer-prompt blocks, the orchestrator's scope line | 2, 6 |
| `herdr_review/dialogs.py` | known startup dialogs, their answers, the MCP refusal, the claude start args | 3 |
| `herdr_review/launch.py` | scope, `uncommitted.txt`, `head`, scratch directories, start args, prompt rendering, summary fields | 3, 4, 6 |
| `herdr_review/runner.py` | refusal → `failed`, commits since `head`, scratch cleanup, `close` | 3, 7, 8 |
| `herdr_review/cli.py` | `--scope`, the scope lines of the summary, `close` | 4, 8 |
| `prompts/scope-commits.md`, `prompts/scope-worktree.md` (new) | reviewer steps 1–2 per scope | 2 |
| `prompts/reviewer.md` | scope steps, hard rules, scratch directory | 4 |
| `prompts/fixer-auto.md`, `prompts/fixer-decision.md` | protection of uncommitted files, commit messages in the repository's style | 5 |
| `prompts/orchestrator.md` | run facts, dialogs, permissions, dismissals, verification by hash, report | 6 |
| `skills/review/SKILL.md`, `README.md`, `config.example.yaml` | the behavior of each task, documented in that task | 3, 4, 5, 7, 8 |
| `CHANGELOG.md`, `tests/SMOKE.md` | release notes, real-herdr checklist | 9 |

---

### Task 1: git helpers for the scope of a review

**Files:**
- Modify: `herdr_review/gitutil.py`
- Test: `tests/unit/test_gitutil.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `UNTRACKED_READ_LIMIT_BYTES: int = 262144`, `BINARY_SNIFF_BYTES: int = 8192`
  - `@dataclass(frozen=True) class UntrackedFile: path: str; size: int; skip: str | None = None` — `skip` is `"symlink"`, `"larger than 256 KB"` or `"binary"`
  - `head_commit(repo: Path | str) -> str`
  - `committed_changes(repo: Path | str, sha: str) -> bool`
  - `status_short(repo: Path | str) -> str` (now colour-free, UTF-8 paths, untracked mode `normal`)
  - `status_lines(repo: Path | str) -> list[str]`
  - `untracked_files(repo: Path | str) -> list[UntrackedFile]`

- [ ] **Step 1: Write the failing tests**

Append to class `GitUtilTest` in `tests/unit/test_gitutil.py` (the file already imports `os`, `subprocess`, `gitutil` and defines `git`):

```python
    def test_head_commit(self):
        out = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(gitutil.head_commit(self.repo), out)

    def test_committed_changes_compare_head_with_the_base(self):
        base = gitutil.merge_base(self.repo, "master")
        self.assertFalse(gitutil.committed_changes(self.repo, base))
        (self.repo / "a.txt").write_text("uncommitted\n")
        self.assertFalse(gitutil.committed_changes(self.repo, base))      # uncommitted edits do not count
        git(self.repo, "commit", "-q", "-am", "change")
        self.assertTrue(gitutil.committed_changes(self.repo, base))
        with self.assertRaises(gitutil.GitError):
            gitutil.committed_changes(self.repo, "not-a-commit")

    def test_status_lines_collapse_untracked_directories(self):
        (self.repo / "a.txt").write_text("edited\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "one.md").write_text("x\n")
        (self.repo / "notes" / "two.md").write_text("y\n")
        (self.repo / "loose.txt").write_text("z\n")
        self.assertEqual(gitutil.status_lines(self.repo), [" M a.txt", "?? loose.txt", "?? notes/"])

    def test_status_lines_of_a_clean_tree_are_empty(self):
        self.assertEqual(gitutil.status_lines(self.repo), [])

    def test_status_lines_survive_the_users_git_config(self):
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        git(self.repo, "config", "color.ui", "always")
        (self.repo / "заметка.md").write_text("x\n")
        self.assertEqual(gitutil.status_lines(self.repo), ["?? заметка.md"])
        self.assertEqual(gitutil.status_short(self.repo), "?? заметка.md\n")

    def test_untracked_files_mark_what_a_reviewer_should_skip(self):
        (self.repo / "src").mkdir()
        (self.repo / "src" / "new.py").write_text("print(1)\n")
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0")
        (self.repo / "big.diff").write_text("x" * (gitutil.UNTRACKED_READ_LIMIT_BYTES + 1))
        os.symlink("a.txt", self.repo / "link.txt")
        files = {f.path: f for f in gitutil.untracked_files(self.repo)}
        self.assertEqual(sorted(files), ["big.diff", "link.txt", "logo.png", "src/new.py"])
        self.assertEqual(files["src/new.py"], gitutil.UntrackedFile("src/new.py", 9))
        self.assertEqual(files["logo.png"].skip, "binary")
        self.assertEqual(files["big.diff"].skip, "larger than 256 KB")
        self.assertEqual(files["link.txt"].skip, "symlink")

    def test_untracked_files_leave_out_ignored_ones(self):
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "out.o").write_text("junk\n")
        self.assertEqual([f.path for f in gitutil.untracked_files(self.repo)], [".gitignore"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_gitutil -v`
Expected: the seven new tests fail or error with `AttributeError: module 'herdr_review.gitutil' has no attribute …` (`status_lines_survive…` fails on the colour codes); the existing tests pass.

- [ ] **Step 3: Implement**

In `herdr_review/gitutil.py`, extend the imports:

```python
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
```

After `UNTRACKED_HASH_BUDGET_BYTES = 64 * 1024 * 1024` add:

```python
# An untracked file above this size is listed for the reviewers but not read.
UNTRACKED_READ_LIMIT_BYTES = 256 * 1024
BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class UntrackedFile:
    path: str
    size: int
    skip: str | None = None     # why a reviewer should not read it; None: read it
```

After `merge_base` add:

```python
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
```

After `_untracked_meta` add:

```python
def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False


def untracked_files(repo: Path | str) -> list[UntrackedFile]:
    """Every untracked, non-ignored file, one entry per file, marked when a reviewer should not read it."""
    root = Path(repo)
    files: list[UntrackedFile] = []
    for path in _untracked(repo):
        try:
            st = os.lstat(root / path)
        except OSError:
            continue                          # vanished since `status`
        if stat.S_ISLNK(st.st_mode):
            skip = "symlink"
        elif st.st_size > UNTRACKED_READ_LIMIT_BYTES:
            skip = f"larger than {UNTRACKED_READ_LIMIT_BYTES // 1024} KB"
        elif _looks_binary(root / path):
            skip = "binary"
        else:
            skip = None
        files.append(UntrackedFile(path, st.st_size, skip))
    return files
```

In `tree_hash`, replace `head = _out(repo, "rev-parse", "HEAD").strip()` with `head = head_commit(repo)`.

Replace `status_short`:

```python
def status_short(repo: Path | str) -> str:
    """`git status --short` as a person reads it, whatever the user's git config says: no colour,
    UTF-8 paths unquoted, and an untracked directory as one `?? dir/` line even under
    status.showUntrackedFiles=no."""
    return _out(repo, "-c", "color.status=false", "-c", "core.quotePath=false",
                "status", "--short", "--untracked-files=normal")


def status_lines(repo: Path | str) -> list[str]:
    return [line for line in status_short(repo).splitlines() if line.strip()]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_gitutil -v`
Expected: all tests pass.

Run: `tests/run.sh`
Expected: every unit and bats test passes (`drift_status` keeps listing `a.txt`).

- [ ] **Step 5: Commit**

```bash
git add herdr_review/gitutil.py tests/unit/test_gitutil.py
git commit -m "feat: git helpers for the scope of a review" -m "head_commit, committed_changes, status_lines and untracked_files with
skip marks (binary, larger than 256 KB, symlink). status_short now reads
the same whatever the user's git config says: no colour, UTF-8 paths,
untracked directories collapsed even under showUntrackedFiles=no."
```

---

### Task 2: the scope policy and its reviewer prompt fragments

**Files:**
- Modify: `herdr_review/config.py`
- Create: `herdr_review/scope.py`
- Create: `prompts/scope-commits.md`, `prompts/scope-worktree.md`
- Test: `tests/unit/test_config.py`, `tests/unit/test_prompts.py`
- Create test: `tests/unit/test_scope.py`

**Interfaces:**
- Consumes: `UntrackedFile` (Task 1).
- Produces:
  - `config.SCOPES = ("auto", "commits", "worktree")`; `Settings.scope: str = "auto"`; `public_json(cfg)["settings"]["scope"]`
  - `scope.UNCOMMITTED_INLINE = 50`, `scope.UNTRACKED_INLINE = 100`
  - `class ScopeError(Exception)`
  - `resolve_scope(requested: str, committed: bool, uncommitted: bool, base: str, merge_base: str) -> str` — returns `"commits"` or `"worktree"`, raises `ScopeError`
  - `uncommitted_counts(lines: list[str]) -> tuple[int, int]` — (changed tracked files, untracked entries)
  - `human_size(n: int) -> str`
  - `uncommitted_block(lines: list[str], listing: Path) -> str`
  - `untracked_block(files: list[UntrackedFile]) -> str`
  - `reviewer_steps(scope: str, merge_base: str, uncommitted: list[str], untracked: list[UntrackedFile], listing: Path) -> str`

- [ ] **Step 1: Write the failing tests**

Append to class `ParseConfigTest` in `tests/unit/test_config.py`:

```python
    def test_scope_setting(self):
        self.assertEqual(parse_config({"profiles": {"p": {"kind": "x"}}}, {}).settings.scope, "auto")
        cfg = parse_config({"profiles": {"p": {"kind": "x"}}, "settings": {"scope": "worktree"}}, {})
        self.assertEqual(cfg.settings.scope, "worktree")
        self.assertEqual(public_json(cfg)["settings"]["scope"], "worktree")
        errs = errors_of({"profiles": {"p": {"kind": "x"}}, "settings": {"scope": "everything"}})
        self.assertTrue(any("settings.scope" in e and "auto, commits, worktree" in e for e in errs))
```

In `tests/unit/test_prompts.py`, add two entries to `EXPECTED`:

```python
    "scope-commits.md": {"MERGE_BASE", "UNCOMMITTED"},
    "scope-worktree.md": {"MERGE_BASE", "UNTRACKED"},
```

Create `tests/unit/test_scope.py`:

```python
import unittest
from pathlib import Path

from herdr_review import scope
from herdr_review.gitutil import UntrackedFile

MB = "abcdef0123456789" + "0" * 24


class ResolveScopeTest(unittest.TestCase):
    def test_auto_prefers_the_commits(self):
        self.assertEqual(scope.resolve_scope("auto", True, True, "master", MB), "commits")
        self.assertEqual(scope.resolve_scope("auto", True, False, "master", MB), "commits")

    def test_auto_falls_back_to_the_working_tree(self):
        self.assertEqual(scope.resolve_scope("auto", False, True, "master", MB), "worktree")

    def test_explicit_scopes(self):
        self.assertEqual(scope.resolve_scope("worktree", True, True, "master", MB), "worktree")
        self.assertEqual(scope.resolve_scope("worktree", True, False, "master", MB), "worktree")
        self.assertEqual(scope.resolve_scope("commits", True, True, "master", MB), "commits")

    def test_commits_without_a_commit_names_the_way_out(self):
        with self.assertRaises(scope.ScopeError) as ctx:
            scope.resolve_scope("commits", False, True, "master", MB)
        self.assertIn("nothing committed on this branch since master (abcdef012345)", str(ctx.exception))
        self.assertIn("--scope worktree", str(ctx.exception))

    def test_nothing_at_all(self):
        for requested in ("auto", "commits", "worktree"):
            with self.subTest(requested=requested):
                with self.assertRaises(scope.ScopeError) as ctx:
                    scope.resolve_scope(requested, False, False, "master", MB)
                self.assertIn("nothing to review: the working tree equals master (abcdef012345)", str(ctx.exception))

    def test_unknown_scope(self):
        with self.assertRaises(scope.ScopeError):
            scope.resolve_scope("everything", True, True, "master", MB)


class BlocksTest(unittest.TestCase):
    def test_uncommitted_block_inlines_up_to_the_limit(self):
        listing = Path("/run/uncommitted.txt")
        self.assertIn("clean", scope.uncommitted_block([], listing))
        short = scope.uncommitted_block([" M a.txt", "?? notes/"], listing)
        self.assertIn("```\n M a.txt\n?? notes/\n```", short)
        self.assertNotIn("more", short)
        many = [f"?? f{i}.txt" for i in range(scope.UNCOMMITTED_INLINE + 7)]
        text = scope.uncommitted_block(many, listing)
        self.assertIn(f"?? f{scope.UNCOMMITTED_INLINE - 1}.txt", text)
        self.assertNotIn(f"?? f{scope.UNCOMMITTED_INLINE}.txt", text)
        self.assertIn("…and 7 more: /run/uncommitted.txt lists them all.", text)

    def test_untracked_block_marks_skipped_files(self):
        text = scope.untracked_block([
            UntrackedFile("src/new.py", 900),
            UntrackedFile("review.diff", 408 * 1024, "larger than 256 KB"),
            UntrackedFile("logo.png", 2048, "binary"),
        ])
        self.assertIn("- `src/new.py` (900 B)", text)
        self.assertIn("- `review.diff` (408 KB) — skip: larger than 256 KB", text)
        self.assertIn("- `logo.png` (2 KB) — skip: binary", text)
        self.assertEqual(scope.untracked_block([]), "There are none.")

    def test_untracked_block_caps_the_list(self):
        files = [UntrackedFile(f"f{i}.txt", 1) for i in range(scope.UNTRACKED_INLINE + 3)]
        text = scope.untracked_block(files)
        self.assertIn("…and 3 more", text)
        self.assertIn("git ls-files --others --exclude-standard", text)

    def test_counts_and_sizes(self):
        self.assertEqual(scope.uncommitted_counts([" M a.txt", "M  b.txt", "?? notes/", "?? x"]), (2, 2))
        self.assertEqual(scope.uncommitted_counts([]), (0, 0))
        self.assertEqual(scope.human_size(3 * 1024 * 1024 // 2), "1.5 MB")


class ReviewerStepsTest(unittest.TestCase):
    def test_commits_steps(self):
        text = scope.reviewer_steps("commits", MB, [" M x.txt"], [], Path("/run/uncommitted.txt"))
        self.assertTrue(text.startswith(f"1. Run `git diff {MB} HEAD --`"))
        self.assertIn("git show HEAD:<path>", text)
        self.assertIn(" M x.txt", text)
        self.assertNotIn("{", text)

    def test_worktree_steps(self):
        text = scope.reviewer_steps("worktree", MB, [], [UntrackedFile("new.py", 10)], Path("/run/uncommitted.txt"))
        self.assertTrue(text.startswith(f"1. Run `git diff {MB} --`"))
        self.assertIn("- `new.py` (10 B)", text)
        self.assertIn("do not `git add` anything", text)
        self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_config tests.unit.test_prompts tests.unit.test_scope -v`
Expected: `test_scope` errors with `ImportError: cannot import name 'scope'`; `test_scope_setting` fails; `test_placeholder_sets` errors with `FileNotFoundError` for `scope-commits.md`.

- [ ] **Step 3: Implement**

In `herdr_review/config.py`:

```python
LAYOUTS = ("tabs", "grid")
SCOPES = ("auto", "commits", "worktree")
```

```python
SETTINGS_KEYS = ("layout", "autodecide", "close_agents_on_finish", "checkin_sec", "runs_dir", "scope")
```

In `Settings`, after `runs_dir`:

```python
    scope: str = "auto"
```

In `_parse_settings`, after the `layout` block:

```python
    if "scope" in raw:
        if raw["scope"] in SCOPES:
            s.scope = raw["scope"]
        else:
            errors.append(f"settings.scope: must be one of {', '.join(SCOPES)}")
```

In `public_json`, after `"runs_dir": str(cfg.settings.runs_dir),`:

```python
            "scope": cfg.settings.scope,
```

Create `herdr_review/scope.py`:

````python
"""What the change under review is: the branch's commits, or the whole working tree."""
from __future__ import annotations

from pathlib import Path

from . import PROMPTS_DIR
from .config import SCOPES
from .gitutil import UntrackedFile
from .render import render_file

# How many entries a reviewer prompt inlines before it points at the full list.
UNCOMMITTED_INLINE = 50
UNTRACKED_INLINE = 100


class ScopeError(Exception):
    """The requested scope holds nothing to review."""


def resolve_scope(requested: str, committed: bool, uncommitted: bool, base: str, merge_base: str) -> str:
    """`commits` or `worktree`. <committed>: the branch has committed changes against the merge-base;
    <uncommitted>: the working tree differs from HEAD."""
    if requested not in SCOPES:
        raise ScopeError(f"unknown scope '{requested}' (allowed: {', '.join(SCOPES)})")
    where = f"{base} ({merge_base[:12]})"
    if not committed and not uncommitted:
        raise ScopeError(f"nothing to review: the working tree equals {where}")
    if requested == "commits" and not committed:
        raise ScopeError(f"nothing committed on this branch since {where}; pass --scope worktree to review the uncommitted work")
    if requested == "auto":
        return "commits" if committed else "worktree"
    return requested


def uncommitted_counts(lines: list[str]) -> tuple[int, int]:
    """(changed tracked files, untracked entries) in `git status --short` lines."""
    untracked = sum(1 for line in lines if line.startswith("??"))
    return len(lines) - untracked, untracked


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def uncommitted_block(lines: list[str], listing: Path) -> str:
    """The user's uncommitted files as the commits-scope reviewer prompt shows them."""
    if not lines:
        return "There are none: the working tree was clean when the review started."
    shown = lines[:UNCOMMITTED_INLINE]
    text = "```\n" + "\n".join(shown) + "\n```"
    if len(lines) > len(shown):
        text += f"\n\n…and {len(lines) - len(shown)} more: {listing} lists them all."
    return text


def untracked_block(files: list[UntrackedFile]) -> str:
    """The untracked files of the change as the worktree-scope reviewer prompt shows them."""
    if not files:
        return "There are none."
    lines = []
    for f in files[:UNTRACKED_INLINE]:
        line = f"- `{f.path}` ({human_size(f.size)})"
        if f.skip:
            line += f" — skip: {f.skip}"
        lines.append(line)
    if len(files) > UNTRACKED_INLINE:
        lines.append(f"- …and {len(files) - UNTRACKED_INLINE} more: `git ls-files --others --exclude-standard` lists them all.")
    return "\n".join(lines)


def reviewer_steps(scope: str, merge_base: str, uncommitted: list[str], untracked: list[UntrackedFile], listing: Path) -> str:
    """Steps 1-2 of the reviewer prompt for <scope>."""
    if scope == "commits":
        return render_file(PROMPTS_DIR / "scope-commits.md", {
            "MERGE_BASE": merge_base, "UNCOMMITTED": uncommitted_block(uncommitted, listing),
        }).strip()
    return render_file(PROMPTS_DIR / "scope-worktree.md", {
        "MERGE_BASE": merge_base, "UNTRACKED": untracked_block(untracked),
    }).strip()
````

Create `prompts/scope-commits.md`:

```markdown
1. Run `git diff {MERGE_BASE} HEAD --`. That diff is the change under review: the commits of this branch since the base, and nothing else.
2. The working tree also holds uncommitted work that is not part of the change — the user's own files, listed below as `git status --short` showed them when the review started. An entry ending in `/` covers everything under that directory. Do not review these files and do not mention them in the review. Where a file of the change also has uncommitted edits, read its committed version with `git show HEAD:<path>`.

{UNCOMMITTED}
```

Create `prompts/scope-worktree.md`:

```markdown
1. Run `git diff {MERGE_BASE} --`. It shows every change to tracked files on this branch against the base, committed and uncommitted.
2. New files of the change are untracked, so no diff shows them: read the files listed below along with the diff. A file marked `skip` is binary, a symlink, or too large to read: do not read it, and mention it in the review only if it matters. Leave every file untracked: do not `git add` anything (see the Hard Rules).

{UNTRACKED}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_config tests.unit.test_prompts tests.unit.test_scope -v`
Expected: all pass.

Run: `tests/run.sh`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/config.py herdr_review/scope.py prompts/scope-commits.md prompts/scope-worktree.md tests/unit/test_config.py tests/unit/test_prompts.py tests/unit/test_scope.py
git commit -m "feat: the scope policy and its reviewer prompt fragments" -m "settings.scope (auto, commits, worktree) and herdr_review/scope.py:
auto takes the branch's commits when there are any, else the working
tree. Two prompt fragments give the reviewer its first two steps: the
commits with the user's uncommitted files listed as outside the change,
or the working tree with the untracked files to read and to skip."
```

---

### Task 3: startup dialogs — answer the trust dialogs, never the MCP one

**Files:**
- Modify: `herdr_review/dialogs.py` (rewrite)
- Modify: `herdr_review/launch.py` (import, `_profile_spec`, the orchestrator start)
- Modify: `herdr_review/runner.py` (import, `_start_agent`)
- Modify: `tests/unit/fakeherdr.py` (`screens_after_wait`)
- Test: `tests/unit/test_dialogs.py` (rewrite), `tests/unit/test_runner_start.py`, `tests/unit/test_launch.py`, `tests/bats/launch.bats`
- Docs: `skills/review/SKILL.md` §6, `README.md`, `config.example.yaml`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `dialogs.CLAUDE_MCP_SETTINGS: str`, `dialogs.MCP_REFUSAL: str`
  - `@dataclass(frozen=True) class DialogOutcome: resolved: bool; refusal: str | None = None`
  - `startup_args(kind: str, args: list[str]) -> list[str]`
  - `recognize(screen: str) -> tuple[str, tuple[str, ...] | None] | None` — dialog names `claude-mcp`, `claude-trust`, `codex-trust`, `grok-trust`
  - `resolve_startup_dialog(herdr, name: str) -> DialogOutcome` (replaces `try_resolve_startup_dialog`)
  - `FakeHerdr.screens_after_wait: dict[str, list[str]]` — the screen each `agent_wait` call switches to

- [ ] **Step 1: Let the fake herdr change screens on `agent_wait`**

In `tests/unit/fakeherdr.py`, in `__init__` after `self.screens: dict[str, str] = {}` add:

```python
        self.screens_after_wait: dict[str, list[str]] = {}
```

Replace `agent_wait`:

```python
    def agent_wait(self, name, until=None, timeout_ms=None):
        self.calls.append(("agent_wait", name, until, timeout_ms))
        queued = self.screens_after_wait.get(name)
        if queued:
            self.screens[name] = queued.pop(0)
        elif self.wait_ok and not self.keep_screen_on_wait:
            self.screens[name] = "idle\n"
        if not self.wait_ok:
            return HerdrResult(False, 1, error_code="timeout", message="wait timed out")
        return HerdrResult(True, 0, result={"type": "agent_info", "agent": {"name": name, "agent_status": until or "idle"}})
```

- [ ] **Step 2: Write the failing tests**

Replace `tests/unit/test_dialogs.py` with:

```python
import unittest

from herdr_review.dialogs import CLAUDE_MCP_SETTINGS, MCP_REFUSAL, DialogOutcome, recognize, resolve_startup_dialog, startup_args
from tests.unit.fakeherdr import FakeHerdr

CLAUDE_TRUST_ON_NO = "Quick safety check … ❯ No, exit\n  Yes, I trust this folder\nEnter to confirm · Esc to cancel\n"
CLAUDE_TRUST_ON_YES = "Quick safety check … ❯ Yes, I trust this folder\n  No, exit\n"
MCP_ONE = (
    "New MCP server found in this project: dummy\n"
    "  Use this MCP server\n  Use this and all future MCP servers in this project\n"
    "❯ Continue without using this MCP server\nEnter to confirm · Esc to cancel\n"
)
MCP_MANY = (
    "2 new MCP servers found in this project\nSelect any you wish to enable.\n"
    "❯ [✔] one\n  [✔] two\n  Enable selected\nSpace to select · Esc to reject all\n"
)
CODEX_TRUST_ON_TRUST = "Trust this folder? Codex can read, edit, and run files here.\n› 1. Trust and continue\n  2. Quit\n  enter continue · esc quit\n"
CODEX_TRUST_ON_QUIT = "Trust this folder? Codex can read, edit, and run files here.\n  1. Trust and continue\n› 2. Quit\n"
GROK_TRUST = "Do you trust the contents of this directory?\n  Yes, proceed   y\n  No, quit   n\n"


class StartupDialogTest(unittest.TestCase):
    def test_trust_dialog_is_confirmed(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_NO
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=True))
        self.assertIn(("agent_send_keys", "hr1-orch", ("down", "enter")), h.calls)
        self.assertIn(("agent_wait", "hr1-orch", "idle", 30000), h.calls)

    def test_cursor_on_yes_sends_enter_only(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        self.assertTrue(resolve_startup_dialog(h, "hr1-orch").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-orch", ("enter",))])

    def test_unknown_dialog_is_left_alone(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Please log in to continue\n"
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_trust_dialog_without_cursor_is_left_to_orchestrator(self):
        h = FakeHerdr()
        h.screens["hr1-orch"] = "Yes, I trust this folder\nNo, exit\n"
        self.assertFalse(resolve_startup_dialog(h, "hr1-orch").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_wait_failure_is_not_resolved(self):
        h = FakeHerdr()
        h.wait_ok = False
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_YES
        self.assertEqual(resolve_startup_dialog(h, "hr1-orch"), DialogOutcome(resolved=False))
        self.assertEqual(len(h.calls_named("agent_send_keys")), 3)       # MAX_DIALOGS tries, then it gives up

    def test_dialog_still_on_screen_after_wait_is_not_resolved(self):
        h = FakeHerdr()
        h.keep_screen_on_wait = True
        h.screens["hr1-orch"] = CLAUDE_TRUST_ON_NO
        self.assertFalse(resolve_startup_dialog(h, "hr1-orch").resolved)

    def test_mcp_dialog_is_refused_without_a_key(self):
        for screen in (MCP_ONE, MCP_MANY):
            with self.subTest(screen=screen.splitlines()[0]):
                h = FakeHerdr()
                h.screens["hr1-rv"] = screen
                self.assertEqual(resolve_startup_dialog(h, "hr1-rv"), DialogOutcome(resolved=False, refusal=MCP_REFUSAL))
                self.assertEqual(h.calls_named("agent_send_keys"), [])

    def test_mcp_dialog_after_the_trust_dialog_is_refused(self):
        h = FakeHerdr()
        h.wait_ok = False                                   # an agent held by a dialog never turns idle
        h.screens["hr1-rv"] = CLAUDE_TRUST_ON_NO
        h.screens_after_wait["hr1-rv"] = [MCP_MANY]
        self.assertEqual(resolve_startup_dialog(h, "hr1-rv").refusal, MCP_REFUSAL)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-rv", ("down", "enter"))])

    def test_codex_trust_dialog(self):
        for screen, keys in ((CODEX_TRUST_ON_TRUST, ("enter",)), (CODEX_TRUST_ON_QUIT, ("up", "enter"))):
            with self.subTest(keys=keys):
                h = FakeHerdr()
                h.screens["hr1-codex"] = screen
                self.assertTrue(resolve_startup_dialog(h, "hr1-codex").resolved)
                self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-codex", keys)])

    def test_grok_trust_dialog(self):
        h = FakeHerdr()
        h.screens["hr1-grok"] = GROK_TRUST
        self.assertTrue(resolve_startup_dialog(h, "hr1-grok").resolved)
        self.assertEqual(h.calls_named("agent_send_keys"), [("agent_send_keys", "hr1-grok", ("y",))])


class RecognizeTest(unittest.TestCase):
    def test_known_dialogs(self):
        self.assertEqual(recognize(MCP_ONE), ("claude-mcp", None))
        self.assertEqual(recognize(MCP_MANY), ("claude-mcp", None))
        self.assertEqual(recognize(CLAUDE_TRUST_ON_NO), ("claude-trust", ("down", "enter")))
        self.assertEqual(recognize(CODEX_TRUST_ON_TRUST), ("codex-trust", ("enter",)))
        self.assertEqual(recognize(GROK_TRUST), ("grok-trust", ("y",)))

    def test_an_idle_agent_is_not_a_dialog(self):
        self.assertIsNone(recognize("❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"))
        self.assertIsNone(recognize("› Ask Codex to do anything\n"))


class StartupArgsTest(unittest.TestCase):
    def test_claude_starts_with_the_session_mcp_setting(self):
        args = ["--model", "opus"]
        self.assertEqual(startup_args("claude", args), ["--settings", CLAUDE_MCP_SETTINGS, "--model", "opus"])
        self.assertEqual(args, ["--model", "opus"])          # the profile's own list stays as it was

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        for args in (["--settings", "/x.json"], ["--model", "opus", "--settings=/x.json"]):
            with self.subTest(args=args):
                self.assertEqual(startup_args("claude", args), args)

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(startup_args("codex", ["-m", "gpt-5.5"]), ["-m", "gpt-5.5"])
        self.assertEqual(startup_args("grok", []), [])


if __name__ == "__main__":
    unittest.main()
```

Append to class `StartReviewersTest` in `tests/unit/test_runner_start.py`:

```python
    def test_mcp_dialog_fails_the_reviewer_with_the_reason(self):
        run_dir = make_run(self.root, self.repo, reviewers=("claude-opus",))
        self.herdr.start_errors["hrtest-claude-opus"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-claude-opus"] = "2 new MCP servers found in this project\n❯ [✔] one\n  [✔] two\n"
        self.herdr.pane_screens["w1:p2"] = "2 new MCP servers found in this project\n"
        out = self.runner(run_dir).start_reviewers()
        a = out["agents"]["hrtest-claude-opus"]
        self.assertEqual(a["state"], "failed")
        self.assertIn("enableAllProjectMcpServers", a["reason"])
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        self.assertEqual(self.herdr.calls_named("agent_prompt"), [])
        status = json.loads((run_dir / "status.json").read_text())
        self.assertIn("MCP servers found", status["agents"]["hrtest-claude-opus"]["last_screen"])
        self.assertIn(("tab_rename", "w1:t2", "rv-hrtest: claude-opus ✗"), self.herdr.calls)

    def test_codex_trust_dialog_resolved_then_prompted(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        self.herdr.start_errors["hrtest-codex"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-codex"] = "Trust this folder?\n› 1. Trust and continue\n  2. Quit\n"
        out = self.runner(run_dir).start_reviewers()
        self.assertEqual(out["agents"]["hrtest-codex"]["state"], "working")
        self.assertIn(("agent_send_keys", "hrtest-codex", ("enter",)), self.herdr.calls)
```

In `tests/unit/test_launch.py`, add the import:

```python
from herdr_review.dialogs import CLAUDE_MCP_SETTINGS
```

In `test_happy_path_creates_run_and_starts_orchestrator`, replace:

```python
        self.assertEqual(self.herdr.calls_named("agent_start")[0], ("agent_start", "hrtest-orch", "claude", "w1:p2", ["--model", "opus"]))
```

with:

```python
        self.assertEqual(self.herdr.calls_named("agent_start")[0], ("agent_start", "hrtest-orch", "claude", "w1:p2", ["--settings", CLAUDE_MCP_SETTINGS, "--model", "opus"]))
```

Append to class `LaunchTest`:

```python
    def test_claude_profiles_start_with_the_session_mcp_setting(self):
        res = self.do_launch()
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual(run_json["reviewers"][0]["args"], ["--settings", CLAUDE_MCP_SETTINGS, "--model", "opus"])
        self.assertEqual(run_json["reviewers"][1]["args"], ["-m", "gpt-5.5"])            # codex: untouched
        self.assertEqual(run_json["orchestrator"]["args"][:2], ["--settings", CLAUDE_MCP_SETTINGS])

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        self.cfg.profiles["claude-opus"].args = ["--model", "opus", "--settings=/home/me/claude.json"]
        res = self.do_launch()
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual(run_json["orchestrator"]["args"], ["--model", "opus", "--settings=/home/me/claude.json"])

    def test_mcp_dialog_at_the_orchestrator_aborts_the_launch_with_the_reason(self):
        self.herdr.start_errors["hrtest-orch"] = ("agent_not_ready", "blocked during startup")
        self.herdr.screens["hrtest-orch"] = "New MCP server found in this project: dummy\n❯ Continue without using this MCP server\n"
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch()
        message = str(ctx.exception)
        self.assertIn("enableAllProjectMcpServers", message)
        self.assertIn("Tab w1:t2 is left open", message)
        self.assertEqual(self.herdr.calls_named("agent_send_keys"), [])
        run_dir = next((self.root / "runs").glob("*/*-hrtest"))
        self.assertEqual(json.loads((run_dir / "status.json").read_text())["phase"], "aborted")
```

In `tests/bats/launch.bats`, in the test `launch: happy path creates the run and starts the orchestrator`, replace:

```bash
  grep -q 'agent start hr.*-orch --kind claude --pane w1:p2 --timeout 300000 -- --model opus' "$FAKE_HERDR_LOG"
```

with:

```bash
  grep -q 'agent start hr.*-orch --kind claude --pane w1:p2' "$FAKE_HERDR_LOG"
  grep -qF -- '--timeout 300000 -- --settings {"enableAllProjectMcpServers": true} --model opus' "$FAKE_HERDR_LOG"
```

Append a test to `tests/bats/launch.bats`:

```bash
@test "launch: the MCP approval dialog is never answered" {
  echo '{"agent_start": {"*-orch": {"code": "agent_not_ready", "message": "blocked"}}, "screens": {"*-orch": "2 new MCP servers found in this project\n❯ [✔] one\n  [✔] two\n", "w1:p2": "2 new MCP servers found in this project\n"}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"enableAllProjectMcpServers"* ]]
  [[ "$output" == *"w1:t2"* ]]
  ! grep -q 'agent send-keys' "$FAKE_HERDR_LOG"
}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_dialogs tests.unit.test_runner_start tests.unit.test_launch -v`
Expected: `test_dialogs` errors with `ImportError: cannot import name 'CLAUDE_MCP_SETTINGS'`; the new runner and launch tests fail.

- [ ] **Step 4: Implement `dialogs.py`**

Replace `herdr_review/dialogs.py` with:

```python
"""Agent startup dialogs: the ones the runner answers without an LLM, and the one it never answers."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. The same setting given on the
# command line makes the dialog unnecessary and is never written back.
CLAUDE_MCP_SETTINGS = '{"enableAllProjectMcpServers": true}'
MCP_DIALOG = re.compile(r"new MCP servers? found in this project", re.IGNORECASE)
MCP_REFUSAL = (
    "Claude Code asks to approve this project's MCP servers (.mcp.json); herdr-review never answers "
    "that dialog: every answer, Esc included, is saved into .claude/settings.local.json of the "
    "repository. It appears only when the profile passes its own --settings: add "
    '"enableAllProjectMcpServers": true there.'
)
# Claude Code: "❯ No, exit" / "Yes, I trust this folder" — the cursor starts on "No, exit".
CLAUDE_CURSOR = "❯"
CLAUDE_TRUST = re.compile(r"yes,\s*i trust", re.IGNORECASE)
CLAUDE_NO = re.compile(r"no,\s*exit", re.IGNORECASE)
# Codex: "› 1. Trust and continue" / "2. Quit".
CODEX_CURSOR = "›"
CODEX_TRUST = re.compile(r"trust and continue", re.IGNORECASE)
CODEX_NO = re.compile(r"\bquit\b", re.IGNORECASE)
# Grok: "Do you trust the contents of this directory?" — the key next to "Yes, proceed" is y.
GROK_TRUST = re.compile(r"do you trust the contents of this directory", re.IGNORECASE)
MAX_DIALOGS = 3          # Claude Code shows the trust dialog first and the MCP dialog after it
WAIT_MS = 30000
SCREEN_LINES = 60


@dataclass(frozen=True)
class DialogOutcome:
    resolved: bool
    refusal: str | None = None      # a dialog the runner recognised and must never answer


def startup_args(kind: str, args: list[str]) -> list[str]:
    """The args an agent of <kind> starts with. A claude agent gets the session-only MCP setting unless
    its profile passes a --settings of its own: the last --settings wins, so one of the two would be lost."""
    if kind == "claude" and not any(a == "--settings" or a.startswith("--settings=") for a in args):
        return ["--settings", CLAUDE_MCP_SETTINGS, *args]
    return list(args)


def _cursor_keys(screen: str, cursor: str, yes: re.Pattern, no: re.Pattern, back: str) -> tuple[str, ...] | None:
    """Keys that pick the <yes> option from where the cursor is, or None for an unknown layout."""
    for raw in screen.splitlines():
        if cursor not in raw:
            continue
        if yes.search(raw):
            return ("enter",)
        if no.search(raw):
            return (back, "enter")
        return None
    return None


def recognize(screen: str) -> tuple[str, tuple[str, ...] | None] | None:
    """(dialog, keys) for a known startup dialog, None for any other screen. The keys are None for the
    dialog the runner never answers and for a known dialog whose cursor layout it does not know."""
    if MCP_DIALOG.search(screen):
        return "claude-mcp", None
    if CLAUDE_TRUST.search(screen):
        return "claude-trust", _cursor_keys(screen, CLAUDE_CURSOR, CLAUDE_TRUST, CLAUDE_NO, "down")
    if CODEX_TRUST.search(screen):
        return "codex-trust", _cursor_keys(screen, CODEX_CURSOR, CODEX_TRUST, CODEX_NO, "up")
    if GROK_TRUST.search(screen):
        return "grok-trust", ("y",)
    return None


def resolve_startup_dialog(herdr, name: str) -> DialogOutcome:
    """Answer the trust dialogs of Claude Code, Codex and Grok; refuse Claude Code's MCP approval dialog."""
    for _ in range(MAX_DIALOGS):
        found = recognize(herdr.agent_read(name, source="visible", lines=SCREEN_LINES) or "")
        if found is None:
            return DialogOutcome(resolved=False)
        dialog, keys = found
        if dialog == "claude-mcp":
            return DialogOutcome(resolved=False, refusal=MCP_REFUSAL)
        if not keys or not herdr.agent_send_keys(name, *keys).ok:
            return DialogOutcome(resolved=False)
        if herdr.agent_wait(name, until="idle", timeout_ms=WAIT_MS).ok:
            if recognize(herdr.agent_read(name, source="visible", lines=SCREEN_LINES) or "") is None:
                return DialogOutcome(resolved=True)
        # A dialog is still on screen — the next one, or the same one again: go round.
    return DialogOutcome(resolved=False)
```

- [ ] **Step 5: Use it in the runner**

In `herdr_review/runner.py`, replace `from .dialogs import try_resolve_startup_dialog` with:

```python
from .dialogs import resolve_startup_dialog
```

Replace the body of `_start_agent`:

```python
    def _start_agent(self, name: str, spec: dict, pane: str) -> None:
        r = self.herdr.agent_start(name, spec["kind"], pane, spec["args"])
        if r.ok:
            self._set_state(name, "idle")
            return
        if r.error_code == "agent_not_ready":
            outcome = resolve_startup_dialog(self.herdr, name)
            if outcome.resolved:
                self.log(f"{name}: startup dialog resolved automatically")
                self._set_state(name, "idle")
            elif outcome.refusal:
                self.log(f"{name}: startup dialog refused: {outcome.refusal}")
                self._set_state(name, "failed", reason=outcome.refusal, last_screen=self._pane_screen(pane))
            else:
                self._set_state(name, "blocked-start", reason=r.message)
            return
        self._set_state(name, "failed", reason=f"{r.error_code}: {r.message}", last_screen=self._pane_screen(pane))
```

- [ ] **Step 6: Use it in launch**

In `herdr_review/launch.py`, replace `from .dialogs import try_resolve_startup_dialog` with:

```python
from .dialogs import resolve_startup_dialog, startup_args
```

Replace `_profile_spec`:

```python
def _profile_spec(cfg: Config, profile: str, name: str) -> dict:
    p = cfg.profiles[profile]
    return {"name": name, "profile": profile, "kind": p.kind, "args": startup_args(p.kind, p.args), "env_keys": sorted(p.env)}
```

Replace:

```python
        r = herdr.agent_start(orch["name"], orch["kind"], pane_id, orch["args"])
        if not r.ok and r.error_code == "agent_not_ready" and try_resolve_startup_dialog(herdr, orch["name"]):
            log("orchestrator startup dialog resolved automatically")
            r = HerdrResult(True, 0)
```

with:

```python
        r = herdr.agent_start(orch["name"], orch["kind"], pane_id, orch["args"])
        if not r.ok and r.error_code == "agent_not_ready":
            outcome = resolve_startup_dialog(herdr, orch["name"])
            if outcome.resolved:
                log("orchestrator startup dialog resolved automatically")
                r = HerdrResult(True, 0)
            elif outcome.refusal:
                raise LaunchError(
                    f"orchestrator '{orch_profile}' stopped at a startup dialog: {outcome.refusal}\n"
                    f"Tab {tab_id} is left open for inspection.\n"
                    f"Run directory: {run_dir}"
                )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_dialogs tests.unit.test_runner_start tests.unit.test_launch -v`
Expected: all pass.

Run: `bats tests/bats/launch.bats`
Expected: all pass, including `launch: the MCP approval dialog is never answered`.

- [ ] **Step 8: Document it**

In `skills/review/SKILL.md` §6, replace the bullet:

```markdown
- «failed to start … Tab … is left open»: `herdr agent read <name> --source visible --lines 60` shows why. A dialog → resolve it with `herdr agent send-keys <name> …`, then send the prompt from the message. Anything else → report and stop.
```

with:

```markdown
- «failed to start … Tab … is left open»: the message says why. The runner answers the trust dialogs of Claude Code, Codex and Grok by itself and never answers Claude Code's MCP approval dialog — the message then says what to change in the profile. For any other screen (a login, a dialog you do not recognise) run `herdr agent read <name> --source visible --lines 60`, show the error and that screen verbatim, and stop: never press keys in that tab and never ask the user which answer to give.
```

In `README.md`, replace the `args` row of the rules table:

```markdown
| `args` | Passed verbatim; the plugin adds nothing, so the permission flags go here (the mode table above). Copied as they are into `run.json` and `runner.log`: never a secret. |
```

with:

```markdown
| `args` | Passed verbatim, with one addition: a profile of kind `claude` without a `--settings` of its own starts with `--settings '{"enableAllProjectMcpServers": true}'` (see MCP servers below). The permission flags go here (the mode table above). Copied as they are into `run.json` and `runner.log`: never a secret. |
```

In `README.md`, after the line `Check the result: `herdr-review profiles`.` insert a blank line and:

```markdown
**MCP servers.** Claude Code asks at startup to approve the servers of a project's `.mcp.json` that you have not decided on, and saves every answer — Esc included — into the repository's `.claude/settings.local.json`. So that no run stops at that dialog or changes your MCP settings, every profile of kind `claude` starts with `--settings '{"enableAllProjectMcpServers": true}'`: the agents of a run get the project's servers, your user servers and the claude.ai connectors, a server you disabled explicitly stays disabled, and the setting lives on the command line only. Two consequences: an MCP server that the branch under review adds to `.mcp.json` starts in every claude agent of the run without approval; and a profile that passes its own `--settings` gets nothing added — put `"enableAllProjectMcpServers": true` into that settings file, or its agents stop at the dialog and leave the run with that reason. Codex and Grok load a project's servers without asking; the runner answers their trust dialogs, as it answers Claude Code's.
```

In `README.md` Troubleshooting, replace:

```markdown
- `orchestrator failed to start … Tab … is left open` — open that tab; a login or dialog is waiting. Resolve it and run the printed `herdr agent prompt …`.
```

with:

```markdown
- `orchestrator failed to start … Tab … is left open` — the message says why. The runner answers the trust dialogs of Claude Code, Codex and Grok; it never answers Claude Code's MCP approval dialog, and the message then says what to add to the profile's own `--settings`. Anything else — a login, an unknown dialog — waits in that tab: resolve it there and run the printed `herdr agent prompt …`.
```

and after the bullet `- A reviewer shows ` ✗` — `herdr-review status` gives the reason and `status.json` the last screen.` insert:

```markdown
- A reviewer or the fixer shows ` ✗` with "Claude Code asks to approve this project's MCP servers" — its profile passes its own `--settings`; add `"enableAllProjectMcpServers": true` to that file (see MCP servers in Configure).
```

In `config.example.yaml`, after the line `# and that agent leaves the run as failed.` insert:

```yaml
#
# Every profile of kind claude starts with --settings '{"enableAllProjectMcpServers": true}': Claude
# Code then loads the project's .mcp.json servers without its approval dialog, whose every answer
# would be saved into the repository's .claude/settings.local.json. A profile that passes its own
# --settings gets nothing added and must carry that key in its settings file.
```

- [ ] **Step 9: Run everything**

Run: `tests/run.sh`
Expected: all unit and bats tests pass.

- [ ] **Step 10: Commit**

```bash
git add herdr_review/dialogs.py herdr_review/launch.py herdr_review/runner.py tests/unit/fakeherdr.py tests/unit/test_dialogs.py tests/unit/test_runner_start.py tests/unit/test_launch.py tests/bats/launch.bats skills/review/SKILL.md README.md config.example.yaml
git commit -m "feat: answer the Codex and Grok trust dialogs, never the MCP one" -m "Claude Code saves every answer to its MCP approval dialog, Esc included,
into the repository's .claude/settings.local.json. claude agents now
start with a session-only --settings '{\"enableAllProjectMcpServers\":
true}' unless the profile passes its own --settings, so the dialog does
not appear and every MCP server stays available. If it appears anyway,
the runner fails the agent with the reason and launch aborts: no key is
ever sent to it. The runner also answers the trust dialogs of Codex and
Grok, as it answers Claude Code's."
```

---

### Task 4: launch reviews the branch's commits and leaves uncommitted work alone

**Files:**
- Modify: `herdr_review/launch.py`, `herdr_review/cli.py`, `herdr_review/gitutil.py` (remove `has_changes`)
- Modify: `prompts/reviewer.md`
- Test: `tests/unit/test_launch.py`, `tests/unit/test_prompts.py`, `tests/unit/test_cli.py`, `tests/unit/test_gitutil.py`, `tests/bats/launch.bats`
- Docs: `skills/review/SKILL.md` §3, §5, §6; `README.md`; `config.example.yaml`

**Interfaces:**
- Consumes: Task 1 (`head_commit`, `committed_changes`, `status_lines`, `untracked_files`), Task 2 (`ScopeError`, `resolve_scope`, `reviewer_steps`, `uncommitted_counts`, `SCOPES`), Task 3 (the launch.py it edits).
- Produces:
  - `LaunchOptions.scope: str | None = None`
  - `run.json` keys `head`, `scope`, `uncommitted`; files `<run_dir>/uncommitted.txt`, `<run_dir>/scratch/<profile>/`
  - launch result keys `scope`, `uncommitted`, `untracked` (`{"files": int, "skipped": int}` in `worktree`, else `None`)
  - `reviewer.md` placeholders `SCOPE_STEPS`, `SCRATCH_DIR`
  - `cli.scope_lines(result: dict) -> list[str]`; `launch --scope {auto,commits,worktree}`
  - Local variables in `launch()` that Task 6 uses: `head`, `scope`, `uncommitted`, `listing`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_launch.py`, replace `test_uncommitted_changes_are_a_warning` with:

```python
    def test_a_dirty_tree_stays_outside_a_review_of_the_commits(self):
        (self.repo / "a.txt").write_text("dirty\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "todo.md").write_text("mine\n")
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertEqual(res["scope"], "commits")
        self.assertEqual(res["uncommitted"], [" M a.txt", "?? notes/"])
        self.assertIsNone(res["untracked"])
        self.assertFalse(any("uncommitted" in w for w in res["warnings"]))
        self.assertEqual((run_dir / "uncommitted.txt").read_text(), " M a.txt\n?? notes/\n")
        run_json = json.loads((run_dir / "run.json").read_text())
        self.assertEqual((run_json["scope"], run_json["uncommitted"]), ("commits", [" M a.txt", "?? notes/"]))
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(run_json["head"], head)
        prompt = (run_dir / "prompts" / "codex.md").read_text()
        self.assertIn(f"git diff {run_json['merge_base']} HEAD --", prompt)
        self.assertIn("?? notes/", prompt)
        self.assertNotIn("git ls-files --others", prompt)

    def test_nothing_committed_reviews_the_working_tree(self):
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "switch", "-q", "-c", "wip")
        (self.repo / "new.py").write_text("print(1)\n")
        (self.repo / "blob.bin").write_bytes(b"\0\1\2")
        res = self.do_launch()
        self.assertEqual(res["scope"], "worktree")
        self.assertEqual(res["untracked"], {"files": 2, "skipped": 1})
        prompt = (Path(res["run_dir"]) / "prompts" / "codex.md").read_text()
        self.assertIn("- `new.py` (9 B)", prompt)
        self.assertIn("- `blob.bin` (3 B) — skip: binary", prompt)

    def test_the_scope_flag_overrides_the_setting(self):
        (self.repo / "new.py").write_text("x\n")
        self.cfg.settings.scope = "worktree"
        self.assertEqual(self.do_launch()["scope"], "worktree")
        res = launch(LaunchOptions(scope="commits"), self.cfg, FakeHerdr(), ENV, self.repo, self.runner, which=which_ok, run_id="hrcommits")
        self.assertEqual(res["scope"], "commits")

    def test_commits_scope_without_commits_is_refused(self):
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "switch", "-q", "-c", "wip")
        (self.repo / "new.py").write_text("x\n")
        with self.assertRaises(LaunchError) as ctx:
            self.do_launch(scope="commits")
        self.assertIn("nothing committed on this branch since master", str(ctx.exception))
        self.assertFalse((self.root / "runs").exists())          # refused before any run directory

    def test_every_reviewer_gets_a_scratch_directory(self):
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertEqual(sorted(p.name for p in (run_dir / "scratch").iterdir()), ["claude-opus", "codex"])
        self.assertIn(str(run_dir / "scratch" / "codex"), (run_dir / "prompts" / "codex.md").read_text())

    def test_a_long_list_of_uncommitted_files_is_capped_in_the_prompt_and_complete_on_disk(self):
        for i in range(60):
            (self.repo / f"junk{i:02}.txt").write_text("x\n")
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertEqual(len((run_dir / "uncommitted.txt").read_text().splitlines()), 60)
        prompt = (run_dir / "prompts" / "codex.md").read_text()
        self.assertIn("?? junk49.txt", prompt)
        self.assertNotIn("?? junk50.txt", prompt)
        self.assertIn(f"…and 10 more: {run_dir / 'uncommitted.txt'} lists them all.", prompt)
```

In `tests/unit/test_prompts.py`, replace the `reviewer.md` entry of `EXPECTED`:

```python
    "reviewer.md": {"DESCRIPTION", "PLAN_REFERENCE", "REPO", "BASE_REF", "MERGE_BASE", "RESULT_PATH", "REVIEWER", "SCOPE_STEPS", "SCRATCH_DIR"},
```

and replace `test_reviewer_prompt_has_required_headings_and_rules` with:

```python
    def test_reviewer_prompt_has_required_headings_and_rules(self):
        values = {k: "v" for k in EXPECTED["reviewer.md"]}
        values["SCOPE_STEPS"] = "1. step one\n2. step two"
        values["SCRATCH_DIR"] = "/run/scratch/codex"
        text = render_file(PROMPTS_DIR / "reviewer.md", values)
        for heading in ("### Strengths", "### Critical Issues", "### Important Issues", "### Minor Issues", "### Assessment"):
            self.assertIn(heading, text)
        self.assertIn("1. step one\n2. step two\n\n3. Read the modified files", text)
        self.assertIn("goes under `/run/scratch/codex`", text)
        self.assertIn("not into /tmp", text)
        self.assertIn("tracked or untracked", text)
        self.assertIn("DONE", text)
        self.assertIn("gitignored", text)
        self.assertNotIn("git ls-files --others", text)
```

In `tests/unit/test_cli.py`, change the imports to:

```python
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from herdr_review import __version__, gitutil
from herdr_review.cli import build_parser, main, resolve_status_run_dir, scope_lines, status_run_spec
```

(the remaining imports stay) and append to class `CliParsingTest`:

```python
    def test_launch_scope_flag(self):
        self.assertEqual(build_parser().parse_args(["launch", "--scope", "worktree"]).scope, "worktree")
        self.assertIsNone(build_parser().parse_args(["launch"]).scope)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["launch", "--scope", "everything"])

    def test_scope_lines(self):
        commits = {"scope": "commits", "base": "origin/master", "uncommitted": [" M a.txt", "?? notes/", "?? x"], "untracked": None}
        self.assertEqual(scope_lines(commits), [
            "  объём:        коммиты ветки (origin/master..HEAD)",
            "  вне ревью:    ваши незакоммиченные файлы (изменённых: 1, неотслеживаемых: 2); их никто не тронет",
        ])
        self.assertEqual(scope_lines({**commits, "uncommitted": []}), ["  объём:        коммиты ветки (origin/master..HEAD)"])
        worktree = {"scope": "worktree", "base": "master", "uncommitted": ["?? new.py"], "untracked": {"files": 3, "skipped": 1}}
        self.assertEqual(scope_lines(worktree), [
            "  объём:        рабочее дерево — коммиты и незакоммиченное; неотслеживаемых файлов у ревьюеров: 3, из них пропущено: 1",
        ])
```

In `tests/unit/test_gitutil.py`, replace `test_merge_base_and_changes` with:

```python
    def test_merge_base_and_current_branch(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        base = gitutil.merge_base(self.repo, "master")
        self.assertEqual(len(base), 40)
        (self.repo / "a.txt").write_text("two\n")
        git(self.repo, "commit", "-q", "-am", "change")
        self.assertEqual(gitutil.merge_base(self.repo, "master"), base)
        self.assertEqual(gitutil.current_branch(self.repo), "feat")
        with self.assertRaises(gitutil.GitError):
            gitutil.merge_base(self.repo, "no-such-branch")
```

and delete `test_has_changes_sees_untracked_files` and `test_has_changes_bad_sha_is_error` (Task 1's `committed_changes` and `status_lines` tests cover them).

Append to `tests/bats/launch.bats`:

```bash
@test "launch: a dirty tree is left out of a review of the commits" {
  echo edited > a.txt; mkdir notes; echo x > notes/one.md
  run "$HR" launch
  [ "$status" -eq 0 ]
  [[ "$output" == *"объём:        коммиты ветки (master..HEAD)"* ]]
  [[ "$output" == *"изменённых: 1, неотслеживаемых: 1"* ]]
  ! [[ "$output" == *"uncommitted changes"* ]]
  RUN="$(run_dir_of)"
  grep -qx ' M a.txt' "$RUN/uncommitted.txt"
  grep -qx '?? notes/' "$RUN/uncommitted.txt"
  grep -q 'git diff .* HEAD --' "$RUN/prompts/codex.md"
}

@test "launch: --scope worktree hands the reviewers the untracked files" {
  echo new > new.txt
  run "$HR" launch --json --scope worktree
  [ "$status" -eq 0 ]
  json_has "$output" 'd["scope"]=="worktree" and d["untracked"]=={"files": 1, "skipped": 0}'
  grep -qF -- '- `new.txt` (4 B)' "$(run_dir_of)/prompts/codex.md"
}

@test "launch: --scope commits refuses a branch with nothing committed" {
  git switch -q master; git switch -q -c empty; echo wip > b.txt
  run "$HR" launch --scope commits
  [ "$status" -eq 1 ]
  [[ "$output" == *"nothing committed on this branch since master"* ]]
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_launch tests.unit.test_prompts tests.unit.test_cli tests.unit.test_gitutil -v`
Expected: `test_cli` errors with `ImportError: cannot import name 'scope_lines'`; the new launch tests fail on `KeyError: 'scope'` or `TypeError` for `scope=`; the reviewer prompt test fails.

- [ ] **Step 3: Implement launch**

In `herdr_review/launch.py`, add the import:

```python
from .scope import ScopeError, resolve_scope, reviewer_steps
```

In `LaunchOptions`, after `plan: str | None = None`:

```python
    scope: str | None = None
```

Replace:

```python
    try:
        base = opts.base or gitutil.detect_base(repo)
        mb = gitutil.merge_base(repo, base)
    except gitutil.GitError as e:
        raise LaunchError(str(e)) from e
    if not gitutil.has_changes(repo, mb):
        raise LaunchError(f"nothing to review: the working tree equals {base} ({mb[:12]})")
    if gitutil.status_short(repo).strip():
        warnings.append("working tree has uncommitted changes; yolo reviewers share this tree and can modify them")
```

with:

```python
    try:
        base = opts.base or gitutil.detect_base(repo)
        mb = gitutil.merge_base(repo, base)
        head = gitutil.head_commit(repo)
        uncommitted = gitutil.status_lines(repo)
        scope = resolve_scope(opts.scope or cfg.settings.scope, gitutil.committed_changes(repo, mb), bool(uncommitted), base, mb)
        untracked = gitutil.untracked_files(repo) if scope == "worktree" else []
    except (gitutil.GitError, ScopeError) as e:
        raise LaunchError(str(e)) from e
```

Replace:

```python
    try:
        run_dir.mkdir(parents=True, mode=0o700)
        run_dir.chmod(0o700)
        (run_dir / "prompts").mkdir()
        (run_dir / "reviews").mkdir()
    except OSError as e:
        raise LaunchError(f"cannot create the run directory {run_dir}: {e}") from e
```

with:

```python
    listing = run_dir / "uncommitted.txt"
    try:
        run_dir.mkdir(parents=True, mode=0o700)
        run_dir.chmod(0o700)
        (run_dir / "prompts").mkdir()
        (run_dir / "reviews").mkdir()
        for pname in usable:
            (run_dir / "scratch" / pname).mkdir(parents=True)
        listing.write_text("".join(f"{line}\n" for line in uncommitted), encoding="utf-8")
    except OSError as e:
        raise LaunchError(f"cannot create the run directory {run_dir}: {e}") from e
```

In `run_json`, replace:

```python
        "base": base,
        "merge_base": mb,
        "description": description,
```

with:

```python
        "base": base,
        "merge_base": mb,
        "head": head,
        "scope": scope,
        "uncommitted": uncommitted,
        "description": description,
```

Replace the reviewer prompt loop:

```python
    for rv in reviewers_spec:
        text = render_file(PROMPTS_DIR / "reviewer.md", {
            "DESCRIPTION": description,
            "PLAN_REFERENCE": plan_ref,
            "REPO": str(repo),
            "BASE_REF": base,
            "MERGE_BASE": mb,
            "RESULT_PATH": str(run_dir / "reviews" / f"{rv['profile']}.md"),
            "REVIEWER": rv["profile"],
        })
        (run_dir / "prompts" / f"{rv['profile']}.md").write_text(text, encoding="utf-8")
```

with:

```python
    steps = reviewer_steps(scope, mb, uncommitted, untracked, listing)
    for rv in reviewers_spec:
        text = render_file(PROMPTS_DIR / "reviewer.md", {
            "DESCRIPTION": description,
            "PLAN_REFERENCE": plan_ref,
            "REPO": str(repo),
            "BASE_REF": base,
            "MERGE_BASE": mb,
            "RESULT_PATH": str(run_dir / "reviews" / f"{rv['profile']}.md"),
            "REVIEWER": rv["profile"],
            "SCOPE_STEPS": steps,
            "SCRATCH_DIR": str(run_dir / "scratch" / rv["profile"]),
        })
        (run_dir / "prompts" / f"{rv['profile']}.md").write_text(text, encoding="utf-8")
```

In the returned dict, replace:

```python
        "merge_base": mb,
        "autodecide": autodecide,
        "layout": layout,
        "warnings": warnings,
```

with:

```python
        "merge_base": mb,
        "scope": scope,
        "uncommitted": uncommitted,
        "untracked": {"files": len(untracked), "skipped": sum(1 for f in untracked if f.skip)} if scope == "worktree" else None,
        "autodecide": autodecide,
        "layout": layout,
        "warnings": warnings,
```

In `herdr_review/gitutil.py`, delete the function `has_changes`.

- [ ] **Step 4: Implement the reviewer prompt**

In `prompts/reviewer.md`, replace:

```markdown
## Your Task

1. Run `git diff {MERGE_BASE} --` in the repository. It shows every change to tracked files on this branch against the base — committed and uncommitted — but no untracked file appears in it.
2. Run `git ls-files --others --exclude-standard` and read every file it lists. Those are new files that are part of this change and are in no diff; review them alongside it. Leave them untracked: do not `git add` anything (see the Hard Rules).
3. Read the modified files for full context. Read the plan if one is referenced above.
```

with:

```markdown
## Your Task

{SCOPE_STEPS}

3. Read the modified files for full context. Read the plan if one is referenced above.
```

and replace the first bullet of `## Hard Rules`:

```markdown
- Do NOT modify, create, or delete any tracked file in the repository. Do NOT create untracked files except those gitignored. Do NOT run `git commit`, `git stash`, `git checkout`, `git reset`, or anything else that changes the tree or the index. Reading, `git diff`, `git log`, and running the project's tests are fine only if they do not write outside gitignored paths.
```

with:

```markdown
- Do NOT modify, move, or delete any file in the working tree, tracked or untracked, and do NOT create files in it. Reading, `git diff`, `git log`, and running the project's own tests are fine as long as they write only into gitignored paths. Do NOT run `git add`, `git commit`, `git stash`, `git checkout`, `git reset`, or anything else that changes the tree or the index.
- Anything you create yourself — scripts, test programs, a copy of the repository — goes under `{SCRATCH_DIR}` and nowhere else: not into the repository, not into /tmp.
```

- [ ] **Step 5: Implement the CLI**

In `herdr_review/cli.py`, replace `from .config import ConfigError, load_config, public_json` with:

```python
from .config import SCOPES, ConfigError, load_config, public_json
```

and add:

```python
from .scope import uncommitted_counts
```

In `build_parser`, after `p.add_argument("--plan", help="path to the plan / requirements document")` add:

```python
    p.add_argument("--scope", choices=SCOPES,
                   help="the change under review: auto (the branch's commits, else the working tree), commits, worktree")
```

Before `cmd_launch` add:

```python
def scope_lines(result: dict) -> list[str]:
    """The launch summary's lines about what the reviewers review."""
    if result["scope"] == "commits":
        lines = [f"  объём:        коммиты ветки ({result['base']}..HEAD)"]
        if result["uncommitted"]:
            changed, untracked = uncommitted_counts(result["uncommitted"])
            lines.append(f"  вне ревью:    ваши незакоммиченные файлы (изменённых: {changed}, неотслеживаемых: {untracked}); их никто не тронет")
        return lines
    line = "  объём:        рабочее дерево — коммиты и незакоммиченное"
    untracked = result.get("untracked") or {}
    if untracked.get("files"):
        line += f"; неотслеживаемых файлов у ревьюеров: {untracked['files']}"
        if untracked.get("skipped"):
            line += f", из них пропущено: {untracked['skipped']}"
    return [line]
```

In `cmd_launch`, add `scope=args.scope` to the `LaunchOptions(...)` call:

```python
    opts = LaunchOptions(
        preset=args.preset, reviewers=reviewers, orchestrator=args.orchestrator, fixer=args.fixer, base=args.base,
        autodecide=args.autodecide, layout=args.layout, description=args.description, plan=args.plan, focus=args.focus,
        scope=args.scope,
    )
```

and after `print(f"  база:         {result['base']} ({result['merge_base'][:12]})")` add:

```python
    for line in scope_lines(result):
        print(line)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_launch tests.unit.test_prompts tests.unit.test_cli tests.unit.test_gitutil -v`
Expected: all pass.

Run: `bats tests/bats/launch.bats`
Expected: all pass (`nothing to review on the base branch` still passes through `ScopeError`).

- [ ] **Step 7: Document it**

In `skills/review/SKILL.md` §3, replace:

```markdown
Recognise, in any order: `default` or another preset name from `presets`; `BASE_BRANCH=<ref>`; `autodecide`; `layout=tabs|grid`; `reviewers=a,b,c`; `orchestrator=<profile>`; `fixer=<profile>`. Anything else is free text: use it as the description.
```

with:

```markdown
Recognise, in any order: `default` or another preset name from `presets`; `BASE_BRANCH=<ref>`; `autodecide`; `layout=tabs|grid`; `scope=commits|worktree`; `reviewers=a,b,c`; `orchestrator=<profile>`; `fixer=<profile>`. Anything else is free text: use it as the description.
```

Replace section 5:

```markdown
## 5. Description and plan

If you know from this session what was implemented, pass `--description "<one or two sentences>"`. If a plan or spec file for this work exists, pass `--plan <path>`. Do not invent either; omit what you do not know.
```

with:

```markdown
## 5. Description, plan and the working tree

If you know from this session what was implemented, pass `--description "<one or two sentences>"`. If a plan or spec exists for this work, pass it with `--plan`: a file path, or free text — `git show <sha>:<path>` when the plan lives only in git history, and the user's rulings from earlier review rounds (decisions not to re-raise). Do not invent either; omit what you do not know.

Never ask the user about untracked or uncommitted files, and never hide them (no `.git/info/exclude`, no stash, no commit). `launch` decides what the change is — the branch's commits, or the working tree when nothing is committed — keeps everything uncommitted out of a review of the commits, and says so in its summary. Pass `--scope worktree` only when the user asked to review uncommitted work.
```

In §6, replace:

```markdown
Compose the flags from the answers: `--preset <name>`, or `--reviewers a,b,c --orchestrator x --fixer y`; `--base` only when `BASE_BRANCH=` was given; `--autodecide` only when chosen, `--no-autodecide` when the user said no; `--layout` when given; `--plan` when a plan file is known.

Exit 0 → show the summary as printed (run dir, orchestrator agent, `herdr agent focus <name>`, `herdr-review status latest`) and end your turn.
```

with:

```markdown
Compose the flags from the answers: `--preset <name>`, or `--reviewers a,b,c --orchestrator x --fixer y`; `--base` only when `BASE_BRANCH=` was given; `--scope` only when `scope=` was given or the user asked to review uncommitted work; `--autodecide` only when chosen, `--no-autodecide` when the user said no; `--layout` when given; `--plan` when a plan is known.

Exit 0 → show the summary as printed (run dir, orchestrator agent, scope, `herdr agent focus <name>`, `herdr-review status latest`) and end your turn.
```

In `README.md`, in the `settings:` block of Configure, after the `autodecide:` line insert:

```yaml
  scope: auto                    # auto: the branch's commits, else the working tree | commits | worktree
```

Replace:

```markdown
Everything starts from a herdr pane: an agent session with the skills, or a shell with `herdr-review` in PATH. Reviewers work on the live working tree, uncommitted files included; `launch` warns when the tree is dirty.
```

with:

```markdown
Everything starts from a herdr pane: an agent session with the skills, or a shell with `herdr-review` in PATH.

The change under review is the branch's commits since the base (`git diff <merge-base> HEAD`). Your uncommitted edits and untracked files stay out of it, and nothing in the run touches them: `launch` lists them in the run directory as `uncommitted.txt` and says so in its summary. When nothing is committed yet, the whole working tree is the change. `scope=worktree` (`--scope worktree`, or `settings.scope`) reviews the working tree with its uncommitted work even when there are commits.
```

In the arguments table, after the row `| `layout=tabs` / `layout=grid` | One tab per agent, or panes inside the orchestrator's tab. |` insert:

```markdown
| `scope=worktree` / `scope=commits` | Review the working tree with its uncommitted work, or only the branch's commits. Default: the commits, else the working tree. |
```

Replace:

```markdown
With a preset or `reviewers=` the skill asks nothing. Without either it asks four questions: reviewers, orchestrator, fixer, autodecide. It also passes a plan file when it knows one from the session.
```

with:

```markdown
With a preset or `reviewers=` the skill asks nothing. Without either it asks four questions: reviewers, orchestrator, fixer, autodecide. It never asks about uncommitted files or startup dialogs. It also passes the plan when it knows one from the session: a file path, or free text — `git show <sha>:<path>` when the plan lives only in git history, and your rulings from earlier review rounds.
```

In the CLI block, after the line `herdr-review launch --reviewers codex,grok --orchestrator claude-opus --fixer claude-opus --base develop --autodecide --layout grid` insert:

```bash
herdr-review launch --preset default --scope worktree   # uncommitted work is part of the change
```

In Troubleshooting, after the `no runs for this repository` bullet insert:

```markdown
- A file you changed is not in the review — it is uncommitted, and the review covers the branch's commits. Commit it, or launch with `scope=worktree`.
```

In `config.example.yaml`, after the line `  autodecide: false              # true = the orchestrator decides disputed issues itself` insert:

```yaml
  scope: auto                    # auto = the branch's commits, else the working tree | commits | worktree
```

- [ ] **Step 8: Run everything**

Run: `tests/run.sh`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add herdr_review/launch.py herdr_review/cli.py herdr_review/gitutil.py prompts/reviewer.md tests/unit/test_launch.py tests/unit/test_prompts.py tests/unit/test_cli.py tests/unit/test_gitutil.py tests/bats/launch.bats skills/review/SKILL.md README.md config.example.yaml
git commit -m "feat: launch reviews the branch's commits and leaves uncommitted work alone" -m "launch resolves the scope before it creates the run directory: the
branch's commits when there are any, else the working tree, or what
--scope / settings.scope asks for. It records HEAD, the scope and the
uncommitted files in run.json and uncommitted.txt, gives every reviewer
a scratch directory, and renders the reviewer's first two steps for the
scope. The summary says what is reviewed and what stays out; the skill
never asks about the working tree."
```

---

### Task 5: the fixer protects uncommitted files and writes commits in the repository's style

**Files:**
- Modify: `prompts/fixer-auto.md`, `prompts/fixer-decision.md`
- Test: `tests/unit/test_prompts.py`
- Docs: `README.md`

**Interfaces:**
- Consumes: `<run_dir>/uncommitted.txt` (Task 4).
- Produces: the report vocabulary Task 6 relies on — `done`, `applied, not committed: <reason>`, `skipped: <reason>`, `skipped: <path> holds the user's uncommitted work; left to the user`, a commit hash or `no commit`; message files `fix-auto-commit.txt`, `fix-<n>-commit.txt`; the `Problem:` marker in the decision skeleton.

- [ ] **Step 1: Write the failing test**

Append to class `PromptTemplatesTest` in `tests/unit/test_prompts.py`:

```python
    def test_fixer_skeletons_protect_the_users_files_and_follow_the_repository_style(self):
        for name, message in (("fixer-auto.md", "/run/fix-auto-commit.txt"), ("fixer-decision.md", "/run/fix-<ORCHESTRATOR: n>-commit.txt")):
            with self.subTest(name=name):
                text = render_file(PROMPTS_DIR / name, {"RUN_DIR": "/run"})
                self.assertIn("/run/uncommitted.txt", text)
                self.assertIn("has no copy in git", text)
                self.assertIn("applied, not committed", text)
                self.assertIn("holds the user's uncommitted work; left to the user", text)
                self.assertIn(f"git commit --only -F {message} --", text)
                self.assertIn("git log -n 20", text)
                self.assertIn("Do not mention the review", text)
                self.assertNotIn("review: auto-fix", text)
                self.assertNotIn('-m "', text)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.unit.test_prompts -v`
Expected: `test_fixer_skeletons_protect_the_users_files_and_follow_the_repository_style` fails on the first assertion.

- [ ] **Step 3: Rewrite the skeletons**

Replace `prompts/fixer-auto.md` with:

```markdown
# Fix task: apply the reviewed AUTO fixes

Repository: the current working directory. Run directory: {RUN_DIR}

## Fixes to apply

<ORCHESTRATOR: one entry per AUTO issue, in this shape>
- **path/to/file.ext:123** — what is wrong — the exact change to make

## Files a reviewer already changed

<ORCHESTRATOR: the paths from `drift_status`, one per line, or "none">

## Uncommitted before the review

{RUN_DIR}/uncommitted.txt lists what `git status --short` showed when the review started: the user's uncommitted edits and untracked files. An entry ending in `/` covers everything under that directory. They are the user's own work.

## Rules

1. Apply exactly the fixes listed above and nothing else: no refactoring beyond them, no style changes elsewhere.
2. If the project has tests relevant to the changed code, run them. Fix a failure only if your change caused it.
3. Never delete, move, rename, or rewrite a file that no fix above names — in particular nothing listed in {RUN_DIR}/uncommitted.txt.
4. Never delete, move, or rename a path listed in {RUN_DIR}/uncommitted.txt, even when a fix asks for it: an untracked file has no copy in git, and a tracked one would lose the user's uncommitted edits. Report such a fix as `skipped: <path> holds the user's uncommitted work; left to the user`.
5. Never stage or commit a path listed in {RUN_DIR}/uncommitted.txt or under "Files a reviewer already changed". When a fix falls into such a file, apply it, leave the file uncommitted, and report the fix as `applied, not committed: the user's uncommitted edits` or `applied, not committed: changed by a reviewer`.
6. Write the commit message in this repository's style. Read `git log -n 20 --format='%s%n%n%b'`: follow its subject convention (for example `fix(scope): …`), and if its commits carry bodies, add one that says why, one line per fix. Do not mention the review, the reviewers, or herdr-review. Write the message to {RUN_DIR}/fix-auto-commit.txt.
7. Stage only the files you changed that rule 5 lets you commit (`git add <file> …`), then commit exactly those paths:
   `git commit --only -F {RUN_DIR}/fix-auto-commit.txt -- <file> …`
   `--only` commits exactly the named paths and disregards anything staged for other paths, so work the user had already staged never enters the commit.
   Do not push.
8. Write a report to {RUN_DIR}/fix-auto-report.md: one line per fix — `done`, `applied, not committed: <reason>`, or `skipped: <reason>` — and the commit hash, or `no commit` when nothing was committed.
9. Reply with the single word DONE when the report is written.
```

Replace `prompts/fixer-decision.md` with:

```markdown
# Fix task: apply one review decision

Repository: the current working directory. Run directory: {RUN_DIR}

Issue: <ORCHESTRATOR: number and title>
Location: <ORCHESTRATOR: path/to/file.ext:123>
Problem: <ORCHESTRATOR: what is wrong, in one or two sentences>
Decision: <ORCHESTRATOR: the chosen variant, described precisely enough to implement without judgment calls>

## Files a reviewer already changed

<ORCHESTRATOR: the paths from `drift_status`, one per line, or "none">

## Uncommitted before the review

{RUN_DIR}/uncommitted.txt lists what `git status --short` showed when the review started: the user's uncommitted edits and untracked files. An entry ending in `/` covers everything under that directory. They are the user's own work.

## Rules

1. Implement exactly the decision above and nothing else.
2. If the project has tests relevant to the changed code, run them. Fix a failure only if your change caused it.
3. Never delete, move, rename, or rewrite a file that the decision does not name — in particular nothing listed in {RUN_DIR}/uncommitted.txt.
4. Never delete, move, or rename a path listed in {RUN_DIR}/uncommitted.txt, even when the decision asks for it: an untracked file has no copy in git, and a tracked one would lose the user's uncommitted edits. Report it as `skipped: <path> holds the user's uncommitted work; left to the user`.
5. Never stage or commit a path listed in {RUN_DIR}/uncommitted.txt or under "Files a reviewer already changed". When the decision falls into such a file, apply it, leave the file uncommitted, and report `applied, not committed: the user's uncommitted edits` or `applied, not committed: changed by a reviewer`.
6. Write the commit message in this repository's style. Read `git log -n 20 --format='%s%n%n%b'`: follow its subject convention (for example `fix(scope): …`), and if its commits carry bodies, add one that says what was wrong and why the change fixes it. Do not mention the review, the reviewers, or herdr-review. Write the message to {RUN_DIR}/fix-<ORCHESTRATOR: n>-commit.txt.
7. Stage only the files you changed that rule 5 lets you commit (`git add <file> …`), then commit exactly those paths:
   `git commit --only -F {RUN_DIR}/fix-<ORCHESTRATOR: n>-commit.txt -- <file> …`
   `--only` commits exactly the named paths and disregards anything staged for other paths, so work the user had already staged never enters the commit.
   Do not push.
8. Write a report to {RUN_DIR}/fix-<ORCHESTRATOR: n>-report.md: what you changed; `done`, `applied, not committed: <reason>`, or `skipped: <reason>`; and the commit hash, or `no commit` when nothing was committed.
9. Reply with the single word DONE when the report is written.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_prompts -v`
Expected: all pass (`test_fixer_skeletons_mention_report_and_done` still finds `/run/`, `DONE`, `Do not push`).

- [ ] **Step 5: Document it**

In `README.md` "During the run", replace:

```markdown
- **Fixes** land on your branch as commits `review: …` and `review(auto-decide): …`; the fixer commits only the files it changed, so your own uncommitted work in other files stays where it was.
```

with:

```markdown
- **Fixes** land on your branch as commits written in your repository's own style: its subject convention, and a body when your history has them. The fixer commits only the files it changed and never deletes, moves or commits your uncommitted files: a fix inside one of them is applied and left uncommitted, and the report says so.
```

- [ ] **Step 6: Run everything and commit**

Run: `tests/run.sh`
Expected: all pass.

```bash
git add prompts/fixer-auto.md prompts/fixer-decision.md tests/unit/test_prompts.py README.md
git commit -m "feat: the fixer protects uncommitted files and writes commits in the repository's style" -m "Both fixer tasks point to uncommitted.txt: the fixer never deletes,
moves or renames those paths, even when a fix asks for it, and never
commits them; a fix inside one is applied and left uncommitted. Commit
messages follow the repository's own convention, with a body where the
history has bodies, and go through git commit -F from a file in the run
directory. No fixed review: subject and no mention of the review."
```

---

### Task 6: the orchestrator knows the scope, the user's files and the dialogs

**Files:**
- Modify: `prompts/orchestrator.md` (rewrite)
- Modify: `herdr_review/scope.py` (`orchestrator_scope`), `herdr_review/launch.py` (orchestrator values, reviewers table)
- Test: `tests/unit/test_scope.py`, `tests/unit/test_prompts.py`, `tests/unit/test_launch.py`

**Interfaces:**
- Consumes: Task 4 locals in `launch()` (`head`, `scope`, `uncommitted`), Task 5 report vocabulary, Task 3 dialog wording.
- Produces: `orchestrator_scope(scope: str, merge_base: str) -> str`; orchestrator placeholders `START_HEAD`, `SCOPE`, `UNCOMMITTED_COUNT`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_scope.py`, before the `if __name__` line:

```python
class OrchestratorScopeTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(scope.orchestrator_scope("commits", MB),
                         f"commits — the change is `git diff {MB} HEAD`; everything uncommitted is the user's own work and outside the change")
        self.assertEqual(scope.orchestrator_scope("worktree", MB),
                         f"worktree — the change is `git diff {MB}`, committed and uncommitted, plus the untracked files the reviewers were given")
```

In `tests/unit/test_prompts.py`, replace the `orchestrator.md` entry of `EXPECTED`:

```python
    "orchestrator.md": {
        "RUN_DIR", "RUNNER", "RUN_ID", "REPO", "BRANCH", "BASE_REF", "MERGE_BASE", "REVIEWERS", "ORCH_NAME",
        "FIXER_NAME", "FIXER_PROFILE", "AUTODECIDE", "LAYOUT", "CHECKIN_SEC", "DESCRIPTION", "PLAN_REFERENCE",
        "FIXER_AUTO_SKELETON", "FIXER_DECISION_SKELETON", "START_HEAD", "SCOPE", "UNCOMMITTED_COUNT",
    },
```

and append to class `PromptTemplatesTest`:

```python
    def test_orchestrator_prompt_names_the_dialogs_and_protects_the_users_files(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUNNER"] = "/opt/hr/bin/herdr-review"
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            "new MCP servers found in this project", "not even with Esc", "Trust and continue", "Grok — `y`",
            "/run/uncommitted.txt", "/run/scratch/<profile>/", "вне изменения: ваш незакоммиченный файл",
            "применено, не закоммичено", "log --oneline v..HEAD", "rev-parse HEAD",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("review(auto-decide)", text)
        self.assertNotIn("review: auto-fix", text)
```

In `tests/unit/test_launch.py`, append to `test_happy_path_creates_run_and_starts_orchestrator` (after the existing `orch = …` assertions):

```python
        self.assertIn("Scope: commits — the change is `git diff", orch)
        self.assertIn(f"HEAD at launch `{run_json['head']}`", orch)
        self.assertIn(f"scratch `{run_dir}/scratch/codex/`", orch)
        self.assertIn("(0 entries;", orch)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_scope tests.unit.test_prompts tests.unit.test_launch -v`
Expected: `OrchestratorScopeTest` errors with `AttributeError`; `test_placeholder_sets` fails for `orchestrator.md`; the new prompt and launch assertions fail.

- [ ] **Step 3: Implement**

Append to `herdr_review/scope.py`:

```python
def orchestrator_scope(scope: str, merge_base: str) -> str:
    """The scope line of the orchestrator's run facts."""
    if scope == "commits":
        return f"commits — the change is `git diff {merge_base} HEAD`; everything uncommitted is the user's own work and outside the change"
    return f"worktree — the change is `git diff {merge_base}`, committed and uncommitted, plus the untracked files the reviewers were given"
```

In `herdr_review/launch.py`, change the scope import to:

```python
from .scope import ScopeError, orchestrator_scope, resolve_scope, reviewer_steps
```

Replace `_reviewers_table`:

```python
def _reviewers_table(reviewers: list[dict], run_dir: Path) -> str:
    return "\n".join(
        f"  - `{rv['name']}` — profile `{rv['profile']}` ({rv['kind']}); prompt `{run_dir}/prompts/{rv['profile']}.md`; "
        f"result `{run_dir}/reviews/{rv['profile']}.md`; scratch `{run_dir}/scratch/{rv['profile']}/`"
        for rv in reviewers
    )
```

In the `orch_text = render_file(PROMPTS_DIR / "orchestrator.md", {…})` dict, replace:

```python
        "MERGE_BASE": mb,
        "REVIEWERS": _reviewers_table(reviewers_spec, run_dir),
```

with:

```python
        "MERGE_BASE": mb,
        "START_HEAD": head,
        "SCOPE": orchestrator_scope(scope, mb),
        "UNCOMMITTED_COUNT": len(uncommitted),
        "REVIEWERS": _reviewers_table(reviewers_spec, run_dir),
```

Replace `prompts/orchestrator.md` with:

````markdown
# herdr-review orchestrator — run {RUN_ID}

You are the orchestrator of a multi-agent code review. You run inside herdr; the reviewers, the fixer and the user sit in other tabs or panes of the same herdr session. The runner CLI does everything mechanical and keeps the run state; you make the decisions. Follow the phases in order and do not stop until Phase 6 is complete — the only exception is a runner call that exits with code 1 (see ground rule 5).

## Run facts

- Run directory: `{RUN_DIR}`
- Runner: `{RUNNER}` — every `run …` command below is `"{RUNNER}" run <subcommand> --run "{RUN_DIR}"`
- Repository: `{REPO}`, branch `{BRANCH}`, base `{BASE_REF}`, merge-base `{MERGE_BASE}`, HEAD at launch `{START_HEAD}`
- Scope: {SCOPE}
- Uncommitted before the review: `{RUN_DIR}/uncommitted.txt` lists what `git status --short` showed at launch — the user's uncommitted edits and untracked files ({UNCOMMITTED_COUNT} entries; an entry ending in `/` covers everything under that directory).
- What was implemented: {DESCRIPTION}
- Plan / requirements: {PLAN_REFERENCE}
- Layout: {LAYOUT}; check-in interval: {CHECKIN_SEC} s; initial autodecide: {AUTODECIDE}. The user may switch the run
  to automatic at any moment, so this is the value the run started with, not necessarily the current one: every
  `run …` command and `"{RUNNER}" status --run "{RUN_DIR}"` report the current value as `autodecide`.
- Your agent name: `{ORCH_NAME}`. Fixer agent name once started: `{FIXER_NAME}` (profile `{FIXER_PROFILE}`).
- Reviewers (agent name, profile, prompt file, result file, scratch directory):
{REVIEWERS}

## Ground rules for the whole run

1. You never edit files in the repository and never run git commands that change the tree or the index (no `commit`, `checkout`, `stash`, `reset`, `add`). Every fix goes through the fixer agent. Nothing in this run deletes, moves or rewrites the user's uncommitted files listed in `{RUN_DIR}/uncommitted.txt`.
2. You never review the code yourself. In Phase 3 you only verify what the reviewers reported by reading the code at the reported locations.
3. You never close tabs or panes. `run finish` does that according to the config.
4. Every `run …` command prints one JSON object. Read it; never guess an agent's state. `"{RUNNER}" status --run "{RUN_DIR}"` shows the whole picture at any time.
5. A runner command that exits with code 1 could not work at all (herdr down, run directory broken). Write what happened into `{RUN_DIR}/report.md`, print it, and stop.
6. If the user writes to you in this pane while you work, answer briefly and return to the current phase.
7. Talk to other agents only through herdr: `herdr agent read <name> --source visible --lines 80`, `herdr agent send-keys <name> <key> …`, `herdr agent prompt <name> "<text>"`. Nothing else.
8. Prompts to agents are in English. The report and everything addressed to the user are in Russian.

## Phase 1 — start the reviewers

Run `"{RUNNER}" run start-reviewers --run "{RUN_DIR}"`. The JSON lists every reviewer with its state:

- `working` — the review prompt was accepted. Nothing to do.
- `failed` — it could not start; the reason and its last screen are in the JSON and in `status.json`. It is out of this run. A reviewer stopped at Claude Code's MCP approval dialog lands here too, with that reason: the runner never answers that dialog.
- `blocked-start` — the agent is alive but stuck on a startup dialog the runner could not answer (it answers the trust dialogs of Claude Code, Codex and Grok by itself). Handle it as described under "blocked" in Phase 2, then send it the review prompt: `"{RUNNER}" run prompt <name> --run "{RUN_DIR}"`.
- `prompt_stalled` — the prompt was submitted but the agent did not start working within 30 s. Do nothing now: the next `wait` / `collect` cycle re-prompts it once automatically.

## Phase 2 — wait, watch, collect

Loop until done:

1. `"{RUNNER}" run wait --run "{RUN_DIR}"`. It returns when a reviewer changes state (`reason: "state_change"`), when a reviewer is blocked (`"blocked"`), when every reviewer is settled (`"settled"`), or after {CHECKIN_SEC} s (`"checkin"`). Per agent it reports `state`, `since_sec`, `result_ok`, `screen_changed` (did the screen change since the previous `wait`?), `reason`.
2. `"{RUNNER}" run collect --run "{RUN_DIR}"` whenever any reviewer is `idle` or `done`. It validates the review files: `collected` means the file is complete. A reviewer that is idle without a valid file is re-prompted once by `collect` itself; on the second miss `collect` marks it `failed`. `collect` also reports `pending` (still to wait for) and `failed` (with reasons).
3. Act on the other states:
   - `blocked` / `blocked-start` → `herdr agent read <name> --source visible --lines 80` and decide:
     * Claude Code's MCP approval dialog ("New MCP server found in this project" or "<N> new MCP servers found in this project") → never answer it, not even with Esc: every answer is saved into the repository's `.claude/settings.local.json`. `"{RUNNER}" run fail <name> --reason "MCP approval dialog" --run "{RUN_DIR}"`;
     * a trust dialog → confirm it. The runner answers these by itself, so one that reaches you has a layout it did not expect: Claude Code — pick "Yes, I trust this folder" (`enter` when the cursor is on it, `down` then `enter` when the cursor is on "No, exit"); Codex — "Trust and continue" (`enter` when the cursor is on it); Grok — `y`;
     * a permission or approval dialog → decide by who asks and what, and answer with the keys the dialog shows (a yes/no prompt: `y` then `enter`; "Enter to confirm": `enter`):
       - a reviewer: a read, a command that only reads, the project's own tests or build, or a write under its scratch directory `{RUN_DIR}/scratch/<profile>/` → confirm; a write into the repository or anywhere else → refuse with the dialog's own "no" option, then `herdr agent prompt <name> "Do not write into the repository or outside your scratch directory. Put experiments under {RUN_DIR}/scratch/<profile>/."`;
       - the fixer: changing repository files and committing them is its job → confirm; deleting, moving or renaming a path from `{RUN_DIR}/uncommitted.txt` → refuse;
     * a question about the task (which base? which files? may I read X?) → answer in one message with `herdr agent prompt <name> "<answer>"`, using the run facts above and the reviewer's prompt file `{RUN_DIR}/prompts/<profile>.md`;
     * a login prompt, quota or API error, or a dialog you do not understand → `"{RUNNER}" run fail <name> --reason "<what you saw>" --run "{RUN_DIR}"`. Never confirm something you do not understand.
     After a `blocked-start` dialog is resolved, send the review prompt: `"{RUNNER}" run prompt <name> --run "{RUN_DIR}"`.
   - `working` with `screen_changed: false` on two consecutive waits → `herdr agent read <name> --source visible --lines 80`. A long tool call or a thinking indicator is fine: keep waiting. A visible loop, a crash trace, or an idle prompt line with nothing happening → `herdr agent send-keys <name> esc`, then `"{RUNNER}" run prompt <name> --retry --run "{RUN_DIR}"`. If the same agent gets stuck again → `run fail`.
   - `unknown` on two consecutive waits → `herdr agent read`. If the screen shows `DONE` or the review file exists → `run collect`. Otherwise treat it like the stuck `working` case.
   - `gone` → the agent process exited. It is out of the run (its last screen is in `status.json`). Nothing to do.
4. Stop looping when `wait` reports `settled: true` and `collect` reports no `pending`.

Then look at `drift` in the last `collect` output. `drift: true` means a reviewer changed the working tree; `drift_status` is `git status --short`.
- Current autodecide `true`: continue; mention the drift in the final report.
- Current autodecide `false`: `"{RUNNER}" run notify --title "herdr-review: нужен ответ" --body "ревьюер изменил рабочее дерево" --sound request --run "{RUN_DIR}"`, show `drift_status` to the user, ask whether to continue, and end your turn. Continue only after the user answers, then run `"{RUNNER}" run phase aggregating --run "{RUN_DIR}"`.

If `collect` reports zero `collected` reviewers, go to Phase 6 and write a report that says the review did not happen, with each reviewer's reason.

## Phase 3 — aggregate

`"{RUNNER}" run phase aggregating --run "{RUN_DIR}"`. Read every file in `{RUN_DIR}/reviews/`. Then:

1. **Deduplicate.** Two findings are one issue when they point at the same file and describe the same problem. Merge them into one entry that lists every reviewer that found it by profile name (`codex`, `claude-opus`, …). Two reviewers agreeing is corroboration; never collapse them into an anonymous entry.
2. **Verify each issue against the code.** Open the file at the reported location. Is the issue real? Is the severity right (Critical / Important / Minor)? Could the reviewer have misread the codebase?
3. **Classify** every issue into exactly one bucket:
   - **AUTO** — valid, and only one reasonable fix exists. Test: "would five competent engineers who know this codebase all make the same change?" Typical: missing error handling, wrong type, broken null check, dead code, typo, broken import, missing test for a new function, naming inconsistency.
   - **DISPUTED** — valid, but the fix involves trade-offs, several reasonable approaches, scope or architecture decisions. Test: "can I name two reasonable approaches, each with a real downside?"
   - **DISMISSED** — false positive: the reviewer misunderstood the codebase, the issue does not apply, or it is already handled elsewhere. Give one line of justification. In scope `commits`, a finding about a path from `{RUN_DIR}/uncommitted.txt` that no commit of the branch touches (`git -C "{REPO}" diff --stat {MERGE_BASE} HEAD` does not list it) is always DISMISSED with «вне изменения: ваш незакоммиченный файл» — whatever it asks, even to delete the file.
4. Write `{RUN_DIR}/issues.md`:

```
# Issues — run {RUN_ID}

| # | Issue | File:line | Severity | Found by | Class |
|---|-------|-----------|----------|----------|-------|
| 1 | one-line summary | `path:line` | Critical / Important / Minor | codex, claude-opus | AUTO / DISPUTED / DISMISSED |

Counts: AUTO = A, DISPUTED = D, DISMISSED = X

## AUTO
### 1. <title>
- Location: `path:line`
- Problem: …
- Fix: <the exact change>

## DISPUTED
### 2. <title>
- Location: `path:line`
- Problem: …
- Why more than one fix is reasonable: …

## DISMISSED
### 3. <title> — <one-line justification>
```

5. Print the table and the counts in this pane, in Russian: «Классификация: AUTO (исправлю автоматически): A, DISPUTED (обсудим по очереди): D, DISMISSED (ложные/неприменимые): X», with one line of justification per DISMISSED entry.

## Phase 4 — the fixer and the AUTO fixes

If A = 0 and D = 0 → go to Phase 6.

Otherwise `"{RUNNER}" run start-fixer --run "{RUN_DIR}"`. The JSON gives the fixer's state. `blocked-start` and `failed` are handled exactly like a reviewer in Phase 2, except that a resolved fixer needs no review prompt — it simply waits for its first task. A `failed` fixer ends the fixing: record it and go to Phase 6.

If A > 0:

1. Write `{RUN_DIR}/fix-auto.md` from this skeleton, replacing every `<ORCHESTRATOR: …>` marker: one entry per AUTO issue (location, problem, exact change), and under "Files a reviewer already changed" the paths from `drift_status` in the last `collect` output, one per line — `none` when there was no drift:

```
{FIXER_AUTO_SKELETON}
```

2. `"{RUNNER}" run prompt {FIXER_NAME} --file "{RUN_DIR}/fix-auto.md" --run "{RUN_DIR}"`
3. Loop `"{RUNNER}" run wait --agent {FIXER_NAME} --run "{RUN_DIR}"` with the Phase 2 rules (blocked → read and resolve; stuck → `esc` and `"{RUNNER}" run prompt {FIXER_NAME} --file "{RUN_DIR}/fix-auto.md" --retry --run "{RUN_DIR}"`). Stop when the fixer is `idle` or `done`.
4. Read `{RUN_DIR}/fix-auto-report.md`. It gives one line per fix — `done`, `applied, not committed: <reason>` or `skipped: <reason>` — and the commit hash, or `no commit`. Verify with `git -C "{REPO}" rev-parse HEAD` that the reported commit is HEAD, and with `git -C "{REPO}" show --stat --format='' <hash>` that it contains exactly the files of the fixes marked `done` and no path from `{RUN_DIR}/uncommitted.txt`. Do not expect a clean tree: the user's own uncommitted work legitimately stays in it. A fix marked `done` without a commit → `herdr agent prompt {FIXER_NAME} "Commit your changes now as described in the task file and reply DONE."` and wait again. `applied, not committed` → record «применено, не закоммичено: <причина>». `skipped` because the fix would delete, move or rename a file from `{RUN_DIR}/uncommitted.txt` → record «не применено: удаление или перенос вашего незакоммиченного файла оставлены вам». Any other `skipped` → judge it: one clarifying prompt if the reason is a misunderstanding, otherwise record the item as «не применено».
5. Note the commit hash for the report.

## Phase 5 — disputed issues

If D = 0 → Phase 6.

`"{RUNNER}" run phase disputed --run "{RUN_DIR}"`. Take the DISPUTED issues one at a time, in table order. For each one write, in this pane, a full analysis in Russian:

```
## [Спорное i/D] <title>
### Суть замечания
…
### Анализ
…
### Варианты решения
**Вариант A: …** Плюсы: … Минусы: …
**Вариант B: …** Плюсы: … Минусы: …
### Рекомендация
Вариант X, потому что …
```

Rules: every variant gets pros and cons; you recommend exactly one; never list variants neutrally; never present more than one disputed issue in a message. If only one variant is genuinely adequate, say so, decide, and apply it without asking — in both modes.

**If autodecide is true** (see the run facts above). After the recommendation add `### Проверка решения`: the strongest argument against your recommendation and why it still holds. If it does not hold, switch the variant. If you remain unsure, mark the decision «под вопросом» with one line saying what was missing. Then apply the decision.

**If autodecide is false** (see the run facts above). `"{RUNNER}" run notify --title "herdr-review: нужен ответ" --body "<i>/<D>: <title>" --sound request --run "{RUN_DIR}"`. The analysis is the last message of your turn: end the turn and wait for the user's answer in this pane. The user answers in free text: a variant letter, a variant of their own, «не исправлять», «стоп», or «авто». On «стоп», record this and every remaining disputed issue as deferred (with your recommendation) and go to Phase 6. On «авто» (also «дальше сам», «решай сам», or the same asked in English), run `"{RUNNER}" run autodecide --run "{RUN_DIR}"` and continue by the autodecide branch above **starting with the issue you just asked about**: the switch is one-way and covers the rest of the run, drift included. After any other answer run `"{RUNNER}" run phase disputed --run "{RUN_DIR}"` (it clears the waiting marker) and apply the decision.

The same switch can arrive without a question pending — the user has a command of their own that flips the mode and then writes to you here. Trust `autodecide` from the runner’s JSON over anything you remember.

**Apply a decision** («не исправлять» is only recorded):

1. Write `{RUN_DIR}/fix-<i>.md` from this skeleton, filling every `<ORCHESTRATOR: …>` marker; `n` is `<i>`. "Files a reviewer already changed" takes the paths from `drift_status` in the last `collect` output, one per line, or `none` when there was no drift. The fixer writes the commit message itself, in the repository's style, to `{RUN_DIR}/fix-<i>-commit.txt`, and its report to `{RUN_DIR}/fix-<i>-report.md`:

```
{FIXER_DECISION_SKELETON}
```

2. `"{RUNNER}" run prompt {FIXER_NAME} --file "{RUN_DIR}/fix-<i>.md" --run "{RUN_DIR}"`, then loop `"{RUNNER}" run wait --agent {FIXER_NAME} --run "{RUN_DIR}"` as in Phase 4.
3. Verify the commit as in Phase 4, note the hash, and move to the next issue.

## Phase 6 — report and finish

Write `{RUN_DIR}/report.md` in Russian:

- **Прогон:** run id, ветка, база, merge-base, объём ревью (коммиты ветки или рабочее дерево), дата, autodecide; если режим переключили посреди прогона — с какого замечания начался автоматический разбор.
- **Ревьюеры:** таблица профиль / kind / `ok` или `failed` + причина.
- **Замечания:** таблица из `issues.md` с итоговым статусом каждого: исправлено в `<hash>` / решено автоматически в `<hash>` (пометка «под вопросом», если была) / применено, не закоммичено: причина / отклонено: обоснование / отложено по «стоп»: рекомендация Вариант X / не применено: причина / не исправлять.
- **Итог:** авто-исправлено A; решено автоматически C (из них «под вопросом» — списком с тем, чего не хватило); обсуждено с пользователем B; отклонено X; отложено по «стоп» S (списком с рекомендацией).
- **Коммиты:** вывод `git -C "{REPO}" log --oneline {START_HEAD}..HEAD` — коммиты, сделанные за время прогона.
- **Drift**, если был: что изменилось и что вы сделали.
- Ревью не состоялось (ноль собранных отзывов): вместо таблиц — причины по каждому ревьюеру.

Then `"{RUNNER}" run finish --commits <hash1>,<hash2> --run "{RUN_DIR}"` (omit `--commits` when there are none). Print the report as your last message.

## Red flags — stop if you catch yourself doing this

| Doing this | Do this instead |
|---|---|
| Listing several disputed issues in one message | Take only the first; full analysis; then the next. |
| Variants without pros and cons, or without a recommendation | Add both; recommend exactly one. |
| Asking the user when only one variant works | Decide, say why, apply. |
| Editing a file or running `git commit` yourself | Write a fix file and prompt the fixer. |
| Reviewing the diff yourself in Phase 3 | Verify the reported issues only. |
| Guessing an agent's state | `run wait`, `run collect`, `herdr agent read`. |
| Confirming a dialog you do not understand | `run fail` that agent with the reason. |
| Answering Claude Code's MCP approval dialog, even with Esc | `run fail` that agent with the reason. |
| Sending the fixer a finding about the user's uncommitted file outside the change | DISMISSED: «вне изменения». |
| Confirming a reviewer's write into the repository or outside its scratch directory | Refuse; point it at `{RUN_DIR}/scratch/<profile>/`. |
| Ending the turn in autodecide mode to wait for the user | Decide, self-check, apply, continue. |
| Skipping `run phase` / `run finish` | The tabs' labels and the notification depend on them. |
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_scope tests.unit.test_prompts tests.unit.test_launch -v`
Expected: all pass, including `test_orchestrator_prompt_renders_and_names_every_subcommand` (no leftover `{`).

Run: `tests/run.sh`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add prompts/orchestrator.md herdr_review/scope.py herdr_review/launch.py tests/unit/test_scope.py tests/unit/test_prompts.py tests/unit/test_launch.py
git commit -m "feat: the orchestrator knows the scope, the user's files and the dialogs" -m "Run facts carry the scope, HEAD at launch and uncommitted.txt; each
reviewer's scratch directory is in the reviewers table. Phase 2 names
the MCP dialog (never answered) and each CLI's trust dialog, and judges
permission dialogs by who asks: a reviewer writes only into its scratch
directory. Phase 3 dismisses findings about the user's files outside
the change, Phases 4 and 5 verify fix commits by hash, and the report
lists the commits made during the run."
```

---

### Task 7: finish lists the run's own commits and removes scratch

**Files:**
- Modify: `herdr_review/runner.py`
- Test: `tests/unit/test_runner_collect.py`, `tests/bats/run.bats`
- Docs: `README.md`

**Interfaces:**
- Consumes: `run.json["head"]` and `<run_dir>/scratch/` (Task 4).
- Produces: `Runner._remove_scratch() -> None`; `_log_commits` counts from `head` (falls back to `merge_base`).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_runner_collect.py`, add `import subprocess` to the imports, and append to class `FinishTest`:

```python
    def test_finish_counts_only_the_commits_made_during_the_run(self):
        run_dir = self.reviewed_run(2)                      # two branch commits made before the launch
        run = json.loads((run_dir / "run.json").read_text())
        run["head"] = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        (run_dir / "run.json").write_text(json.dumps(run))
        (self.repo / "a.txt").write_text("fixed during the run\n")
        git(self.repo, "commit", "-q", "-am", "fix during the run")
        out = Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
        self.assertEqual(out["commits"], [gitutil.log_oneline(self.repo, "HEAD~1..HEAD").split()[0]])
        self.assertIn("коммитов 1", self.herdr.calls_named("notification_show")[-1][2])

    def test_finish_removes_the_scratch_directory(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        (run_dir / "scratch" / "codex" / "copy").mkdir(parents=True)
        (run_dir / "scratch" / "codex" / "copy" / "x.go").write_text("package main\n")
        Runner(run_dir, herdr=self.herdr, poll_sec=0, sleep=lambda s: None).finish([])
        self.assertFalse((run_dir / "scratch").exists())
        self.assertIn("finish: removed scratch/", (run_dir / "runner.log").read_text())
```

`test_finish_records_the_commits_git_reports` stays as it is: its run has no `head`, so it covers the fallback to the merge-base.

In `tests/bats/run.bats`, in `run: full pipeline through the fake herdr`, replace:

```bash
  HEAD_FULL="$(git -C "$REPO" rev-parse HEAD)"
  run "$HR" run finish --commits abc123,def456
```

with:

```bash
  echo fixed > "$REPO/a.txt"; git -C "$REPO" commit -q -am "fix during the run"
  HEAD_FULL="$(git -C "$REPO" rev-parse HEAD)"
  run "$HR" run finish --commits abc123,def456
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_collect -v`
Expected: the two new tests fail (three commits reported instead of one; `scratch/` still exists).

- [ ] **Step 3: Implement**

In `herdr_review/runner.py`, add `import shutil` to the imports (between `re` and `time`).

Replace `_log_commits`:

```python
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
            shutil.rmtree(scratch)
            self.log("finish: removed scratch/")
        except OSError as e:
            self.log(f"finish: cannot remove {scratch}: {e}")
```

In `finish`, replace the last two lines:

```python
        self.status.save()
        return {"phase": "finished", "commits": data["commits"], "closed": closed}
```

with:

```python
        self._remove_scratch()
        self.status.save()
        return {"phase": "finished", "commits": data["commits"], "closed": closed}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_runner_collect -v`
Expected: all pass.

Run: `bats tests/bats/run.bats`
Expected: all pass (the pipeline records the one commit made after `launch`).

- [ ] **Step 5: Document it**

In `README.md` "During the run", before the bullet that starts with `- **The end:**` insert:

```markdown
- **Experiments:** each reviewer keeps its own scripts and scratch copies under `scratch/<profile>/` in the run directory — never in your repository or `/tmp`. `run finish` deletes `scratch/`.
```

and replace:

```markdown
- **The end:** a done notification and `report.md` in the run directory, `~/.local/state/herdr-review/runs/<project>/<timestamp>-<run_id>/` — next to `status.json`, `reviews/<profile>.md`, `issues.md`, `fix-*.md` and `runner.log`. `latest` there points at the newest run of that repository.
```

with:

```markdown
- **The end:** a done notification and `report.md` in the run directory, `~/.local/state/herdr-review/runs/<project>/<timestamp>-<run_id>/` — next to `status.json`, `uncommitted.txt`, `reviews/<profile>.md`, `issues.md`, `fix-*.md` and `runner.log`. The report and `herdr-review status` list the commits made during the run. `latest` there points at the newest run of that repository.
```

- [ ] **Step 6: Run everything and commit**

Run: `tests/run.sh`
Expected: all pass.

```bash
git add herdr_review/runner.py tests/unit/test_runner_collect.py tests/bats/run.bats README.md
git commit -m "feat: finish lists the run's own commits and removes scratch" -m "run finish counts commits from the HEAD recorded at launch instead of the
merge-base, so status, the notification and the report show what the run
committed, not the whole branch. A run made before head existed keeps
the old range. finish also deletes the reviewers' scratch directory."
```

---

### Task 8: `herdr-review close`

**Files:**
- Modify: `herdr_review/runner.py`, `herdr_review/cli.py`, `tests/fake-herdr/herdr`
- Create test: `tests/unit/test_runner_close.py`
- Test: `tests/unit/test_cli.py`, `tests/bats/run.bats`
- Docs: `skills/review/SKILL.md` §7, `README.md`

**Interfaces:**
- Consumes: `finish` as left by Task 7.
- Produces:
  - `Runner._agent_targets() -> list[tuple[str, bool]]` — (id, is_tab)
  - `Runner._close_all(targets: list[tuple[str, bool]], who: str) -> tuple[list[str], list[str], dict[str, str]]` — (closed, already closed, failed)
  - `Runner.close(force: bool = False) -> dict` — `{"closed": [...], "already_closed": [...], "failed": {id: reason}}`
  - CLI `herdr-review close [DIR|latest] [--run …] [--force] [--json]`; exit 1 when a close failed
  - fake herdr scenario key `close`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_runner_close.py`:

```python
import json
import unittest

from herdr_review.runner import RunnerError
from herdr_review.status import RunStatus
from tests.unit.test_runner_start import RunnerBase, make_run


class CloseTest(RunnerBase):
    def finished_run(self, layout="tabs"):
        run_dir = make_run(self.root, self.repo, layout=layout, reviewers=("codex", "gemini"))
        r = self.runner(run_dir)
        r.start_reviewers()
        r.start_fixer()
        r.finish([])
        self.herdr.calls.clear()
        return run_dir

    def test_close_shuts_every_tab_of_a_finished_run(self):
        run_dir = self.finished_run()
        out = self.runner(run_dir).close()
        self.assertEqual(out, {"closed": ["w1:t2", "w1:t3", "w1:t4", "w1:t1"], "already_closed": [], "failed": {}})
        self.assertEqual([c[1] for c in self.herdr.calls_named("tab_close")], ["w1:t2", "w1:t3", "w1:t4", "w1:t1"])
        self.assertIn("closed_at", json.loads((run_dir / "status.json").read_text()))

    def test_grid_closes_the_orchestrator_tab_only(self):
        run_dir = self.finished_run(layout="grid")
        out = self.runner(run_dir).close()
        self.assertEqual(out["closed"], ["w1:t1"])
        self.assertEqual(self.herdr.calls_named("pane_close"), [])

    def test_a_run_in_progress_is_refused_unless_forced(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        r = self.runner(run_dir)
        r.start_reviewers()
        with self.assertRaises(RunnerError) as ctx:
            r.close()
        self.assertIn("still in phase reviewing", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))
        self.assertEqual(self.herdr.calls_named("tab_close"), [])
        self.assertEqual(r.close(force=True)["closed"], ["w1:t2", "w1:t1"])

    def test_a_tab_closed_by_hand_is_not_an_error(self):
        run_dir = self.finished_run()
        self.herdr.close_errors["w1:t3"] = ("tab_not_found", "tab w1:t3 not found")
        self.herdr.close_errors["w1:t4"] = ("server_error", "boom")
        out = self.runner(run_dir).close()
        self.assertEqual(out["closed"], ["w1:t2", "w1:t1"])
        self.assertEqual(out["already_closed"], ["w1:t3"])
        self.assertEqual(out["failed"], {"w1:t4": "server_error: boom"})

    def test_an_aborted_run_closes_the_orchestrator_tab_it_left_open(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",))
        RunStatus.load(run_dir).set_phase("aborted")
        self.assertEqual(self.runner(run_dir).close()["closed"], ["w1:t1"])


if __name__ == "__main__":
    unittest.main()
```

Append to class `CliParsingTest` in `tests/unit/test_cli.py`:

```python
    def test_close_parses(self):
        args = build_parser().parse_args(["close", "latest", "--force", "--json"])
        self.assertEqual((args.cmd, status_run_spec(args), args.force, args.json), ("close", "latest", True, True))
        args = build_parser().parse_args(["close"])
        self.assertEqual((status_run_spec(args), args.force), (None, False))
```

In `tests/fake-herdr/herdr`, change the docstring line

```
  FAKE_HERDR_SCENARIO  JSON file: server_down, agent_start, agent_prompt, agent_get, screens (see Task 14)
```

to

```
  FAKE_HERDR_SCENARIO  JSON file: server_down, agent_start, agent_prompt, agent_get, screens, close
```

and replace:

```python
    if head in (["agent", "send-keys"], ["tab", "rename"], ["pane", "rename"], ["tab", "close"], ["pane", "close"], ["tab", "focus"]):
        return ok({"type": "ok"})
```

with:

```python
    if head in (["tab", "close"], ["pane", "close"]):
        e = lookup(scenario.get("close"), argv[2])
        if e:
            return err(e["code"], e.get("message", e["code"]))
        return ok({"type": "ok"})
    if head in (["agent", "send-keys"], ["tab", "rename"], ["pane", "rename"], ["tab", "focus"]):
        return ok({"type": "ok"})
```

Append to `tests/bats/run.bats`:

```bash
@test "close: refuses a run in progress, then closes every tab once it has finished" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"
  run "$HR" run start-reviewers --run "$RUN"
  [ "$status" -eq 0 ]
  run "$HR" close
  [ "$status" -eq 1 ]
  [[ "$output" == *"still in phase reviewing"* ]]
  ! grep -q 'tab close' "$FAKE_HERDR_LOG"
  run "$HR" run finish --run "$RUN"
  [ "$status" -eq 0 ]
  run "$HR" close --json
  [ "$status" -eq 0 ]
  json_has "$output" 'd["closed"]==["w1:t3","w1:t4","w1:t2"] and d["failed"]=={}'
}

@test "close: a tab closed by hand is not an error; a failed close is" {
  run "$HR" launch --json
  RUN="$(run_dir_of)"
  run "$HR" run start-reviewers --run "$RUN"
  run "$HR" run finish --run "$RUN"
  echo '{"close": {"w1:t3": {"code": "tab_not_found", "message": "tab w1:t3 not found"}}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" close --json
  [ "$status" -eq 0 ]
  json_has "$output" 'd["already_closed"]==["w1:t3"] and "w1:t4" in d["closed"]'
  echo '{"close": {"w1:t4": {"code": "server_error", "message": "boom"}}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" close
  [ "$status" -eq 1 ]
  [[ "$output" == *"не удалось закрыть w1:t4: server_error: boom"* ]]
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_close tests.unit.test_cli -v`
Expected: `test_runner_close` errors with `AttributeError: 'Runner' object has no attribute 'close'`; `test_close_parses` fails with `SystemExit` (invalid choice `close`).

- [ ] **Step 3: Implement the runner**

In `herdr_review/runner.py`, in `finish`, replace the closing loop:

```python
        closed: list[str] = []
        if self.run.get("close_agents_on_finish"):
            for a in data["agents"].values():
                if self.layout == "tabs" and a.get("tab"):
                    ident = a["tab"]
                    r = self.herdr.tab_close(ident)
                    if r.ok:
                        closed.append(ident)
                    else:
                        self.log(f"finish: close {ident} failed: {r.error_code}: {r.message}")
                elif a.get("pane"):
                    ident = a["pane"]
                    r = self.herdr.pane_close(ident)
                    if r.ok:
                        closed.append(ident)
                    else:
                        self.log(f"finish: close {ident} failed: {r.error_code}: {r.message}")
```

with:

```python
        closed: list[str] = []
        if self.run.get("close_agents_on_finish"):
            closed, _, _ = self._close_all(self._agent_targets(), "finish")
```

After `finish`, add:

```python
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

    def _close_all(self, targets: list[tuple[str, bool]], who: str) -> tuple[list[str], list[str], dict[str, str]]:
        """Close each target: (closed, already closed, failed with the reason)."""
        closed: list[str] = []
        gone: list[str] = []
        failed: dict[str, str] = {}
        for ident, is_tab in targets:
            r = self.herdr.tab_close(ident) if is_tab else self.herdr.pane_close(ident)
            if r.ok:
                closed.append(ident)
            elif r.error_code and r.error_code.endswith("not_found"):
                gone.append(ident)
            else:
                failed[ident] = f"{r.error_code}: {r.message}"
                self.log(f"{who}: close {ident} failed: {r.error_code}: {r.message}")
        return closed, gone, failed

    def close(self, force: bool = False) -> dict:
        """Close every tab and pane the run opened. A run in progress is refused unless forced."""
        phase = self.status.data.get("phase")
        if phase not in ("finished", "aborted") and not force:
            raise RunnerError(f"run {self.run_id} is still in phase {phase}; closing its tabs stops its agents — pass --force")
        # In the grid layout every agent is a pane of the orchestrator's tab: closing that tab closes them all.
        targets = self._agent_targets() if self.layout == "tabs" else []
        orch = self.status.data.get("orchestrator") or {}
        if orch.get("tab"):
            targets.append((orch["tab"], True))
        closed, gone, failed = self._close_all(targets, "close")
        self.status.set("closed_at", now_iso())
        self.status.save()
        return {"closed": closed, "already_closed": gone, "failed": failed}
```

- [ ] **Step 4: Implement the CLI**

In `herdr_review/cli.py` `build_parser`, after the `status` parser block add:

```python
    p = sub.add_parser("close", help="close every tab and pane of a finished run")
    p.add_argument("run_pos", nargs="?", metavar="DIR", help="run directory or 'latest'")
    p.add_argument("--run", help="run directory or 'latest' (default: $HERDR_REVIEW_RUN, else latest)")
    p.add_argument("--force", action="store_true", help="close a run that is still in progress")
    p.add_argument("--json", action="store_true")
```

After `cmd_status` add:

```python
def cmd_close(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    run_dir = resolve_status_run_dir(status_run_spec(args), environ, Path.cwd())
    result = Runner(run_dir).close(force=args.force)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"закрыто: {', '.join(result['closed']) or 'ничего'}")
        if result["already_closed"]:
            print(f"уже закрыты: {', '.join(result['already_closed'])}")
        for ident, why in result["failed"].items():
            print(f"не удалось закрыть {ident}: {why}", file=sys.stderr)
    return 1 if result["failed"] else 0
```

In `dispatch`, after the `status` branch add:

```python
    if args.cmd == "close":
        return cmd_close(args, environ)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_runner_close tests.unit.test_cli tests.unit.test_runner_collect -v`
Expected: all pass, including `test_finish_does_not_report_failed_close_as_closed`.

Run: `bats tests/bats/run.bats`
Expected: all pass.

- [ ] **Step 6: Document it**

In `skills/review/SKILL.md` §7, append a paragraph:

```markdown
«Закрой вкладки ревью» → `"$HR" close --run latest`, relay the output. It refuses a run that is still in progress; pass `--force` only when the user asks for it.
```

In `README.md`, in the CLI block, after `herdr-review status <run dir>       # any run` insert:

```bash
herdr-review close                  # close every tab of the latest finished run
```

and in "During the run", append to the `**Tabs:**` bullet:

```markdown
 Once the run has finished, `herdr-review close` closes every tab it opened; `settings.close_agents_on_finish: true` closes the reviewers' and the fixer's by itself at the end.
```

(the bullet stays one line: the sentence is added at its end, after `waiting for you.`)

- [ ] **Step 7: Run everything and commit**

Run: `tests/run.sh`
Expected: all pass.

```bash
git add herdr_review/runner.py herdr_review/cli.py tests/fake-herdr/herdr tests/unit/test_runner_close.py tests/unit/test_cli.py tests/bats/run.bats skills/review/SKILL.md README.md
git commit -m "feat: herdr-review close" -m "Closes every tab and pane a run opened: the reviewers', the fixer's and
the orchestrator's tab, or just the orchestrator's tab in the grid
layout. It refuses a run in progress unless --force, counts a tab closed
by hand as already closed, and exits 1 when herdr could not close one.
finish shares the closing loop."
```

---

### Task 9: changelog, smoke checklist, final check

**Files:**
- Modify: `CHANGELOG.md`, `tests/SMOKE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: release notes and the real-herdr checklist.

- [ ] **Step 1: Update the changelog**

In `CHANGELOG.md`, replace:

```markdown
## [Unreleased]

### Changed
```

with:

```markdown
## [Unreleased]

### Added
- `settings.scope`, `launch --scope` and the skill argument `scope=`: the change under review is the
  branch's commits (`git diff <merge-base> HEAD`), or the working tree when nothing is committed. The
  owner's uncommitted edits and untracked files stay out of a review of the commits and are listed in
  `<run_dir>/uncommitted.txt`; the launch summary says so. In the working-tree scope the reviewers get
  the untracked files as a list, with binary and large ones marked to skip.
- `herdr-review close`: closes every tab and pane of a finished run; `--force` for one in progress.
- A scratch directory per reviewer, `<run_dir>/scratch/<profile>/`, for its experiments; `run finish`
  deletes it.
- The runner answers the trust dialogs of Codex and Grok, as it answers Claude Code's.

### Changed
- Claude Code's MCP approval dialog is never answered: every answer, Esc included, is saved into the
  repository's `.claude/settings.local.json`. Profiles of kind `claude` start with
  `--settings '{"enableAllProjectMcpServers": true}'` unless they pass their own `--settings`, so the
  dialog does not appear and the agents get every MCP server; an agent that still stops at it leaves
  the run with the reason.
- The fixer never deletes, moves, renames or commits the owner's uncommitted files; a fix inside one
  is applied and left uncommitted. The orchestrator dismisses findings about files outside the change.
- Fix commits follow the repository's own commit style instead of fixed `review:` subjects; the
  orchestrator verifies them by hash.
- `run finish`, `status` and the report list the commits made during the run, not every commit since
  the merge-base.
- The launcher never asks about untracked files or startup dialogs; `--plan` is documented as a path
  or free text.
```

(the two existing bullets under `### Changed` stay after the new ones.)

- [ ] **Step 2: Extend the smoke checklist**

Append to `tests/SMOKE.md`:

```markdown
8. Dirty tree: before launching, edit a tracked file and add an untracked file and an untracked directory unrelated to the change. The summary shows `вне ревью` with both counts, `uncommitted.txt` lists them, no review mentions them, and after the run they are unchanged and in no commit.
9. MCP: in a repository whose `.mcp.json` names a server nobody has approved yet, every claude agent starts without the MCP approval dialog, `/mcp` in a reviewer's tab lists the project's servers, and `.claude/settings.local.json` stays as it was.
10. `herdr-review close` refuses while the run is in progress and closes every tab of the run once it has finished.
```

- [ ] **Step 3: Run everything**

Run: `tests/run.sh`
Expected: every unit test and every bats test passes.

Run: `git status --short`
Expected: only `?? docs/2026-09-23-launcher-without-questions-prompt.md` besides the two files of this task — the owner's prompt file stays untracked.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md tests/SMOKE.md
git commit -m "docs: changelog and smoke checklist for the launcher without questions"
```
