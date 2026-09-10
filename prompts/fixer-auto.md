# Fix task: apply the reviewed AUTO fixes

Repository: the current working directory. Run directory: {RUN_DIR}

## Fixes to apply

<ORCHESTRATOR: one entry per AUTO issue, in this shape>
- **path/to/file.ext:123** — what is wrong — the exact change to make

## Files a reviewer already changed

<ORCHESTRATOR: the paths from `drift_status`, one per line, or "none">

## Rules

1. Apply exactly the fixes listed above and nothing else: no refactoring beyond them, no style changes elsewhere.
2. If the project has tests relevant to the changed code, run them. Fix a failure only if your change caused it.
3. Do not stage or commit a file listed under "Files a reviewer already changed" unless one of the fixes above is in that file. If a fix is in such a file, apply it but report that item as `skipped: file already modified by a reviewer` instead of committing it.
4. Stage only the files you changed (`git add <file> …`), then commit exactly those paths:
   `git commit --only -m "review: auto-fix valid issues from herdr review" -- <file> …`
   `--only` commits exactly the named paths and disregards anything staged for other paths, so work the user had already staged never enters the review commit.
   Do not push.
5. Write a report to {RUN_DIR}/fix-auto-report.md: one line per fix — `done` or `skipped: <reason>` — and the commit hash.
6. Reply with the single word DONE when the report is written.
