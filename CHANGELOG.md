# Changelog

All notable changes to herdr-review will be documented here.

## [0.6.0] - 2026-10-10

### Fixed
- A reviewer or the fixer that herdr reports idle, done or unknown while it waits for its own background work stays
  `working`: its command in the build queue, for every agent kind, and for grok any background task or subagent that
  grok's status line shows above its input (trusted alone for 30 minutes at most). `collect` looks at such a reviewer
  again before it takes its review, re-prompts it or fails it, so a reviewer waiting for its own tests is neither
  collected on a draft, nor re-prompted, nor failed.
- The wrapper runs nothing for an agent whose CLI exited (`gone`), nor once its review is over (`finished`,
  `aborted`), like for an agent `run fail` took off, also when that happens just as it takes its turn: a background
  wrapper still waiting for its turn no longer starts a build after the review, in a `scratch/` already removed.
- An agent whose CLI exited (`gone`) no longer leaves its command holding the build queue up to its `--timeout`:
  the runner stops it, as `run fail` does. A `close --force` that leaves a tab open, and with it the run in
  progress, makes the agents whose tabs did close `gone` too.
- `close --force` stops once more, after it has set the run `aborted`, a wrapper of the run that took the build
  queue while the tabs closed, and reports that stop apart from the first one (`exclusive_stopped_while_closing`).

### Added
- `herdr-review exclusive` registers each wrapper of a review in `<run_dir>/wrappers/<pid>-<token>.json` while it
  waits for its turn and while it runs its command, and keeps that entry live by holding a lock on
  `<pid>-<token>.lock`, so it works for an agent whose CLI runs its commands in a PID namespace of their own, such as
  Codex.
- `background`: what an agent that herdr shows idle still waits for, in `run wait`, `run prompt`, `start-reviewers`,
  `start-fixer`, `status.json` and `herdr-review status` («фон:»).

### Changed
- The heavy-command rules let an agent start the wrapper through its shell tool's own background mode, never with a
  shell `&`, and ask it to wait for every command it started before it replies DONE. The orchestrator leaves an
  agent with `background` alone and never presses a key in a grok tab that shows a running background task or a
  queued prompt, except to answer a `blocked` dialog.

## [0.5.0] - 2026-10-04

### Added
- One automatic restart per reviewer of kind `codex` and run after a successful startup update followed by a
  confirmed process exit. The replacement keeps the original pane, working directory, environment, arguments
  and review prompt. `update_restarts` records the persistent one-shot budget separately from prompt retries.
- A persisted, launch-specific 600-second deadline for running startup installers. `wait` and `collect` keep
  them pending without sending extra input; an expired, still-live, non-blocked installer becomes `failed`
  with its masked diagnostic screen. Genuine permission dialogs retain their existing handling.
- Launch-generation fencing and a per-reviewer guard across restart claims, replacement startup and every Codex
  prompt submission. Concurrent commands defer while another runner owns startup or input, reporting
  `codex_restart_pending: true`; `run fail` stays available for explicit removal.

### Changed
- Update provenance compares masked baselines and evidence from up to 10,000 available `recent-unwrapped` pane
  history rows, independently of the 40-row `last_screen` diagnostics. Exact prefix or unique retained-tail
  alignment is required; unavailable, wrong-generation, discarded or ambiguous history follows ordinary failure
  handling. Confirmed sessions and manual revival close startup recovery; a second update exit gets no restart.
- Codex prompt delivery revalidates its original generation, current lifecycle and retry quota inside the shared
  startup guard. Deferred or superseded commands send no input and consume no retry quota.

### Fixed
- Overlapping status saves retain current lifecycle state, successful prompt delivery, owned retry increments,
  collect quotas and the spent restart budget.
- Delayed Herdr responses are re-read after concurrent startup progress, keeping an active reviewer pending.
- Accepted failures survive restart claims, startup, state writes and automatic prompt completion. A rejected
  `run fail` reports a conflict before stopping a replacement's queue holder. Explicit terminal revival through
  `run prompt` preserves diagnostics until its result.
- Observers cannot mark the newly claimed generation `gone` before its replacement has started, or submit an
  extra first review prompt. Startup guards release on completion, errors or process exit; abandoned claims
  keep their spent restart budget and resume ordinary failure handling.
