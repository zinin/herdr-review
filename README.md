# herdr-review

Multi-agent code review orchestrated inside [herdr](https://herdr.dev). Every participant is a visible, interactive agent in its own herdr tab: N **reviewers** (any agent kind and model), one **orchestrator** that drives them, and one **fixer** that commits the fixes. You launch it, switch to any tab to watch, and come back to a report — or to a question the orchestrator is waiting to ask you.

The plugin is two [Agent Skills](https://agentskills.io) — `review` launches a run, `auto-decide` hands a running one over to the orchestrator — and a runner CLI. The skills run in Claude Code, Grok, Codex, OpenCode and any other host that reads Agent Skills; the reviewers, the orchestrator and the fixer can be any agent herdr starts.

## Requirements

- herdr ≥ 0.9.0 with a running server; the launching session must be a herdr-managed pane (`HERDR_ENV=1`).
- Python ≥ 3.11 with PyYAML; git.
- The agent CLIs you want to use in PATH (`claude`, `codex`, `gemini`, `grok`, `opencode`, …). `herdr agent start --help` lists every kind herdr can start.

## Install

### Claude Code

```
/plugin marketplace add zinin/claude-plugins
/plugin install herdr-review@zinin
```

Upgrade with `claude plugin marketplace update zinin` and `claude plugin update herdr-review`. A checkout works without the marketplace: `claude --plugin-dir /path/to/herdr-review`. Claude Code puts the plugin's `bin` on PATH in its sessions, so `herdr-review` resolves there.

### Grok

Nothing to install: Grok loads every plugin Claude Code has installed (`~/.claude/plugins/installed_plugins.json`), and `grok inspect` lists both skills under `herdr-review`. Without Claude Code, `grok plugin install zinin/herdr-review`. Grok exposes the skills as `/herdr-review:review` — the bare `/review` is Grok's own command — and `/auto-decide`.

### Codex and OpenCode

Both read `~/.agents/skills` and follow symlinks:

```bash
git clone https://github.com/zinin/herdr-review.git /path/to/herdr-review
ln -s /path/to/herdr-review/skills/review ~/.agents/skills/herdr-review
ln -s /path/to/herdr-review/skills/auto-decide ~/.agents/skills/herdr-review-auto-decide
```

The Claude Code cache (`~/.claude/plugins/cache/zinin/herdr-review/<version>`) serves as the link target too, but its path carries the version and moves on every update.

- **Codex** recognises the plugin manifest behind the link and namespaces the skills: `$herdr-review:review` and `$herdr-review:auto-decide` (`/skills` lists them). Its sandbox blocks the herdr server socket, so `herdr-review launch` runs only outside it: approve the escalation Codex asks for, or start Codex with `--sandbox danger-full-access`.
- **OpenCode** lists them as `review` and `auto-decide` (`opencode debug skill`). Skills have no slash form there: ask in plain words («запусти herdr review с пресетом default»), and the agent loads the skill through its `skill` tool.

Any other host that reads Agent Skills takes the same links; the skill names come from the `SKILL.md` frontmatter.

### The runner in a shell

`bin/herdr-review` runs from the checkout without installation. To call it from a shell pane outside Claude Code, link it into PATH: `ln -s /path/to/herdr-review/bin/herdr-review ~/.local/bin/herdr-review`.

## Configure

```bash
mkdir -p ~/.config/herdr-review
cp config.example.yaml ~/.config/herdr-review/config.yaml
chmod 600 ~/.config/herdr-review/config.yaml
```

`config.example.yaml` sits in the plugin directory: the checkout, or `~/.claude/plugins/cache/zinin/herdr-review/<version>/` after a marketplace install. `HERDR_REVIEW_CONFIG` points the plugin at a config file elsewhere.

A **profile** is a herdr agent kind plus CLI args plus env; the same profile can serve as reviewer, orchestrator, or fixer. Put the yolo flags in `args` — the plugin adds none. An alt-provider model is the `claude` kind with `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` in `env` (`${VAR}` expands from the launcher's environment). A **preset** names the reviewers, the orchestrator and the fixer. `settings`: `layout` (`tabs` — one tab per agent; `grid` — panes in the orchestrator's tab), `autodecide`, `close_agents_on_finish`, `checkin_sec`, `runs_dir`.

herdr 0.9.0 takes profile env as `--env K=V` on `tab create` / `pane split`. Those values are visible in `/proc/<pid>/cmdline` for the duration of the call and may appear in the herdr server's own logs. `runner.log` masks them; the server log is outside this plugin. A secret belongs in `env` and never in `args`: `args` values are masked neither in `run.json` nor in `runner.log`. Use the tool on a machine you trust.

Check it: `herdr-review profiles`.

## Run

From an agent session inside herdr, invoke the `review` skill with a preset name — `/herdr-review:review default` in Claude Code and Grok, `$herdr-review:review default` in Codex, a plain request in OpenCode — or without arguments to pick the reviewers interactively. The skill takes, in any order: a preset name, `reviewers=a,b,c`, `orchestrator=<profile>`, `fixer=<profile>`, `BASE_BRANCH=<ref>`, `autodecide`, `layout=tabs|grid`; any other text becomes the description. From a shell pane:

```bash
herdr-review launch --preset default --description "what was implemented" --plan docs/plan.md
herdr-review status --run latest
```

A run costs what its preset costs: every reviewer reads the whole diff, and the orchestrator stays alive until the report is written.

`latest` is resolved per repository, from the run directory of the repository you are standing in: runs are stored under `<directory name>-<short hash of the repository path>`, so two checkouts with the same directory name keep separate runs and separate `latest` symlinks. Ask about a review from the repository that was reviewed, or pass that run's directory instead.

Reviewers see the live working tree, including uncommitted files. There is no stash or snapshot: yolo flags in the profile can change that tree. `launch` warns when the tree is dirty.

What happens:

1. `launch` creates the tab `rv-<run_id>: orch`, starts the orchestrator agent there and hands it `orchestrator.md`.
2. The orchestrator starts every reviewer in its own tab (`rv-<run_id>: <profile>`), waits, reads screens when something looks stuck, answers startup dialogs, and drops reviewers that cannot work.
3. Reviewers write `reviews/<profile>.md`; the orchestrator deduplicates, verifies each finding in the code and classifies it AUTO / DISPUTED / DISMISSED (`issues.md`).
4. The fixer starts (`rv-<run_id>: fixer`), applies the AUTO fixes and commits them.
5. Disputed issues go one at a time: with `autodecide` the orchestrator decides (with a self-check and a «под вопросом» mark when unsure); without it, it writes the analysis in its tab, sends a herdr notification, and waits for your answer there. You can hand the rest over in the middle of a run: answer «авто» in the orchestrator's tab, or run the `auto-decide` skill from your own session (`/herdr-review:auto-decide` in Claude Code, `/auto-decide` in Grok, `$herdr-review:auto-decide` in Codex) — it flips the mode and writes to the orchestrator, which is what actually wakes it. The switch is one-way and covers the drift question too.
6. `report.md` and a «готово» notification. Tab labels carry the state: ` ⏳` working, ` ✓` done, ` ✗` failed, ` ❓` blocked or waiting for you.

Run artifacts live in `~/.local/state/herdr-review/runs/<project>/<timestamp>-<run_id>/` (`latest` symlink), where `<project>` is the repository's directory name plus a short hash of its path: `run.json`, `status.json`, `orchestrator.md`, `prompts/`, `reviews/`, `issues.md`, `fix-*.md`, `report.md`, `runner.log`.

## Troubleshooting

- «not inside herdr» — start the session in a herdr pane.
- «config not found» — copy `config.example.yaml` from the plugin directory as shown in Configure; the message names the path the plugin looked at.
- «Operation not permitted» from `herdr` in a Codex session — the Codex sandbox blocks the herdr socket. Approve the escalation, or start Codex with `--sandbox danger-full-access`.
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

`tests/fake-herdr/herdr` fakes the herdr CLI from a JSON scenario; `tests/bats/helpers.bash` builds a temp config and git repository for each test. The bats half needs [bats](https://github.com/bats-core/bats-core) in PATH; without it `tests/run.sh` runs the unit tests and says so.

## License

MIT — see [LICENSE](LICENSE).
