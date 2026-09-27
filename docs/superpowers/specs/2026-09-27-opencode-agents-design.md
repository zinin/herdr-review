# OpenCode agents — design

Date: 2026-09-27. Branch: `feat/opencode`. The owner approved each section of this design in brainstorming;
this document records it for the implementation plan.

## Summary

A profile may name any herdr agent kind, but a profile of kind `opencode` cannot work today: the OpenCode 2 TUI has
no `--model`, a profile's OpenCode config reaches only a server started with it, and every agent of a run reads and
writes the run directory, which lies outside the repository, where OpenCode asks first. herdr-review will start
every opencode agent with `--standalone` and a session-only config that allows the run directory and denies the
question tool, as it already starts every claude agent with session-only settings. The owner's profile names the
model in `OPENCODE_CONFIG_CONTENT`. The orchestrator learns OpenCode's permission dialog, and the prompts cover a
shell tool that stops a command after a timeout of its own.

The owner's case: a MiMo model on the Xiaomi Token Plan, which misbehaves under Grok, joins reviews through
OpenCode. The reviewer role gets a real run; the orchestrator and fixer roles stay open to opencode in code but
unverified, as they are today for codex and grok.

## Background: OpenCode 2

Checked on this machine with OpenCode 2.0.18 and herdr 0.9.1, and read in the OpenCode source at tag `v2.0.18`
(paths below are under `packages/`).

- **herdr.** `herdr agent start --kind opencode` is supported. herdr's opencode integration (installed here, version
  12) is a lifecycle authority: a plugin in the pane's TUI reports `idle`, `working` and `blocked`, and a pending
  permission request, a pending form or a failed execution keeps the pane `blocked`. OpenCode Mini runs no TUI
  plugin, so it reports nothing.
- **Flags.** The TUI takes `--standalone`, `--server`, `--auto`, `--continue`, `--session` and `--prompt`; `--model`
  and `--agent` belong to `opencode run` and `opencode mini`. `opencode --model x` exits with
  `Unrecognized flag: --model in command opencode`.
- **Servers.** Without `--standalone` the TUI talks to a shared background server, `opencode serve --service`. The
  first TUI that finds none starts it with its own full environment (`client/src/service-contender.ts:18-22`), and
  the server outlives that TUI. With `--standalone` the TUI starts a private `opencode serve --stdio --port 0` as its
  child; the child inherits the TUI's environment and exits with it.
- **Config documents.** The server reads `OPENCODE_CONFIG` (a file) and `OPENCODE_CONFIG_CONTENT` (inline JSON) from
  its own environment, once, at start (`cli/src/server-process.ts:114-115`). A variable set for a later TUI never
  reaches a shared server that already runs: with `OPENCODE_CONFIG_CONTENT='{"model":"…/mimo-v2.6-flash"}'` and no
  `--standalone`, the TUI stayed on the owner's most recent model, Pro; with `--standalone` it showed Flash.
- **Precedence.** Global `opencode.json` < `OPENCODE_CONFIG` < the project's config files < `OPENCODE_CONFIG_CONTENT`
  (`core/src/config.ts:231-236`, `core/src/config/discovery.ts:65-66`). A single-value key such as `model` comes from the last document that sets it.
  Permission rules of all documents are concatenated, and the last rule that matches decides.
- **Tool environment.** Shell commands run with the session environment the TUI sends: its whole `process.env` but
  the two password variables (`cli/src/env.ts:15-22`, `core/src/shell.ts:254-272`), on the shared server as well.
  A subagent session the TUI does not display gets the server's environment instead.
- **Default rules.** Everything is allowed except `external_directory` and reads of `*.env` and `*.env.*`, which ask.
  v2 has no `doom_loop`. `--auto` works in the TUI: it answers `once` to every request that no rule denies.
- **external_directory.** read, write, edit, patch, glob and grep check it; the shell tool checks only its working
  directory and the targets of `cd`-like commands, not the paths in a command's arguments. A resource is a
  directory followed by `/*`, `*` crosses `/`, and a pattern without the trailing `/*` never matches
  (`core/src/file-access.ts:99-127`, `core/src/util/wildcard.ts`).
- **The permission dialog.** `△ Permission required` over the buttons `Allow once` · `Always allow` · `Reject`, the
  cursor on `Allow once`. ←/→ move, `enter` confirms, `esc` rejects; no letter selects a button. OpenCode saves
  `Always allow` for the project in its own database, for every later session. A rejection ends the agent's turn; for
  a subagent, `esc` first opens `△ Reject permission` with a text box, and `enter` confirms it.
