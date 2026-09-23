1. Run `git diff {MERGE_BASE} HEAD --`. That diff is the change under review: the commits of this branch since the base, and nothing else.
2. The working tree also holds uncommitted work that is not part of the change — the user's own files, listed below as `git status --short` showed them when the review started. An entry ending in `/` covers everything under that directory. Do not review these files and do not mention them in the review. Where a file of the change also has uncommitted edits, read its committed version with `git show HEAD:<path>`.

{UNCOMMITTED}
