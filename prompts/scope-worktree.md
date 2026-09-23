1. Run `git diff {MERGE_BASE} --`. It shows every change to tracked files on this branch against the base, committed and uncommitted.
2. New files of the change are untracked, so no diff shows them: read the files listed below along with the diff. A file marked `skip` is binary, a symlink, or too large to read: do not read it, and mention it in the review only if it matters. Leave every file untracked: do not `git add` anything (see the Hard Rules).

{UNTRACKED}