- **The question tool** opens a `Questions` form, and the agent is `blocked` until it is answered; `esc` dismisses it
  and ends the turn, and `--auto` does not answer it. A denied permission returns `Permission denied: <permission>`
  to the model without any dialog (`core/src/permission.ts:70-78`).
- **Shell timeout.** 120 s unless the call passes its own `timeout` in milliseconds, with no maximum, or runs in the
  background. On timeout the command's process group gets SIGTERM, then SIGKILL 3 s later.
- **Startup.** No trust dialog, onboarding or model picker. A model missing from the catalog shows the toast
  `Model unavailable` and leaves the prompt unsent. Without a connected provider, the first prompt opens a
  `Connect an integration` dialog.
- **Provider errors.** OpenCode retries rate limits and server errors for up to about 84 s (`⚠ Retrying …`). An
  authentication or quota error ends the turn with a red `Error: …` line, which herdr's plugin reports as `blocked`.
- **Nothing else to handle.** OpenCode adds no trailer to a commit, never reads `.mcp.json` and shows no MCP dialog;
  the 2.0.18 core has no LSP client. `mimo-v2.6-pro` and `mimo-v2.6-flash` always reason and have no variants.

## Problem

With a plain profile of kind `opencode`:

1. **The model.** `--model` in `args` stops the TUI at start. `OPENCODE_CONFIG_CONTENT` in the profile's `env`
   reaches only a server started with it, so on the shared server the agent silently works on the owner's most
   recent model.
2. **The owner's OpenCode.** When no shared server runs, the first agent of a run starts it, and the server keeps
   that agent's environment — its `OPENCODE_CONFIG_CONTENT`, its `HERDR_REVIEW_AGENT` — for the owner's own sessions
   after the run.
3. **The run directory.** A reviewer asks before it reads its prompt file, before it writes its review and before it
   works in `scratch/`. The orchestrator answers every such dialog without knowing OpenCode's keys: `y` does nothing
   there, and `Always allow` would change the owner's OpenCode permissions for that project for good.
4. **Questions.** A `Questions` form blocks the agent, and `herdr agent prompt` refuses a blocked agent
   (`agent_blocked`), so the orchestrator's rule "answer with `herdr agent prompt`" cannot reach it.
5. **Timeouts.** The shell tool stops a call after 120 s. A build through `herdr-review exclusive` dies with the call,
   since the wrapper stops its command on SIGTERM, and the orchestrator's `run wait` (up to `checkin_sec`, 300 s by
   default) is cut short.

## Goals

1. A profile of kind `opencode` takes part in a review in auto mode on the model it names, with no dialog left for a
   human.
2. A run changes nothing in the owner's OpenCode setup: no agent starts or configures the shared server, and no
   `Always allow` is saved.
3. The build queue attributes an opencode agent's heavy commands to that agent, as it does for every kind.
4. A profile stays kind + args + env; herdr-review adds only what the run needs, as it does for claude.
5. A real run verifies the reviewer role. The orchestrator and fixer roles are allowed and unverified.

## Design

### 1. `herdr_review/kinds.py`

A new module: what an agent of a given kind starts with beyond its profile. `startup_args` and
`CLAUDE_SESSION_SETTINGS` move here from `dialogs.py`, which keeps recognising and answering dialogs.

- `startup_args(kind, args)`: `claude` as today. `opencode`: `["--standalone", *args]`, unless an argument is
  `--standalone` or `--server`, or starts with `--standalone=` or `--server=`. Any other kind: `args` unchanged.
- `startup_env(kind, run_dir)`: `{"OPENCODE_CONFIG": "<run_dir>/opencode.json"}` for `opencode`; `{}` otherwise.
- `opencode_config(runs_dir)`: the session config.

  ```json
  {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
      "external_directory": {"<runs_dir>/*": "allow"},
      "question": "deny"
    }
  }
  ```

  `<runs_dir>` is the resolved absolute path that `run.json` records. The file uses the `permission` object of the
  published docs, which OpenCode 2 converts to its own rules.

**Why `--standalone`.** The profile's `OPENCODE_CONFIG_CONTENT` and herdr-review's `OPENCODE_CONFIG` reach only a
server started with them (Problem 1). An agent never starts, restarts or configures the owner's shared server
(Problem 2). Subagent sessions get the agent's environment too. The private server exits with its TUI, so closing a
tab leaves no process behind.

