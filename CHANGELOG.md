# Changelog

All notable changes to herdr-review will be documented here.

## [Unreleased]

### Added
- `settings.scope`, `launch --scope` and the skill argument `scope=`: the change under review is the
  branch's commits (`git diff <merge-base> HEAD`), or the working tree when nothing is committed. The
  owner's uncommitted edits and untracked files stay out of a review of the commits and are listed in
  `<run_dir>/uncommitted.txt`; the launch summary says so. In the working-tree scope the reviewers get
  the untracked files as a list, with binary and large ones marked to skip, and the fixer commits
  nothing: every fix is applied and left for the owner to commit.
- `herdr-review close`: closes every tab and pane of a finished run; `--force` for one in progress.
  It runs only from the herdr session the run was launched in, and leaves open a tab whose ID now
  names another tab.
- A scratch directory per reviewer, `<run_dir>/scratch/<profile>/`, for its experiments; `run finish`
  deletes it.
- The runner answers the trust dialogs of Codex and Grok, as it answers Claude Code's.

### Changed
- Claude Code's MCP approval dialog is never answered: every answer, Esc included, is saved into the
  repository's `.claude/settings.local.json`. Profiles of kind `claude` start with
  `--settings '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'` unless they pass
  their own `--settings`, so the dialog does not appear and the agents get every MCP server; an agent
  that still stops at it leaves the run with the reason. The empty `attribution.commit` keeps Claude
  Code's trailer out of fix commits.
- The fixer never deletes, moves, renames or commits the owner's uncommitted files; a fix inside one
  is applied and left uncommitted. In a review of the commits, the orchestrator dismisses findings
  about the owner's uncommitted files outside the change.
- Fix commits follow the repository's own commit style instead of fixed `review:` subjects; the
  orchestrator verifies them by hash.
- `run finish`, `status` and the report list the commits made during the run, not every commit since
  the merge-base.
- The launcher never asks about untracked files or startup dialogs; `--plan` is documented as a path
  or free text.
- `config.example.yaml`: every profile offers the agent's auto mode next to yolo, one of the two
  commented out; auto mode is the active default (`--permission-mode auto` for claude and grok,
  `--approve-for-me` for codex, plus `--add-dir` for the run directory). Checked with a real run:
  reviewers, orchestrator and fixer all in auto mode.
- README: the auto mode / yolo table in Configure, the `args` rule, the ` ❓` entry in Troubleshooting.

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
