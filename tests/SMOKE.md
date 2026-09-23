# Smoke checklist (real herdr)

1. `cp config.example.yaml ~/.config/herdr-review/config.yaml`, keep two profiles (`claude-opus`, `codex`), preset `default` with both, `orchestrator: claude-opus`, `fixer: claude-opus`.
2. In a herdr pane inside this repository on a branch with changes: `bin/herdr-review launch --preset default --description "smoke"`.
3. Expect tabs `rv-<id>: orch`, then `rv-<id>: claude-opus ⏳` and `rv-<id>: codex ⏳`; `bin/herdr-review status --run latest` shows `working`.
4. Reviewers turn ` ✓`; `reviews/*.md` exist; the orchestrator prints the classification table.
5. The fixer tab appears if there is anything to fix; its commits land on the branch in the repository's own commit style, with no trailers and no mention of the review.
6. Without autodecide: the orchestrator's tab turns ` ❓` on the first disputed issue and a notification appears; answer in the tab.
7. `report.md` is written; the «готово» notification appears; `status` says `finished`.
8. Dirty tree: before launching, edit a tracked file and add an untracked file and an untracked directory unrelated to the change. The summary shows `вне ревью` with both counts, `uncommitted.txt` lists them, no review mentions them, and after the run they are unchanged and in no commit.
9. MCP: in a repository whose `.mcp.json` names a server nobody has approved yet, every claude agent starts without the MCP approval dialog, `/mcp` in a reviewer's tab lists the project's servers, and `.claude/settings.local.json` stays as it was.
10. `herdr-review close` refuses while the run is in progress and closes every tab of the run once it has finished.
