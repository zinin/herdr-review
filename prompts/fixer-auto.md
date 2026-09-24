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
5. Never stage or commit a path listed in {RUN_DIR}/uncommitted.txt or under "Files a reviewer already changed". When a fix falls into such a file, apply it, leave the file uncommitted, and report the fix as `applied, not committed: the user's uncommitted edits` or `applied, not committed: changed by a reviewer`. A fix is committed whole or not at all: when any file a fix changes is such a path, apply every file of the fix, commit none of them, and report the whole fix as `applied, not committed: …`. Before your first edit of any file, run `git status --porcelain --untracked-files=all -- <file>`: if the file already differs from HEAD or is untracked, the user has been editing it since the review started — treat it exactly like a path listed in {RUN_DIR}/uncommitted.txt: apply the fix, commit nothing of that fix, and report `applied, not committed: the user's uncommitted edits`.
6. Write the commit message in this repository's style. Read `git log -n 20 --format='%s%n%n%b'`: follow its subject convention (for example `fix(scope): …`), and if its commits carry bodies, add one that says why, one line per fix. Do not mention the review, the reviewers, or herdr-review. Add no trailers or attribution lines after the body — no `Co-Authored-By:`, `Claude-Session:` or "Generated with …" line — even where the history has them. Write the message to {RUN_DIR}/fix-auto-commit.txt.
7. Stage only the files of the fixes you report as `done` (`git add <file> …`), then commit exactly those paths:
   `git commit --only -F {RUN_DIR}/fix-auto-commit.txt -- <file> …`
   `--only` commits exactly the named paths and disregards anything staged for other paths, so work the user had already staged never enters the commit.
   Do not push.
8. Write a report to {RUN_DIR}/fix-auto-report.md: one line per fix — `done`, `applied, not committed: <reason>`, or `skipped: <reason>` — and the commit hash, or `no commit` when nothing was committed.
9. Reply with the single word DONE when the report is written.