- Successful updates with verbose installer output are recognized while old success messages remain excluded
  from the current launch's evidence.

## [0.4.0] - 2026-09-28

### Added
- Agents of kind `opencode` (OpenCode 2). herdr-review starts each with `--standalone`, a private OpenCode server
  that gets the agent's environment, with `OPENCODE_CONFIG` naming `<run_dir>/opencode.json`, a session-only config
  that allows the run directory and denies the question tool and reading `.env` files other than `.env.example`,
  and with `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so the repository's own OpenCode config, which could override those
  rules, and its root `AGENTS.md` stay out; a profile may set it to `0`. The profile names its model in
  `OPENCODE_CONFIG_CONTENT`. The validator requires that value, in a profile of any kind, to be a strict JSON
  object: comments, trailing commas, `NaN`, `Infinity` and an empty value are refused.

### Changed
- A `--plan` file outside the repository is copied into `<run_dir>/plan/`, and the prompts name the copy: every
  agent may read it there, while elsewhere an agent may have to ask and an opencode orchestrator has nobody to answer.
- The orchestrator answers OpenCode's permission dialog with `Allow once` or `esc`, never `Always allow`, dismisses
  its question form before it answers, interrupts a stuck OpenCode agent with two `esc`, and takes an agent with a
  red `Error: …` line off the run.
- The heavy-command rules and `run wait` tell an agent whose shell tool stops a call after a timeout of its own to
  give the call a longer one.

### Fixed
- A profile's `${VAR}` expands to its launch value for the reviewers and the fixer of every kind, even when the
  orchestrator's tab sets that variable to a value of its own: its profile's `env`, or an opencode orchestrator's
  `OPENCODE_CONFIG`.

## [0.3.0] - 2026-09-26

### Added
- `herdr-review exclusive -- <command>`: runs a heavy command — a build, tests, a dependency install, a server — only
  while it holds the machine's build queue (`<runs_dir>/exclusive.lock`), one at a time among every review on the
  machine. It waits up to 60 s for its turn (`--wait`) and exits 75 without running the command when the turn does
  not come; a command still running after 30 minutes (`--timeout`) is stopped, with every process under it, and the
  wrapper exits 124. The reviewer and fixer prompts require it for every heavy command, the fixer's commits included,
  since a commit's hooks may build or test; the orchestrator refuses a heavy command run without it when an agent's
  CLI asks.
- `herdr-review status` names who holds the build queue (`exclusive` in `--json`).

### Changed
- `run fail` stops the failed agent's command that still holds the build queue, and the wrapper runs nothing more for
  that agent; `run finish` and `close` stop any command of their run that still holds it.
- Every agent starts with `HERDR_REVIEW_AGENT` naming it, and with `GIT_OPTIONAL_LOCKS=0`, so its `git status` no
  longer takes `.git/index.lock` from under the owner's `git commit`. The runner's own check of the working tree no
  longer takes it either: it compared the tree with `git diff`, which rewrites the index after a change to a file's
  stat alone.
- `run.json` records `runs_dir`.

## [0.2.0] - 2026-09-24

### Added
- `settings.scope`, `launch --scope` and the skill argument `scope=`: the change under review is the
  branch's commits (`git diff <merge-base> HEAD`), or the working tree when nothing is committed. The
  owner's uncommitted edits and untracked files stay out of a review of the commits and are listed in
  `<run_dir>/uncommitted.txt`; the launch summary says so. In the working-tree scope the reviewers get
  the untracked files as a list, with binary and large ones marked to skip, and the fixer commits
  nothing: every fix it applies is left for the owner to commit, as the launch summary says, and a fix
  that would delete, move or rename one of the owner's uncommitted files is still skipped.
- `herdr-review close`: closes every tab and pane of a finished run; `--force` for one in progress.
  It runs only from the herdr session the run was launched in, and leaves open a tab whose ID now
  names another tab, listed as left open (`left_open` in `--json`) rather than as closed.
- A scratch directory per reviewer, `<run_dir>/scratch/<profile>/`, for its experiments; `run finish`
  deletes it.
- The runner answers the trust dialogs of Codex and Grok, as it answers Claude Code's.

### Changed
- Claude Code's MCP approval dialog is never answered: every answer, Esc included, is saved into the
  repository's `.claude/settings.local.json`. Profiles of kind `claude` start with
  `--settings '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'` unless they pass
  their own `--settings`, so the dialog does not appear and the agents get every MCP server; an agent
  that still stops at it leaves the run with the reason, also where a narrow pane of the grid layout
  wraps the dialog's text. The empty `attribution.commit` keeps Claude Code's trailer out of fix
  commits.
- The fixer never deletes, moves, renames or commits the owner's uncommitted files; a fix inside one
  is applied and left uncommitted. In a review of the commits, the orchestrator dismisses findings
  about the owner's uncommitted files outside the change.
- Fix commits follow the repository's own commit style instead of fixed `review:` subjects; the
  orchestrator verifies each one by hash and subject.
- `run finish`, `status` and the report list the commits made during the run, not every commit since
  the merge-base. A merge commit that brings the base in during the run is listed alone, without the
  base's commits; a fast-forward to the base still lists them. When the branch was rewritten during
  the run (a rebase, an amend, a reset or a branch switch), git can no longer tell which commits are
  the run's, and they list the fixer's commits as the orchestrator noted them from its reports.
- Drift is worded as a change of the working tree, not as a reviewer's doing: `status` says «рабочее
  дерево изменилось во время ревью», and `drift_status` holds only the `git status --short` lines
  that are new since the launch, so the owner's files uncommitted then are not reported as changed.
  A line of the launch whose path no line shows any more is listed as `gone since launch: …`:
  uncommitted work reverted, stashed or committed during the review, which the orchestrator names as
  such. A path whose status alone changed, staged say, is listed by its new line only. When no line
  is new or gone, `drift_status` says so in one line and names what may have changed: a file inside
  an untracked directory of the launch, a file already uncommitted then, or a commit.
- The launcher never asks about untracked files or startup dialogs; `--plan` is documented as a path
  or free text.
- `config.example.yaml`: every profile offers the agent's auto mode next to yolo, one of the two
  commented out; auto mode is the active default (`--permission-mode auto` for claude and grok,
  `--approve-for-me` for codex, plus `--add-dir` for the run directory). Checked with a real run:
  reviewers, orchestrator and fixer all in auto mode.
- README: the auto mode / yolo table in Configure, the `args` rule, the ` ❓` entry in Troubleshooting.

### Fixed
- An untracked file git could not hash stopped the run: one nobody can read (a `chmod 000` file, a
  root-owned file from a Docker bind mount), a name that starts with `"`, or a file deleted meanwhile
  made `run start-reviewers` fail before any agent started, and `collect` fail during the run. The
  drift check now reads the untracked files itself, with no clean filter of `.gitattributes`, and
  describes a file it cannot read by its size and mtime.
