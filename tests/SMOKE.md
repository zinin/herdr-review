# Smoke checklist (real herdr)

1. `cp config.example.yaml ~/.config/herdr-review/config.yaml`, keep two profiles (`claude-opus`, `codex`), preset `default` with both, `orchestrator: claude-opus`, `fixer: claude-opus`.
2. In a herdr pane inside this repository on a branch with changes: `bin/herdr-review launch --preset default --description "smoke"`.
3. Expect tabs `rv-<id>: orch`, then `rv-<id>: claude-opus ⏳` and `rv-<id>: codex ⏳`; `bin/herdr-review status --run latest` shows `working`.
4. Reviewers turn ` ✓`; `reviews/*.md` exist; the orchestrator prints the classification table.
5. The fixer tab appears if there is anything to fix; commits `review: …` land on the branch.
6. Without autodecide: the orchestrator's tab turns ` ❓` on the first disputed issue and a notification appears; answer in the tab.
7. `report.md` is written; the «готово» notification appears; `status` says `finished`.
