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
