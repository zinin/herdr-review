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
5. Never stage or commit a path listed in {RUN_DIR}/uncommitted.txt or under "Files a reviewer already changed". When the decision falls into such a file, apply it, leave the file uncommitted, and report `applied, not committed: the user's uncommitted edits` or `applied, not committed: changed by a reviewer`. The decision is committed whole or not at all: when any file it changes is such a path, apply every file, commit none of them, and report `applied, not committed: …`.
6. Write the commit message in this repository's style. Read `git log -n 20 --format='%s%n%n%b'`: follow its subject convention (for example `fix(scope): …`), and if its commits carry bodies, add one that says what was wrong and why the change fixes it. Do not mention the review, the reviewers, or herdr-review. Add no trailers or attribution lines after the body — no `Co-Authored-By:`, `Claude-Session:` or "Generated with …" line — even where the history has them. Write the message to {RUN_DIR}/fix-<ORCHESTRATOR: n>-commit.txt.
7. Stage only the files you changed when you report `done` (`git add <file> …`), then commit exactly those paths:
   `git commit --only -F {RUN_DIR}/fix-<ORCHESTRATOR: n>-commit.txt -- <file> …`
   `--only` commits exactly the named paths and disregards anything staged for other paths, so work the user had already staged never enters the commit.
   Do not push.
8. Write a report to {RUN_DIR}/fix-<ORCHESTRATOR: n>-report.md: what you changed; `done`, `applied, not committed: <reason>`, or `skipped: <reason>`; and the commit hash, or `no commit` when nothing was committed.
9. Reply with the single word DONE when the report is written.
