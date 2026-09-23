# A launcher that asks no questions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/herdr-review:review` never asks the owner about uncommitted files or startup dialogs: `launch` decides the scope of the change and protects the owner's files, and claude agents start without Claude Code's MCP approval dialog.

**Architecture:** `launch` resolves the scope (`commits` or `worktree`) from git state, records the owner's uncommitted files in `<run_dir>/uncommitted.txt` and renders scope-specific reviewer steps; the fixer and orchestrator prompts protect those files. `dialogs.py` gains a table of known startup dialogs — it answers the trust dialogs of Claude Code, Codex and Grok and refuses the MCP dialog — and claude agents start with a session-only `--settings '{"enableAllProjectMcpServers": true}'`. Four agreed extras: a scratch directory per reviewer, the run's own commits in `finish`, fixer commit messages in the repository's style, and `herdr-review close`.

**Tech Stack:** Python ≥ 3.11 (stdlib + PyYAML), git, herdr 0.9.0 CLI, `unittest`, bats.

**Spec:** `docs/superpowers/specs/2026-09-23-launcher-without-questions-design.md`

## Global Constraints

- Python ≥ 3.11, stdlib plus PyYAML only; no new dependency.
- Code, comments, prompts, docs and commit messages in English. The launch summary, the `close` output and the orchestrator's report stay in Russian, as today.
- The plugin never edits the user's config file.
- `CLAUDE_MCP_SETTINGS = '{"enableAllProjectMcpServers": true}'`, added only for kind `claude` and only when the profile's args hold no `--settings` / `--settings=…`.
- Scopes: `auto` (default), `commits`, `worktree`.
- `UNTRACKED_READ_LIMIT_BYTES = 256 * 1024`, `BINARY_SNIFF_BYTES = 8192`, `UNCOMMITTED_INLINE = 50`, `UNTRACKED_INLINE = 100`, `MAX_DIALOGS = 3`, dialog wait `30000` ms.
- Run-directory files: `uncommitted.txt`, `scratch/<profile>/`, `fix-auto-commit.txt`, `fix-<n>-commit.txt`.
- Fixer commits carry no trailer and never mention the review, the reviewers or herdr-review.
- Stage files by exact path in every commit. Never `git add -A` or `git add .`: the working tree holds the owner's untracked `docs/2026-09-23-launcher-without-questions-prompt.md`, which must stay untracked.
- Unit tests run from the repository root with `python3 -m unittest …`; bats with `bats tests/bats/<file>`; everything with `tests/run.sh`. Where the environment asks for build and test commands to go through a runner agent, delegate them; the commands stay as written.
- Do not push and do not open a pull request. Before a pull request, and only when the owner asks for one: `git rm -r docs/superpowers` and commit.

## Review Focus

- A git config that changes `git status` output (`color.ui=always`, `status.showUntrackedFiles=no`, UTF-8 paths): `uncommitted.txt` and the prompts must still hold a plain, complete list. Test: Task 1, `test_status_lines_survive_the_users_git_config`.
- Claude Code shows the trust dialog and then the MCP dialog in one start: the trust dialog gets its keys, the MCP dialog gets none, the agent fails with the reason. Test: Task 3, `test_mcp_dialog_after_the_trust_dialog_is_refused`.
- A claude profile that already passes `--settings=<file>` (the `=` form): nothing is added and the owner's settings file stays in force. Test: Task 3, `test_a_profile_with_its_own_settings_is_left_alone` (unit and launch).
- More than 50 uncommitted entries: the reviewer prompt stops at 50 and points at `uncommitted.txt`, which holds them all. Test: Task 4, `test_a_long_list_of_uncommitted_files_is_capped_in_the_prompt_and_complete_on_disk`.
- `close` on a run whose tabs the owner partly closed by hand: those count as already closed, the rest close, exit 0; a real herdr failure exits 1. Tests: Task 8, `test_a_tab_closed_by_hand_is_not_an_error` and the bats `close` test.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `herdr_review/gitutil.py` | git facts: HEAD, committed changes, status lines, untracked files with skip marks | 1, 4 |
| `herdr_review/config.py` | `settings.scope` | 2 |
| `herdr_review/scope.py` (new) | what the change is: scope resolution, reviewer-prompt blocks, the orchestrator's scope line | 2, 6 |
| `herdr_review/dialogs.py` | known startup dialogs, their answers, the MCP refusal, the claude start args | 3 |
| `herdr_review/launch.py` | scope, `uncommitted.txt`, `head`, scratch directories, start args, prompt rendering, summary fields | 3, 4, 6 |
| `herdr_review/runner.py` | refusal → `failed`, commits since `head`, scratch cleanup, `close` | 3, 7, 8 |
| `herdr_review/cli.py` | `--scope`, the scope lines of the summary, `close` | 4, 8 |
| `prompts/scope-commits.md`, `prompts/scope-worktree.md` (new) | reviewer steps 1–2 per scope | 2 |
| `prompts/reviewer.md` | scope steps, hard rules, scratch directory | 4 |
| `prompts/fixer-auto.md`, `prompts/fixer-decision.md` | protection of uncommitted files, commit messages in the repository's style | 5 |
| `prompts/orchestrator.md` | run facts, dialogs, permissions, dismissals, verification by hash, report | 6 |
| `skills/review/SKILL.md`, `README.md`, `config.example.yaml` | the behavior of each task, documented in that task | 3, 4, 5, 7, 8 |
| `CHANGELOG.md`, `tests/SMOKE.md` | release notes, real-herdr checklist | 9 |

---

### Task 1: git helpers for the scope of a review

✅ Done — see commit(s): `ddcca8d`, `733f4cf`

---

### Task 2: the scope policy and its reviewer prompt fragments

✅ Done — see commit(s): `2c3b8f4`

---

### Task 3: startup dialogs — answer the trust dialogs, never the MCP one

✅ Done — see commit(s): `0f7f4ec`

---

### Task 4: launch reviews the branch's commits and leaves uncommitted work alone

✅ Done — see commit(s): `0337a41`, `f771dba`

---

### Task 5: the fixer protects uncommitted files and writes commits in the repository's style

✅ Done — see commit(s): `76caf4f`, `1ce4729`

---

### Task 6: the orchestrator knows the scope, the user's files and the dialogs

✅ Done — see commit(s): `7712459`

---

### Task 7: finish lists the run's own commits and removes scratch

✅ Done — see commit(s): `516292f`

---

### Task 8: `herdr-review close`

✅ Done — see commit(s): `77ecf20`

---

### Task 9: changelog, smoke checklist, final check

✅ Done — see commit(s): `ae9d9a6`
