# Changelog

All notable changes to herdr-review will be documented here.

## [Unreleased]

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
  longer takes `.git/index.lock` from under the owner's `git commit`.
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
