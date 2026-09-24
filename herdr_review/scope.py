"""What the change under review is: the branch's commits, or the whole working tree."""
from __future__ import annotations

from pathlib import Path

from . import PROMPTS_DIR
from .config import SCOPES
from .gitutil import NESTED_REPO, UntrackedFile, quote_path
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


def untracked_line(f: UntrackedFile) -> str:
    """One untracked file as the reviewer reads it, in the prompt and in untracked.txt. A name that is not UTF-8
    or holds a control character is quoted as git quotes a path."""
    name = quote_path(f.path)
    # A nested repository is a directory: its size is no file size.
    line = f"- `{name}`" if f.skip == NESTED_REPO else f"- `{name}` ({human_size(f.size)})"
    if f.skip:
        line += f" — skip: {f.skip}"
    return line


def untracked_block(files: list[UntrackedFile], listing: Path) -> str:
    """The untracked files of the change as the worktree-scope reviewer prompt shows them."""
    if not files:
        return "There are none."
    lines = [untracked_line(f) for f in files[:UNTRACKED_INLINE]]
    if len(files) > UNTRACKED_INLINE:
        lines.append(f"- …and {len(files) - UNTRACKED_INLINE} more: `{listing}` lists them all with the same marks.")
    return "\n".join(lines)


def reviewer_steps(scope: str, merge_base: str, uncommitted: list[str], untracked: list[UntrackedFile], uncommitted_listing: Path, untracked_listing: Path) -> str:
    """Steps 1-2 of the reviewer prompt for <scope>."""
    if scope == "commits":
        return render_file(PROMPTS_DIR / "scope-commits.md", {
            "MERGE_BASE": merge_base, "UNCOMMITTED": uncommitted_block(uncommitted, uncommitted_listing),
        }).strip()
    return render_file(PROMPTS_DIR / "scope-worktree.md", {
        "MERGE_BASE": merge_base, "UNTRACKED": untracked_block(untracked, untracked_listing),
    }).strip()


def fixer_skeleton(kind: str, scope: str, run_dir: Path | str) -> str:
    """The fixer's task skeleton, `auto` or `decision`, with the commit rules of <scope>. In scope
    `worktree` the change is uncommitted work and the fixer commits nothing: the rule reaches it in the
    skeleton itself, not through the orchestrator's memory."""
    values = {"RUN_DIR": str(run_dir)}
    rules = f"fixer-commit-{kind}.md" if scope == "commits" else "fixer-commit-none.md"
    return render_file(PROMPTS_DIR / f"fixer-{kind}.md", {
        **values, "COMMIT_RULES": render_file(PROMPTS_DIR / rules, values).strip(),
    })


def orchestrator_scope(scope: str, merge_base: str) -> str:
    """The scope line of the orchestrator's run facts."""
    if scope == "commits":
        return f"commits — the change is `git diff {merge_base} HEAD`; everything uncommitted is the user's own work and outside the change"
    return f"worktree — the change is `git diff {merge_base}`, committed and uncommitted, plus the untracked files the reviewers were given"
