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
- **OpenCode** lists them as `review` and `auto-decide` (`opencode debug skill`). Skills have no slash form there: ask in plain words («запусти herdr review с пресетом default»), and the agent loads the skill through its `skill` tool.

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
    args: [--model, opus, --dangerously-skip-permissions]
  codex:
    kind: codex
    args: [-m, gpt-5.5, -c, model_reasoning_effort=high, --dangerously-bypass-approvals-and-sandbox]
  grok:
    kind: grok
    args: [-m, grok-4.6, --always-approve]
  glm:                           # an alt-provider model: the claude kind plus env
    kind: claude
    args: [--model, glm-5, --dangerously-skip-permissions]
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
  close_agents_on_finish: false  # true: close the reviewer and fixer tabs when the run ends
  checkin_sec: 300               # how often `run wait` hands control back to the orchestrator
  runs_dir: ~/.local/state/herdr-review/runs
```

Rules the validator enforces and facts worth knowing:

| What | Rule |
|---|---|
| `kind` | A herdr agent kind, which is also the executable name; `herdr agent start --help` lists them. |
| `args` | Passed verbatim; the plugin adds nothing, so the yolo flags go here. Copied as they are into `run.json` and `runner.log`: never a secret. |
| `env` | For tokens and base URLs. Masked in `runner.log`; still visible in `/proc/<pid>/cmdline` while herdr creates the tab and possibly in the herdr server's own log. |
| Profile names | `^[a-z][a-z0-9_-]{0,24}$`; `orch` and `fixer` are reserved. |
| Presets | Every name must be a profile; `default` is used when nothing else is named. Each reviewer is a full review of the diff, so a preset's size is its cost. |
| The file | 600 permissions (the plugin warns otherwise). The plugin never edits it: validation prints the problems and stops. |

Check the result: `herdr-review profiles`.

## Usage

Everything starts from a herdr pane: an agent session with the skills, or a shell with `herdr-review` in PATH. Reviewers work on the live working tree, uncommitted files included; `launch` warns when the tree is dirty.

### `review` — launch a run

| Host | Command |
|---|---|
| Claude Code, Grok | `/herdr-review:review default` |
| Codex | `$herdr-review:review default` |
| OpenCode | «запусти herdr review default» |

Arguments, in any order:

| Argument | Meaning |
|---|---|
| `default` (any preset name) | Take reviewers, orchestrator and fixer from that preset. |
| `reviewers=codex,grok` | Reviewers by profile name instead of a preset. |
| `orchestrator=<profile>`, `fixer=<profile>` | Override those two roles. |
| `BASE_BRANCH=<ref>` | Review against this ref. Default: `origin/HEAD`, else `master`, else `main`. |
| `autodecide` | The orchestrator decides disputed issues itself instead of asking you. |
| `layout=tabs` / `layout=grid` | One tab per agent, or panes inside the orchestrator's tab. |
| anything else | The description of the change, handed to the reviewers. |

With a preset or `reviewers=` the skill asks nothing. Without either it asks four questions: reviewers, orchestrator, fixer, autodecide. It also passes a plan file when it knows one from the session.

```
/herdr-review:review default
/herdr-review:review default autodecide added retries to the uploader
/herdr-review:review reviewers=codex,grok orchestrator=claude-opus fixer=claude-opus BASE_BRANCH=develop
```

The skill prints the run directory, the orchestrator's agent name and the two commands to watch it (`herdr agent focus <name>`, `herdr-review status latest`), then ends its turn. The run continues in the orchestrator's tab.

### `auto-decide` — stop answering

`/herdr-review:auto-decide` (Grok: `/auto-decide`, Codex: `$herdr-review:auto-decide`) switches the current run of the repository you are in to automatic decisions and wakes the orchestrator. One-way; it also settles the drift question. Answering «авто» in the orchestrator's tab does the same.

### The CLI

```bash
herdr-review launch --preset default --description "what was implemented" --plan docs/plan.md
herdr-review launch --reviewers codex,grok --orchestrator claude-opus --fixer claude-opus --base develop --autodecide --layout grid
herdr-review status                 # the latest run of the repository you are in
herdr-review status <run dir>       # any run
herdr-review profiles               # the validated config, secrets omitted
```

`launch --help` lists every flag (`--no-autodecide`, `--focus`, …); `--json` on any command gives machine-readable output. `herdr-review run …` is what the orchestrator calls during the run; you never need it.

### During the run

- **Tabs:** `rv-<run_id>: orch` (the orchestrator), `rv-<run_id>: <profile>` per reviewer, `rv-<run_id>: fixer` once there is something to fix. Labels carry the state: ` ⏳` working, ` ✓` done, ` ✗` failed, ` ❓` stuck on a dialog or waiting for you.
- **The orchestrator's tab** shows the progress, the classification of every finding (AUTO — fixed by the fixer; DISPUTED — decided one at a time; DISMISSED — false positive, with a reason), and the final report.
- **A disputed issue without autodecide:** the tab turns ` ❓` and a herdr notification arrives. The orchestrator has written its analysis with variants and a recommendation; answer in that tab with a variant letter, a variant of your own, «не исправлять», «стоп» (defer the rest) or «авто» (the orchestrator decides the rest).
- **Fixes** land on your branch as commits `review: …` and `review(auto-decide): …`; the fixer commits only the files it changed, so your own uncommitted work in other files stays where it was.
- **The end:** a «готово» notification and `report.md` in the run directory, `~/.local/state/herdr-review/runs/<project>/<timestamp>-<run_id>/` — next to `status.json`, `reviews/<profile>.md`, `issues.md`, `fix-*.md` and `runner.log`. `latest` there points at the newest run of that repository.

## Troubleshooting

- «not inside herdr» — start the session in a herdr pane.
- «config not found» — copy `config.example.yaml` from the plugin directory as shown in Configure; the message names the path the plugin looked at.
- «Operation not permitted» from `herdr` in a Codex session — the Codex sandbox blocks the herdr socket. Approve the escalation, or start Codex with `--sandbox danger-full-access`.
- «orchestrator failed to start … Tab … is left open» — open that tab; a login or dialog is waiting. Resolve it and run the printed `herdr agent prompt …`.
- «no runs for this repository» from `status` — `latest` is per repository. Run it from the repository under review, or pass the run directory.
- A reviewer shows ` ✗` — `herdr-review status` gives the reason and `status.json` the last screen.
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
