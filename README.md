# herdr-review

Multi-agent code review orchestrated inside [herdr](https://herdr.dev). Every participant is a visible, interactive agent in its own herdr tab: N **reviewers** (any agent kind and model), one **orchestrator** that drives them, and one **fixer** that commits the fixes. You launch it, switch to any tab to watch, and come back to a report — or to a question the orchestrator is waiting to ask you.

## Requirements

- herdr ≥ 0.9.0 with a running server; the launching session must be a herdr-managed pane (`HERDR_ENV=1`).
- Python ≥ 3.11 with PyYAML; git.
- The agent CLIs you want to use in PATH (`claude`, `codex`, `gemini`, `grok`, …).

## Install

Claude Code: `claude --plugin-dir /path/to/herdr-review` for a session, or add the repository to a marketplace and `claude plugin install herdr-review`.

Any other host that reads Agent Skills: `ln -s /path/to/herdr-review/skills/review ~/.agents/skills/herdr-review` and `ln -s /path/to/herdr-review/skills/auto-decide ~/.agents/skills/herdr-review-auto-decide`. Optionally put `bin/herdr-review` in PATH.

## Configure

```bash
mkdir -p ~/.config/herdr-review
cp config.example.yaml ~/.config/herdr-review/config.yaml
chmod 600 ~/.config/herdr-review/config.yaml
```

A **profile** is a herdr agent kind plus CLI args plus env; the same profile can serve as reviewer, orchestrator, or fixer. Put the yolo flags in `args` — the plugin adds none. An alt-provider model is the `claude` kind with `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` in `env` (`${VAR}` expands from the launcher's environment). A **preset** names the reviewers, the orchestrator and the fixer. `settings`: `layout` (`tabs` — one tab per agent; `grid` — panes in the orchestrator's tab), `autodecide`, `close_agents_on_finish`, `checkin_sec`, `runs_dir`.

herdr 0.9.0 takes profile env as `--env K=V` on `tab create` / `pane split`. Those values are visible in `/proc/<pid>/cmdline` for the duration of the call and may appear in the herdr server's own logs. `runner.log` masks them; the server log is outside this plugin. A secret belongs in `env` and never in `args`: `args` values are masked neither in `run.json` nor in `runner.log`. Use the tool on a machine you trust.

Check it: `bin/herdr-review profiles`.

## Run

From an agent session inside herdr: `/herdr-review:review default` (or without arguments to pick reviewers interactively). From a shell pane:

```bash
herdr-review launch --preset default --description "what was implemented" --plan docs/plan.md
herdr-review status --run latest
```

`latest` is resolved per repository, from the run directory of the repository you are standing in: runs are stored under `<directory name>-<short hash of the repository path>`, so two checkouts with the same directory name keep separate runs and separate `latest` symlinks. Ask about a review from the repository that was reviewed, or pass that run's directory instead.

Reviewers see the live working tree, including uncommitted files. There is no stash or snapshot: yolo flags in the profile can change that tree. `launch` warns when the tree is dirty.

What happens:

1. `launch` creates the tab `rv-<run_id>: orch`, starts the orchestrator agent there and hands it `orchestrator.md`.
2. The orchestrator starts every reviewer in its own tab (`rv-<run_id>: <profile>`), waits, reads screens when something looks stuck, answers startup dialogs, and drops reviewers that cannot work.
3. Reviewers write `reviews/<profile>.md`; the orchestrator deduplicates, verifies each finding in the code and classifies it AUTO / DISPUTED / DISMISSED (`issues.md`).
4. The fixer starts (`rv-<run_id>: fixer`), applies the AUTO fixes and commits them.
5. Disputed issues go one at a time: with `autodecide` the orchestrator decides (with a self-check and a «под вопросом» mark when unsure); without it, it writes the analysis in its tab, sends a herdr notification, and waits for your answer there. You can hand the rest over in the middle of a run: answer «авто» in the orchestrator's tab, or run `/herdr-review:auto-decide` from your own session — it flips the mode and writes to the orchestrator, which is what actually wakes it. The switch is one-way and covers the drift question too.
6. `report.md` and a «готово» notification. Tab labels carry the state: ` ⏳` working, ` ✓` done, ` ✗` failed, ` ❓` blocked or waiting for you.

Run artifacts live in `~/.local/state/herdr-review/runs/<project>/<timestamp>-<run_id>/` (`latest` symlink), where `<project>` is the repository's directory name plus a short hash of its path: `run.json`, `status.json`, `orchestrator.md`, `prompts/`, `reviews/`, `issues.md`, `fix-*.md`, `report.md`, `runner.log`.

## Troubleshooting

- «not inside herdr» — start the session in a herdr pane.
- «orchestrator failed to start … Tab … is left open» — open that tab; a login or dialog is waiting. Resolve it and run the printed `herdr agent prompt …`.
- «no runs for this repository» from `status --run latest` — you are in a different repository than the one under review; `latest` is per repository. Run it there, or pass `--run <run dir>`.
- A reviewer shows ` ✗` — `herdr-review status` gives the reason and `status.json` the last screen.
- The orchestrator's tab shows ` ❓` — it is waiting for your answer in that tab.
- A run died with the orchestrator — `status.json` stays at its phase; a new `launch` starts a new run.

## Smoke checklist (real herdr)

1. `cp config.example.yaml ~/.config/herdr-review/config.yaml`, keep two profiles (`claude-opus`, `codex`), preset `default` with both, `orchestrator: claude-opus`, `fixer: claude-opus`.
2. In a herdr pane inside this repository on a branch with changes: `bin/herdr-review launch --preset default --description "smoke"`.
3. Expect tabs `rv-<id>: orch`, then `rv-<id>: claude-opus ⏳` and `rv-<id>: codex ⏳`; `bin/herdr-review status --run latest` shows `working`.
4. Reviewers turn ` ✓`; `reviews/*.md` exist; the orchestrator prints the classification table.
5. The fixer tab appears if there is anything to fix; commits `review: …` land on the branch.
6. Without autodecide: the orchestrator's tab turns ` ❓` on the first disputed issue and a notification appears; answer in the tab.
7. `report.md` is written; the «готово» notification appears; `status` says `finished`.

## Development

```bash
tests/run.sh          # unit tests (python -m unittest) + bats end-to-end tests with a fake herdr
tests/run.sh unit
tests/run.sh bats
```

`tests/fake-herdr/herdr` fakes the herdr CLI from a JSON scenario; `tests/bats/helpers.bash` builds a temp config and git repository for each test.
