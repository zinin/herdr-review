# herdr-review

Multi-agent code review orchestrated inside [herdr](https://herdr.dev). Every participant is a visible, interactive agent in its own herdr tab: N **reviewers** (any agent kind and model), one **orchestrator** that drives them, and one **fixer** that commits the fixes. You launch it, switch to any tab to watch, and come back to a report — or to a question the orchestrator is waiting to ask you.

The plugin is two [Agent Skills](https://agentskills.io) — `review` launches a run, `auto-decide` hands a running one over to the orchestrator — and the `herdr-review` CLI behind them. The skills run in Claude Code, Grok, Codex, OpenCode and any other host that reads Agent Skills; the reviewers, the orchestrator and the fixer can be any agent herdr starts.

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
- **OpenCode** lists them as `review` and `auto-decide` (`opencode debug skill`). Skills have no slash form there: ask in plain words ("run a herdr review with the default preset"), and the agent loads the skill through its `skill` tool.

Any other host that reads Agent Skills takes the same links; the skill names come from the `SKILL.md` frontmatter.

### The CLI in a shell

`bin/herdr-review` runs from the checkout without installation. To call it from a shell pane outside Claude Code, link it into PATH: `ln -s /path/to/herdr-review/bin/herdr-review ~/.local/bin/herdr-review`.

## Configure

The config is `~/.config/herdr-review/config.yaml` (`$XDG_CONFIG_HOME` is honoured; `HERDR_REVIEW_CONFIG` overrides the path). Start from the example in the plugin directory — the checkout, or `~/.claude/plugins/cache/zinin/herdr-review/<version>/` after a marketplace install:

```bash
mkdir -p ~/.config/herdr-review
cp config.example.yaml ~/.config/herdr-review/config.yaml
chmod 600 ~/.config/herdr-review/config.yaml
```

Three sections: **profiles** (what an agent is), **presets** (which profiles make a review), **settings**.

```yaml
profiles:                        # a profile = herdr agent kind + CLI args + env;
  claude-opus:                   # the same profile can be a reviewer, the orchestrator or the fixer
    kind: claude                 # a kind from `herdr agent start --help`; its executable must be in PATH
    args: [--model, opus, --permission-mode, auto, --add-dir, "~/.local/state/herdr-review/runs"]
    # args: [--model, opus, --dangerously-skip-permissions]          # yolo: swap the comments
  codex:
    kind: codex
    args: [-m, gpt-5.5, -c, model_reasoning_effort=high, --approve-for-me, --add-dir, "~/.local/state/herdr-review/runs"]
    # args: [-m, gpt-5.5, -c, model_reasoning_effort=high, --dangerously-bypass-approvals-and-sandbox]
  grok:
    kind: grok
    args: [-m, grok-4.6, --permission-mode, auto]
    # args: [-m, grok-4.6, --always-approve]
  glm:                           # an alt-provider model: the claude kind plus env
    kind: claude
    args: [--model, glm-5, --dangerously-skip-permissions]   # the classifier is a model call too: auto mode untested here
    env:
      ANTHROPIC_BASE_URL: https://api.z.ai/api/anthropic
      ANTHROPIC_AUTH_TOKEN: "${ZAI_TOKEN}"   # ${VAR} expands from the launcher's environment

presets:
  default:                       # `/herdr-review:review default`; also `launch` without --preset
    reviewers: [claude-opus, codex, grok]
    orchestrator: claude-opus
    fixer: claude-opus

settings:
  layout: tabs                   # tabs: one tab per agent | grid: panes in the orchestrator's tab
  autodecide: false              # true: the orchestrator decides disputed issues itself
  scope: auto                    # auto: the branch's commits, else the working tree | commits | worktree
  close_agents_on_finish: false  # true: close the reviewer and fixer tabs when the run ends
  checkin_sec: 300               # how often `run wait` hands control back to the orchestrator
  runs_dir: ~/.local/state/herdr-review/runs
```

Each profile's `args` is one of two lines, the other commented out. **Auto mode** (active in the example): the CLI's own classifier or sandbox judges every action, and what it does not allow becomes a dialog in the agent's tab that the orchestrator answers (the tab shows ` ❓` until it does). **Yolo**: everything is approved in advance; for a throwaway VM or container.

| kind | auto mode | yolo |
|---|---|---|
| `claude` | `--permission-mode auto --add-dir <runs_dir>` | `--dangerously-skip-permissions` |
| `codex` | `--approve-for-me --add-dir <runs_dir>` | `--dangerously-bypass-approvals-and-sandbox` |
| `grok` | `--permission-mode auto` | `--always-approve` |
| `gemini` | none: `--approval-mode auto_edit` still asks about every shell command | `--yolo` |

`<runs_dir>` is `settings.runs_dir`, where the agents write outside the repository; both CLIs expand the `~`. The codex sandbox refuses that write without `--add-dir`; claude only consults its classifier more often. The auto mode flags need Claude Code ≥ 2.1.111 (auto mode is also gated by the subscription plan) and Codex ≥ 0.147.0; an older CLI rejects the flag, and that agent leaves the run as ` ✗` with the reason in `herdr-review status`. Checked with Claude Code 2.1.267, Codex 0.153.4 and Grok 1.0.25: reviewers, orchestrator and fixer all in auto mode finished a run with no dialog left for a human.

Rules the validator enforces and facts worth knowing:

| What | Rule |
|---|---|
| `kind` | A herdr agent kind, which is also the executable name; `herdr agent start --help` lists them. |
| `args` | Passed verbatim, with one addition: a profile of kind `claude` without a `--settings` of its own starts with `--settings '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'` (see MCP servers below). The permission flags go here (the mode table above). Copied as they are into `run.json` and `runner.log`: never a secret. |
| `env` | For tokens and base URLs. Masked in `runner.log`; still visible in `/proc/<pid>/cmdline` while herdr creates the tab and possibly in the herdr server's own log. |
| Profile names | `^[a-z][a-z0-9_-]{0,24}$`; `orch` and `fixer` are reserved. |
| Presets | Every name must be a profile; `default` is used when nothing else is named. Each reviewer is a full review of the diff, so a preset's size is its cost. |
| The file | 600 permissions (the plugin warns otherwise). The plugin never edits it: validation prints the problems and stops. |

Check the result: `herdr-review profiles`.

**MCP servers.** Claude Code asks at startup to approve the servers of a project's `.mcp.json` that you have not decided on, and saves every answer — Esc included — into the repository's `.claude/settings.local.json`. So that no run stops at that dialog or changes your MCP settings, every profile of kind `claude` starts with `--settings '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'`: the agents of a run get the project's servers, your user servers and the claude.ai connectors, a server you disabled explicitly stays disabled, and the settings live on the command line only; `attribution.commit` set to an empty string keeps Claude Code's commit trailer out of the fixer's commits. Two consequences: an MCP server that the branch under review adds to `.mcp.json` starts in every claude agent of the run without approval; and a profile that passes its own `--settings` gets nothing added — put `"enableAllProjectMcpServers": true` into that settings file, or its agents stop at the dialog and leave the run with that reason, and `"attribution": {"commit": ""}` if its fix commits are to carry no trailer. Codex and Grok load a project's servers without asking; the runner answers their trust dialogs, as it answers Claude Code's. The trust answers the runner gives are saved by each CLI, so after one run the repository stays trusted in Claude Code, Codex and Grok. For a branch you do not fully trust, put `--strict-mcp-config` into the claude profile's `args`: its agents then load no MCP servers at all, project or user.

## Usage

Everything starts from a herdr pane: an agent session with the skills, or a shell with `herdr-review` in PATH.

The change under review is the branch's commits since the base (`git diff <merge-base> HEAD`). Your uncommitted edits and untracked files stay out of it, and nothing in the run deletes, moves or commits them: `launch` lists them in the run directory as `uncommitted.txt` and says so in its summary. When nothing is committed yet, the whole working tree is the change. `scope=worktree` (`--scope worktree`, or `settings.scope`) reviews the working tree with its uncommitted work even when there are commits.

### `review` — launch a run

| Host | Command |
|---|---|
| Claude Code, Grok | `/herdr-review:review default` |
| Codex | `$herdr-review:review default` |
| OpenCode | "run a herdr review with the default preset" |

Arguments, in any order:

| Argument | Meaning |
|---|---|
| `default` (any preset name) | Take reviewers, orchestrator and fixer from that preset. |
| `reviewers=codex,grok` | Reviewers by profile name instead of a preset. |
| `orchestrator=<profile>`, `fixer=<profile>` | Override those two roles. |
| `BASE_BRANCH=<ref>` | Review against this ref. Default: `origin/HEAD`, else `master`, else `main`. |
| `autodecide` | The orchestrator decides disputed issues itself instead of asking you. |
| `layout=tabs` / `layout=grid` | One tab per agent, or panes inside the orchestrator's tab. |
| `scope=worktree` / `scope=commits` / `scope=auto` | Review the working tree with its uncommitted work, only the branch's commits, or (`auto`) the commits, else the working tree. Default: `settings.scope`, `auto` unless you changed it. |
| anything else | The description of the change, handed to the reviewers. |

With a preset or `reviewers=` the skill asks nothing. Without either it asks four questions: reviewers, orchestrator, fixer, autodecide. It never asks about uncommitted files or startup dialogs. It also passes the plan when it knows one from the session: a file path, or free text — `git show <sha>:<path>` when the plan lives only in git history, and your rulings from earlier review rounds.

```
/herdr-review:review default
/herdr-review:review default autodecide added retries to the uploader
/herdr-review:review reviewers=codex,grok orchestrator=claude-opus fixer=claude-opus BASE_BRANCH=develop
```

The skill prints the run directory, the orchestrator's agent name, what is under review and what stays out of it, that the fixes stay uncommitted for you to commit when the working tree is under review, and the two commands to watch it (`herdr agent focus <name>`, `herdr-review status latest`), then ends its turn. The run continues in the orchestrator's tab.

### `auto-decide` — stop answering

`/herdr-review:auto-decide` (Grok: `/auto-decide`, Codex: `$herdr-review:auto-decide`) switches the current run of the repository you are in to automatic decisions and wakes the orchestrator. One-way; it also settles the drift question. Answering "auto" in the orchestrator's tab does the same.

### The CLI

```bash
herdr-review launch --preset default --description "what was implemented" --plan docs/plan.md
herdr-review launch --reviewers codex,grok --orchestrator claude-opus --fixer claude-opus --base develop --autodecide --layout grid
herdr-review launch --preset default --scope worktree   # uncommitted work is part of the change
herdr-review status                 # the latest run of the repository you are in
herdr-review status <run dir>       # any run
herdr-review close                  # close every tab of the latest finished run
herdr-review profiles               # the validated config, secrets omitted
herdr-review exclusive -- ./gradlew build   # your own heavy command, in turn with the reviews on this machine
```

`launch --help` lists every flag (`--no-autodecide`, `--focus`, …); `--json` on any command gives machine-readable output. `launch --json` also carries `scope`, `uncommitted` (the `git status --short` lines kept out of the review) and, in the worktree scope, `untracked` (`files`, `skipped`). `herdr-review run …` is what the orchestrator calls during the run; you never need it.

`herdr-review exclusive -- <command>` runs a heavy command — a build, tests, a dependency install, a server — only while no other heavy command runs on this machine: it holds an exclusive lock on `<runs_dir>/exclusive.lock`, the queue that the reviewers and the fixer of every review share. It waits up to 60 s for its turn (`--wait SEC`; `0` tries once) and exits 75 without running the command when the turn does not come; a command still running after 30 minutes (`--timeout SEC`) is stopped, with everything it started, and the wrapper exits 124. Otherwise it exits with the command's own code. Run your own build through it during a review, and it takes its turn with the agents.

### During the run

- **Tabs:** `rv-<run_id>: orch` (the orchestrator), `rv-<run_id>: <profile>` per reviewer, `rv-<run_id>: fixer` once there is something to fix. Labels carry the state: ` ⏳` working, ` ✓` done, ` ✗` failed, ` ❓` stuck on a dialog or waiting for you. Once the run has finished, `herdr-review close` closes every tab it opened; `settings.close_agents_on_finish: true` closes the reviewers' and the fixer's by itself at the end. herdr numbers tabs per session, so `close` runs only from a pane of the herdr session the run was launched in, and it leaves open a tab whose label no longer names the run — after a herdr restart its ID can belong to another tab — and lists it under «оставлены открытыми» (`left_open` with `--json`).
- **The orchestrator's tab** shows the progress, the classification of every finding (AUTO — fixed by the fixer; DISPUTED — decided one at a time; DISMISSED — false positive, with a reason), and the final report.
- **A disputed issue without autodecide:** the tab turns ` ❓` and a herdr notification arrives. The orchestrator has written its analysis with variants and a recommendation; answer in that tab in free text: a variant letter, a variant of your own, "don't fix", "stop" (defers the rest) or "auto" (the orchestrator decides the rest).
- **Fixes** land on your branch as commits written in your repository's own style: its subject convention, and a body when your history has them. The fixer commits only the files it changed and never deletes, moves or commits your uncommitted files: a fix that touches one of them is applied in full and left uncommitted, as is a fix that shares a file with such a fix, and the report says so. A file you start editing while the run is going on is treated the same way: the fixer checks `git status` before its first edit of a file. In the working-tree scope the fixer commits nothing: the change under review is uncommitted, and a fix committed without it could leave HEAD failing its own tests, so every fix it applies is left for you to commit with the rest. A fix that would delete, move or rename one of your uncommitted files is still skipped there.
- **Experiments:** each reviewer keeps its own scripts and scratch copies under `scratch/<profile>/` in the run directory — never in your repository or `/tmp`. `run finish` deletes `scratch/`.
- **Builds and tests:** the reviewers share your working tree and your machine, so their heavy commands — builds, tests, dependency installs, servers, containers — take turns: each goes through `herdr-review exclusive`, one at a time among all reviews on this machine, and a reviewer reads code while it waits. The fixer runs its tests the same way. `herdr-review status` shows who holds the queue (`очередь сборок: занята — …`). `run fail` stops a command of the agent it takes out of the run, and `run finish` and `close` stop any command of their run that still holds the queue.
- **The end:** a done notification and `report.md` in the run directory, `~/.local/state/herdr-review/runs/<project>/<timestamp>-<run_id>/` — next to `status.json`, `uncommitted.txt`, `reviews/<profile>.md`, `issues.md`, `fix-*.md` and `runner.log`. The report and `herdr-review status` list the commits made during the run. A merge commit that brings the base in is listed alone, without the base's commits; a fast-forward to the base (what `git merge` does on a branch with no commits of its own) still lists them. When the branch was rewritten during the run (a rebase, an amend, a reset or a branch switch), git can no longer tell which commits are the run's, and both list the fixer's commits from its reports instead. `latest` there points at the newest run of that repository.

## Troubleshooting

- `not inside herdr` — start the session in a herdr pane.
- `config not found` — copy `config.example.yaml` from the plugin directory as shown in Configure; the message names the path the plugin looked at.
- `Operation not permitted` from `herdr` in a Codex session — the Codex sandbox blocks the herdr socket. Approve the escalation, or start Codex with `--sandbox danger-full-access`.
- `orchestrator failed to start … Tab … is left open` — the message says why. The runner answers the trust dialogs of Claude Code, Codex and Grok; it never answers Claude Code's MCP approval dialog, and the message then says what to add to the profile's own `--settings`. Anything else — a login, an unknown dialog — waits in that tab: resolve it there, close that tab with `herdr-review close <run dir>` (the message's `Run directory:`; the run is already aborted) and launch again.
- `run … was started in herdr session '…'` from `close` — tab IDs are per herdr session: run `close` from a pane of that session.
- `no runs for this repository` from `status` — `latest` is per repository. Run it from the repository under review, or pass the run directory.
- A file you changed is not in the review — it is uncommitted, and the review covers the branch's commits. Commit it, or launch with `scope=worktree`.
- A reviewer shows ` ✗` — `herdr-review status` gives the reason and `status.json` the last screen.
- A reviewer or the fixer shows ` ✗` with "Claude Code asks to approve this project's MCP servers" — its profile passes its own `--settings`; add `"enableAllProjectMcpServers": true` to that file (see MCP servers in Configure). Do not answer the dialog left in its tab: any answer, Esc included, is saved; `herdr-review close` closes the tab once the run has finished.
- A reviewer or the fixer shows ` ❓` — a dialog is waiting in its tab; in auto mode that is the CLI asking about an action. `run wait` reports it to the orchestrator within seconds, and the orchestrator answers it or fails the agent when it does not understand the dialog. `herdr agent read <name> --source visible` shows what is asked.
- A reviewer stays ` ⏳` a long time and its tab shows `herdr-review exclusive: waiting` — another heavy command holds the build queue; `herdr-review status` names it and says how long it has run. A command stops by itself after 30 minutes unless its caller asked for more with `--timeout`.
- `herdr-review exclusive` exits 75 — its turn did not come within `--wait`, and the command did not run; run it again later. Exit 124 — the command ran past `--timeout` and was stopped.
- The orchestrator's tab shows ` ❓` — it is waiting for your answer in that tab.
- A run died with the orchestrator — `status.json` stays at its phase; a new `launch` starts a new run.

## Development

```bash
tests/run.sh          # unit tests (python -m unittest) + bats end-to-end tests with a fake herdr
tests/run.sh unit
tests/run.sh bats
```

`tests/fake-herdr/herdr` fakes the herdr CLI from a JSON scenario; `tests/bats/helpers.bash` builds a temp config and git repository for each test. The bats half needs [bats](https://github.com/bats-core/bats-core) in PATH; without it `tests/run.sh` runs the unit tests and says so. [tests/SMOKE.md](tests/SMOKE.md) is the checklist for a run against a real herdr.

## License

MIT — see [LICENSE](LICENSE).