**Why a file through `OPENCODE_CONFIG`.** The profile already uses `OPENCODE_CONFIG_CONTENT` for the model. Two
documents keep herdr-review's rules and the profile's settings apart without merging JSON. The rules of both apply,
and where they cover the same path, the profile's, read later, win. The project's own OpenCode config is read
between the two and can override the run-directory rule: that project's agents then ask, and the orchestrator
answers `Allow once` by its rules for reads and for writes to a reviewer's own files.

**Why `question: deny`.** The form blocks the agent, and nobody sits at its tab. The reviewer and fixer prompts
forbid questions anyway, and the orchestrator asks the owner by ending its turn. A denied call returns
`Permission denied: question` to the model, which goes on with its task. One config serves every role.

### 2. Launch and runner

- `launch` writes `<run_dir>/opencode.json` next to `run.json` when any agent of the run is of kind `opencode`: a
  reviewer whose executable is in PATH, the orchestrator or the fixer. A run without one gets no file.
  `_profile_spec` still records `startup_args` in `run.json`, so `--standalone` shows there as `--settings` does.
- The orchestrator's tab environment: the run's own variables (`HERDR_REVIEW_RUN`, `GIT_OPTIONAL_LOCKS`, the config
  variables passed through, the `${VAR}` references), then `startup_env`, then the profile's `env`, then
  `HERDR_REVIEW_AGENT`.
- `Runner._agent_env(name, profile, kind)` builds the reviewers' and the fixer's environments in the same order, for
  tabs and for the grid.
- A profile whose `env` sets `OPENCODE_CONFIG` replaces herdr-review's file, because the profile's `env` comes later.
  Its own file must then allow the run directory, or its agents ask. README says so, as it does for a claude profile
  with its own `--settings`.
- Nothing else depends on the kind. OpenCode shows no startup dialog, so `resolve_startup_dialog` has nothing to
  answer, and `mcp_check` reads the screen once after the start and finds no MCP dialog.

### 3. Config validation

An `env` value named `OPENCODE_CONFIG_CONTENT` must parse, after `${VAR}` expansion, as a JSON object; otherwise the
validator reports `profiles.<name>.env.OPENCODE_CONFIG_CONTENT: not a JSON object (<reason, line, column>)` and
`launch` stops, as for any other config problem. JSON inside YAML is easy to mistype, and OpenCode's config loader
dies on a document it cannot parse (`parseInfo(…, "OPENCODE_CONFIG_CONTENT")` under `Effect.orDie`,
`core/src/config.ts`), so the agent would fail in its tab, far from the cause. The message never echoes the value,
which may hold a key. The check applies to every kind, since the variable is OpenCode's own.

### 4. Prompts

The prompts stay the same for every kind; the orchestrator's already names the dialogs of every CLI, whatever the
run holds.

- `prompts/orchestrator.md`, Phase 2, the `blocked` cases:
  - **OpenCode's permission dialog** (`△ Permission required`; `Allow once` · `Always allow` · `Reject`, the cursor
    on `Allow once`): confirm with `enter`; refuse with `esc`, then `enter` when a `Reject permission` box opens.
    Never select `Always allow`: OpenCode keeps it for the project in every later session. No letter selects a
    button, so the general "`y` then `enter`" does not apply. A refusal ends the agent's turn, so the prompt that
    follows a refusal reaches it.
  - **OpenCode's `Questions` form**: `esc` dismisses it and ends the agent's turn; then answer with
    `herdr agent prompt <name> "<answer>"`, which herdr refuses while the form is open.
  - **API errors**: OpenCode's is a red `Error: …` line under the agent's reply; like any quota or API error, it
    means `run fail`.
  - **Red flags**: a new row — selecting `Always allow` in OpenCode's dialog → `enter` on `Allow once`, or `esc`.
  - **`run wait`**, Phase 2 step 1: the call lasts up to `{CHECKIN_SEC}` s; a shell tool that stops a command after
    a timeout of its own needs a longer one for this call.
