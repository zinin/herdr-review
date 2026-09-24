# Fix task: apply the reviewed AUTO fixes

Repository: the current working directory. Run directory: {RUN_DIR}

## Fixes to apply

<ORCHESTRATOR: one entry per AUTO issue, in this shape>
- **path/to/file.ext:123** — what is wrong — the exact change to make

## Files changed during the review

<ORCHESTRATOR: the paths from `drift_status`, one per line, or "none">

## Uncommitted before the review

{RUN_DIR}/uncommitted.txt lists what `git status --short` showed when the review started: the user's uncommitted edits and untracked files. An entry ending in `/` covers everything under that directory. They are the user's own work.

## Rules

1. Apply exactly the fixes listed above and nothing else: no refactoring beyond them, no style changes elsewhere.
2. If the project has tests relevant to the changed code, run them. Fix a failure only if your change caused it.
3. Never delete, move, rename, or rewrite a file that no fix above names — in particular nothing listed in {RUN_DIR}/uncommitted.txt.
4. Never delete, move, or rename a path listed in {RUN_DIR}/uncommitted.txt, even when a fix asks for it: an untracked file has no copy in git, and a tracked one would lose the user's uncommitted edits. Report such a fix as `skipped: <path> holds the user's uncommitted work; left to the user`.

## Committing

{COMMIT_RULES}

## Report

Write a report to {RUN_DIR}/fix-auto-report.md: one line per fix — `done`, `applied, not committed: <reason>`, or `skipped: <reason>` — and the commit hash, or `no commit` when nothing was committed. Reply with the single word DONE when the report is written.
