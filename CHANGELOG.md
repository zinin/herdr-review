# Changelog

All notable changes to herdr-review will be documented here.

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