- An untracked file whose name is not UTF-8 or holds a CR, such as a Latin-1 `café.py` or macOS's
  `Icon\r`, was dropped from the working-tree scope: the reviewers' list, `untracked.txt` and the
  launch summary's count missed it, and so did the drift check. Such a name is now listed quoted as
  git quotes a path: `"caf\351.py"`, `"Icon\r"`.

## [0.1.0] - 2026-09-10

### Added
- `herdr-review launch`: preflight, run directory, orchestrator tab and agent inside herdr.
- `herdr-review run …`: start-reviewers, wait, prompt, fail, collect, start-fixer, notify, phase, autodecide, finish.
- `herdr-review status`, `herdr-review profiles`.
- Prompts: orchestrator protocol (six phases), reviewer prompt, fixer task skeletons.
- `skills/review/SKILL.md` launcher for Claude Code, Grok, Codex, OpenCode and other Agent Skills hosts.
- `skills/auto-decide/SKILL.md`: switch a running review to automatic decisions without leaving your own
  session; «авто» answered in the orchestrator's pane does the same.
- Layouts: one tab per agent (default) or a pane grid in the orchestrator's tab.
- README: install and launch instructions per host (Claude Code, Grok, Codex, OpenCode), the Codex
  sandbox caveat, where `config.example.yaml` lives after a marketplace install.
