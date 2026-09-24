# Fix task: apply one review decision

Repository: the current working directory. Run directory: {RUN_DIR}

Issue: <ORCHESTRATOR: number and title>
Location: <ORCHESTRATOR: path/to/file.ext:123>
Problem: <ORCHESTRATOR: what is wrong, in one or two sentences>
Decision: <ORCHESTRATOR: the chosen variant, described precisely enough to implement without judgment calls>

## Files changed during the review

<ORCHESTRATOR: the paths from `drift_status`, one per line, or "none">

## Uncommitted before the review

{RUN_DIR}/uncommitted.txt lists what `git status --short` showed when the review started: the user's uncommitted edits and untracked files. An entry whose path ends in `/` covers everything under that directory. They are the user's own work. Each entry is a `git status --short` line: a two-character status such as `??` or ` M`, a space, then the path — in double quotes with C escapes when it holds a space or another special character, and `old -> new` for a rename. `git … --name-only` leaves a path unquoted when a space is its only special character, so compare paths, not their quoting: `?? "my notes/"` is the untracked directory `my notes/`.

## Rules

1. Implement exactly the decision above and nothing else.
2. If the project has tests relevant to the changed code, run them. Fix a failure only if your change caused it.
3. Never delete, move, rename, or rewrite a file that the decision does not name — in particular nothing listed in {RUN_DIR}/uncommitted.txt.
4. Never delete, move, or rename a path listed in {RUN_DIR}/uncommitted.txt, even when the decision asks for it: an untracked file has no copy in git, and a tracked one would lose the user's uncommitted edits. Report it as `skipped: <path> holds the user's uncommitted work; left to the user`.

## Committing

{COMMIT_RULES}

## Report

Write a report to {RUN_DIR}/fix-<ORCHESTRATOR: n>-report.md: what you changed; `done`, `applied, not committed: <reason>`, or `skipped: <reason>`; and the commit hash, or `no commit` when nothing was committed. Reply with the single word DONE when the report is written.
