# Fix task: apply one review decision

Repository: the current working directory. Run directory: {RUN_DIR}

Issue: <ORCHESTRATOR: number and title>
Location: <ORCHESTRATOR: path/to/file.ext:123>
Decision: <ORCHESTRATOR: the chosen variant, described precisely enough to implement without judgment calls>

## Files a reviewer already changed

<ORCHESTRATOR: the paths from `drift_status`, one per line, or "none">

## Rules

1. Implement exactly the decision above and nothing else.
2. If the project has tests relevant to the changed code, run them. Fix a failure only if your change caused it.
3. Do not stage or commit a file listed under "Files a reviewer already changed" unless the decision above is in that file. If it is, apply it but report it as `skipped: file already modified by a reviewer` instead of committing it.
4. Stage only the files you changed (`git add <file> …`), then commit exactly those paths:
   `git commit --only -m "<ORCHESTRATOR: commit message>" -- <file> …`
   `--only` commits exactly the named paths and disregards anything staged for other paths, so work the user had already staged never enters the review commit.
   Do not push.
5. Write a report to {RUN_DIR}/fix-<ORCHESTRATOR: n>-report.md: what you changed and the commit hash.
6. Reply with the single word DONE when the report is written.