- `prompts/exclusive.md`, which the reviewer and the fixer get: a new bullet — when your shell tool stops a command
  after a timeout of its own (OpenCode's: 2 minutes unless the call sets one), give the call a timeout that covers
  the command and up to 60 s of waiting for the turn; a call the tool stops takes the command down with it, and the
  build is lost.

### 5. Documentation

- **README.**
  - Requirements: OpenCode 2.x for kind `opencode`, and herdr's opencode integration
    (`herdr integration install opencode`); without it herdr reads the agent's state from its screen, less exactly.
  - Configure: an opencode profile in the example; a row in the mode table — auto mode: no flag, OpenCode's own rules
    allow every command and edit without asking and ask about paths outside the repository, and herdr-review allows
    the run directory; yolo: `--auto`. The `args` and `env` rules name `--standalone` and `OPENCODE_CONFIG`.
  - An "OpenCode" paragraph after "MCP servers": why `--standalone`; what `opencode.json` holds; the model in
    `OPENCODE_CONFIG_CONTENT` because the TUI has no `--model`; a profile with its own `OPENCODE_CONFIG`; the
    orchestrator never selects `Always allow`; the shell tool's 2-minute timeout.
  - Troubleshooting: an opencode agent stuck right after its prompt (`Model unavailable`: check the id with
    `opencode models`); `Unrecognized flag: --model`; OpenCode 1.x; ` ❓` with `Error: …` (quota or authentication).
  - The "Checked with" sentence gains OpenCode after the real run.
- **`config.example.yaml`**: a `mimo` profile with comments, outside the `default` preset, which would otherwise
  demand a MiMo subscription from every reader; the header comment names OpenCode's modes.
- **CHANGELOG** (Unreleased) and a new item in `tests/SMOKE.md`.

## Testing

- **Unit.**
  - `tests/unit/test_kinds.py`, new: the `startup_args` tests move here from `test_dialogs.py`, plus `opencode` with
    no flag, `--standalone`, `--server URL` and `--server=URL`; `startup_env` per kind; `opencode_config` gives the
    absolute `<runs_dir>/*` rule and `question: deny`.
  - `test_launch.py`: `opencode.json` is written only when the run has an opencode agent and holds the resolved
    `runs_dir`; an opencode orchestrator's tab gets `OPENCODE_CONFIG` before the profile's `env`; a profile's own
    `OPENCODE_CONFIG` wins; `run.json` records `--standalone`, and no `--standalone` for a profile with `--server`.
  - `test_runner_start.py`: an opencode reviewer, in tabs and in the grid, and an opencode fixer get
    `OPENCODE_CONFIG` and `HERDR_REVIEW_AGENT`, and start with `--standalone`.
  - `test_config.py`: a JSON object passes; broken JSON, an array or a string is an error that names the profile and
    the key but not the value.
  - `test_prompts.py`: the orchestrator names `Allow once`, `Always allow`, `Reject permission`, `Questions` and
    `Error:`; the heavy-command rules and the `run wait` step mention the tool's own timeout.
- **bats with the fake herdr.** One case with an opencode reviewer: the log holds `agent start … --kind opencode …
  -- --standalone` and `tab create … --env OPENCODE_CONFIG=<run_dir>/opencode.json`, and the file exists.
- **A real run in herdr** on this branch. The owner adds a `mimo` profile to their own config
  (`OPENCODE_CONFIG_CONTENT: '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'`); reviewers `mimo` and one more,
  orchestrator and fixer `claude-opus`. Expected: the MiMo tab's status line names MiMo-V2.6-Pro; its review is
  collected; while it runs the tests, `herdr-review status` names it as the build queue's holder; no dialog waits for
  a human; OpenCode's permission table gains no `Always allow` entry for the repository.

## Limitations

- OpenCode 1.x is not supported: it has no `--standalone`.
- A profile with `--server <url>` starts without `--standalone`, and neither its `env` nor herdr-review's file reaches
  that server.
- A project's own OpenCode config can override the run-directory rule; its agents then ask, and the orchestrator
  answers.
- Auto mode is weaker than Claude Code's: OpenCode has no classifier, and its default rules allow every command and
  edit inside the repository and never check a path in a command's arguments (`cat ~/.ssh/id_rsa` runs without a
  question). A reviewer that writes into the repository shows only as drift, as under yolo.
- A mistyped model id shows only at the first prompt: the prompt stalls, and the orchestrator takes the agent off.

## Out of scope

- A `model` key in profiles, or turning `--model` into config.
- OpenCode 1.x.
- Session configs per role, such as edits denied to reviewers.
- Real runs with an opencode orchestrator or fixer.
- The owner's config: the plugin never edits it; the owner adds the `mimo` profile.
