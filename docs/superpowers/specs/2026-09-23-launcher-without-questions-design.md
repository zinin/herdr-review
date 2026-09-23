# A launcher that asks no questions — design

Date: 2026-09-23. Branch: `feat/launcher-without-questions`.

## Problem

`/herdr-review:review` asked the owner two kinds of questions that nobody designed:

1. **What to do about uncommitted and untracked files.** In run `hrcl4s` (vpn-director, 2026-09-23)
   the working tree held about 700 KB of the owner's own files unrelated to the change — an old
   408 KB diff, 19 old session prompts, two session-transfer notes, a test script — and an
   uncommitted edit to the tracked `.claude/settings.local.json`. Step 2 of `prompts/reviewer.md`
   tells every reviewer to read every untracked file as part of the change. `launch.py` only warns
   about a dirty tree and `SKILL.md` says nothing, so the launcher invented a question of its own.
   With autodecide on, a finding such as "remove the stray `test_exit.sh`" could have reached the
   fixer, which would then have deleted the owner's file.
2. **How to answer an agent's startup dialog about MCP servers.** Claude Code stops at a dialog
   when a project's `.mcp.json` names servers the owner has not decided on. `dialogs.py` answers
   only the trust dialog; for anything else `SKILL.md` §6 says "resolve it with send-keys" without
   saying which answer, so the launcher asked the owner.

The four questions the skill asks on purpose — reviewers, orchestrator, fixer, autodecide — stay.

## Goals

- No question to the owner about either topic. The tool decides where it can act; `SKILL.md` and
  the orchestrator prompt carry explicit rules where an LLM acts. The launch summary says what was
  decided.
- Nothing in a run deletes, moves, rewrites or commits the owner's uncommitted work.
- Every claude agent gets the MCP servers of the project, of the user and of claude.ai, except
  those the owner disabled explicitly, and the run never changes the owner's MCP settings.
- Agreed in brainstorming, from the same run's observations: a scratch directory for reviewers'
  experiments, the run's own commits in `status` and the report, fixer commit messages in the
  repository's style, a `herdr-review close` command, and documentation that `--plan` accepts
  free text.

## Non-goals

- The plugin still never edits the user's config file.
- No separate input for rulings from earlier review rounds: `--plan` carries them as free text.
- No per-file check at `finish` that the owner's files are unchanged.
- No handling of gemini or opencode startup dialogs: none was observed.
- No version bump; `CHANGELOG.md` gets an Unreleased entry.

## Verified CLI behavior

Checked on 2026-09-23 in throwaway repositories under the session scratchpad, each CLI running in
a private tmux server: Claude Code 2.1.280, Codex 0.156.1, Grok 1.0.41, herdr 0.9.0.

Claude Code:

- Right after the trust dialog, Claude Code shows an approval dialog for every `.mcp.json` server
  that is neither enabled nor disabled in the project's settings. One server:
  `New MCP server found in this project: <name>` with the options "Use this MCP server",
  "Use this and all future MCP servers in this project" and "Continue without using this MCP
  server" (the cursor starts on the last), footer "Enter to confirm · Esc to cancel". Several
  servers: `<N> new MCP servers found in this project`, a multi-select with every server
  pre-checked, "Enable selected", footer "Space to select · Esc to reject all".
- **Every answer persists, Esc included**: Esc wrote `{"disabledMcpjsonServers": [...]}` into
  `<repo>/.claude/settings.local.json` in both variants. That file sits in the working tree; the
  owner's global gitignore hides it, but vpn-director tracks it. Killing the agent at the dialog
  writes nothing.
- `--strict-mcp-config` suppresses the dialog but removes every MCP server, user-scope servers and
  claude.ai connectors included.
- `--settings '{"enableAllProjectMcpServers": true}'` suppresses the dialog and writes nothing.
  `/mcp` then lists the project servers, the user servers (`~/.claude.json`) and the claude.ai
  connectors. A server the project settings disable explicitly (`disabledMcpjsonServers`) stays
  disabled.
- With two `--settings` flags the last one wins: `--settings '{"enableAllProjectMcpServers":
  true}' --settings '{…}'` shows the dialog again.
- The trust dialog: `❯ No, exit` / `Yes, I trust this folder`, cursor on "No, exit".

Codex: no MCP dialog. The trust dialog reads `Trust this folder? … Your trust decision will be
saved.` with `› 1. Trust and continue` / `2. Quit` (cursor `›` on the first). After trust, Codex
loads the project's `.codex/config.toml` MCP servers without asking. It does not read `.mcp.json`.
Today's `TRUST_DIALOG` regex matches this screen, but `_trust_keys` finds no `❯` and gives up.

Grok: no MCP dialog. The trust dialog reads `Do you trust the contents of this directory?` with
`Yes, proceed  y` / `No, quit  n`; the key `y` answers it. After trust, Grok loads `.mcp.json`
servers without asking.

herdr: `herdr agent start` reports `agent_not_ready` when the agent is not ready for input, which
is how a startup dialog reaches the runner today.

## Design

### 1. What the change under review is

`launch` decides the scope before it creates the run directory.

- New setting `settings.scope`: `auto` (default), `commits` or `worktree`. The same values come
  from the new flag `launch --scope` and from the skill argument `scope=…`.
- `auto` resolves to `commits` when the branch has committed changes against the merge-base
  (`git diff --quiet <mb> HEAD` exits 1), otherwise to `worktree`.
- `commits` with nothing committed fails: `nothing committed on this branch since <base>
  (<mb12>); pass --scope worktree to review the uncommitted work`. An empty tree fails as today:
  `nothing to review: the working tree equals <base> (<mb12>)`.
- `launch` records the uncommitted state once: `git status --short --untracked-files=normal`
  (an untracked directory shows as one `?? dir/` line; `normal` is explicit because a user's
  `status.showUntrackedFiles=no` would hide untracked files). It writes the lines to
  `<run_dir>/uncommitted.txt` (empty when the tree is clean) and to `run.json` as `uncommitted`.
  `run.json` also gains `scope` and `head` (HEAD at launch).

The reviewer prompt takes its first two steps from one of two new fragments, rendered by `launch`
into `{SCOPE_STEPS}`:

- `prompts/scope-commits.md`: the change is exactly `git diff <mb> HEAD --`. The working tree also
  holds uncommitted work that is not part of the change: the user's own files, listed as
  `git status --short` showed them at launch. Do not review them and do not mention them. Where a
  file of the change also has uncommitted edits, read its committed version with
  `git show HEAD:<path>`. The list is inlined up to 50 lines; beyond that, a line points to
  `<run_dir>/uncommitted.txt`.
- `prompts/scope-worktree.md`: the change is `git diff <mb> --` (committed and uncommitted) plus
  the untracked files listed below. `launch` builds the list from
  `git status --porcelain --untracked-files=all -z`, one line per file with its size. A binary file
  (a NUL byte in its first 8 KiB), a file larger than 256 KiB and a symlink are marked `skip`:
  the reviewer names them in the review if they matter but does not read them. The list is inlined
  up to 100 entries; beyond that, a line points to `git ls-files --others --exclude-standard`.

The reviewer's hard rules widen: do not modify, move or delete any file in the working tree,
tracked or untracked (section 4 says where the reviewer's own files go).

The launch summary replaces the warning `working tree has uncommitted changes; yolo reviewers
share this tree and can modify them` with a scope line and, when there is uncommitted work, a line
about it:

```
  объём:        коммиты ветки (origin/master..HEAD)
  вне ревью:    ваши незакоммиченные файлы — 1 изменённый, 5 неотслеживаемых; их никто не тронет
```

In `worktree` scope the scope line says that the working tree is reviewed with its uncommitted
work and how many untracked files the reviewers got and skipped. `launch --json` adds `scope` and
`uncommitted`.

`SKILL.md`: never ask about untracked or uncommitted files and never hide them (no
`.git/info/exclude`, no stash). `launch` decides what the change is; relay its summary lines. Pass
`scope=worktree` only when the user asked to review uncommitted work.

### 2. The owner's uncommitted files are protected

`<run_dir>/uncommitted.txt` is the single source. Prompts refer to the file, so the orchestrator
never copies the list into fixer tasks.

Both fixer skeletons (`fixer-auto.md`, `fixer-decision.md`) gain a section "Uncommitted before
the review" that points to the file (an entry ending in `/` covers everything under that
directory) and these rules:

- Never delete, move, rename or rewrite a file that no fix in the task names — in particular
  nothing from `uncommitted.txt`.
- Never delete, move or rename a path from `uncommitted.txt`, even when a fix asks for it: an
  untracked file has no copy in git, and a tracked one would lose the user's uncommitted edits.
  Such an item is reported as `skipped: <path> holds the user's uncommitted work; left to the
  user`. This matters in `worktree` scope, where those files are part of the change and a fix may
  name them.
- Never stage or commit a path from `uncommitted.txt` or from "Files a reviewer already changed".
  A fix that falls into such a file is applied, left uncommitted and reported as
  `applied, not committed: <reason>` (the user's uncommitted edits, or a reviewer's change). Today
  the reviewer case is reported as `skipped` although the fix is applied; both cases now use the
  same wording. `git commit --only` would otherwise commit the owner's uncommitted edits together
  with the fix when both are in one file.

The orchestrator prompt:

- Run facts gain the scope, the HEAD at launch and a pointer to `uncommitted.txt`.
- A ground rule: nothing in the run deletes, moves or rewrites the owner's uncommitted files.
- Phase 3: in `commits` scope, a finding about a path from `uncommitted.txt` that no commit of the
  branch touches is DISMISSED — outside the change, the user's own file. "Remove the stray
  `test_exit.sh`" never reaches the fixer, autodecide or not.
- Phases 4 and 5 check that a fix commit holds no path from `uncommitted.txt`. The report shows
  `applied, not committed` as «применено, не закоммичено: <причина>» and the refused deletion as
  «не применено: удаление или перенос вашего незакоммиченного файла оставлены вам».

### 3. Startup dialogs

**No MCP dialog for claude agents.** `launch._profile_spec` prepends
`--settings '{"enableAllProjectMcpServers": true}'` to the args of every profile of kind `claude`
whose args hold no `--settings` or `--settings=…` of their own. The effective args go into
`run.json`, so the orchestrator, the reviewers and the fixer all start with them, and `runner.log`
shows them. Other kinds keep their args. A profile with its own `--settings` gets nothing added:
the last `--settings` wins, so the documentation tells the owner to put the key into that file.

**`dialogs.py` knows four dialogs:**

| Dialog | Recognized by | Answer |
|---|---|---|
| Claude Code MCP approval | `New MCP server found in this project` or `<N> new MCP servers found in this project` | never answered: a refusal with a reason |
| Claude Code trust | `Yes, I trust this folder` | cursor `❯` on it → `enter`; on `No, exit` → `down`, `enter` (as today) |
| Codex trust | `Trust and continue` | cursor `›` on it → `enter`; on `Quit` → `up`, `enter` |
| Grok trust | `Do you trust the contents of this directory` | `y` |

`resolve_startup_dialog(herdr, name)` returns an outcome with `resolved` and `refusal` instead of
a bool. It handles up to three dialogs in a row, because Claude Code shows the trust dialog first
and the MCP dialog second: after the keys it waits up to 30 s for `idle`, reads the screen again,
and goes on while a known dialog is on it. It checks for the MCP dialog before anything else and
never sends a key to it. An unknown screen or an unknown cursor layout leaves the dialog alone, as
today.

The refusal reason: `Claude Code asks to approve this project's MCP servers (.mcp.json);
herdr-review never answers that dialog: every answer, Esc included, is saved into
.claude/settings.local.json of the repository. It appears only when the profile passes its own
--settings: add "enableAllProjectMcpServers": true there.`

- **Runner** (`_start_agent`): resolved → `idle`, then the review prompt as today; refusal →
  `failed` with the reason and the last screen, never `blocked-start`; anything else →
  `blocked-start` as today.
- **Launch** (the orchestrator): a refusal raises `LaunchError` with the reason; the tab stays
  open, and the run is marked aborted as for any launch failure.
- **Orchestrator prompt**: Phase 1 lists what the runner answers itself. Phase 2 names the MCP
  dialog — never answer it, not even with Esc; `run fail <name> --reason "MCP approval dialog"` —
  and the trust dialog of each CLI with its answer, in case one ever reaches the orchestrator.
- **`SKILL.md` §6** drops "A dialog → resolve it with send-keys". The runner answers the trust
  dialogs and refuses the MCP dialog; on any other screen (a login, an unknown dialog) the
  launcher shows the error and the screen verbatim and stops. It never presses keys in that tab
  and never asks the user which answer to give.
- **README**: one line on the accepted risk — an MCP server that the branch under review adds to
  `.mcp.json` starts in every claude agent of the run without approval.

### 4. A scratch directory and the run's own commits

**Scratch directory.** `launch` creates `<run_dir>/scratch/<profile>/` for every reviewer, and
the reviewer prompt names it through `{SCRATCH_DIR}`: everything the reviewer creates itself —
scripts, test programs, a copy of the repository — goes there, never into the repository and never
into `/tmp`. Running the project's own tests stays allowed when they write only to gitignored
paths. Phase 2 of the orchestrator prompt gains a rule for permission dialogs: a write under
`<run_dir>/scratch/` → approve; a write into the repository → refuse with the dialog's own "no"
option and tell the reviewer in one prompt to use its scratch directory. `--add-dir <runs_dir>`
from the example config already covers the directory for claude and codex. `run finish` deletes
`<run_dir>/scratch/` and logs it: a reviewer may copy the whole repository there.

**The run's own commits.** `run finish` lists commits from `head` in `run.json`
(`git log head..HEAD`) instead of `merge_base..HEAD`; a run without `head` (made by an older
version) keeps the old range. The orchestrator's Phase 6 report takes "Коммиты" from
`git log --oneline {START_HEAD}..HEAD`. `status` and the «готово» notification show the same list.
A commit the owner makes on the branch during the run counts too: the list means "commits made
during the run".

### 5. Fixer commit messages

The fixer writes each commit message in the repository's style instead of a fixed subject:

- It reads `git log -n 20 --format='%s%n%n%b'` for the subject convention (for example
  `fix(scope): …`) and for whether commits carry bodies. The subject says what the commit fixes;
  the body, where the repository uses bodies, says why — one line per fix for the AUTO batch.
- It writes the message into `<run_dir>/fix-auto-commit.txt` or `<run_dir>/fix-<n>-commit.txt`
  and commits with `git commit --only -F <that file> -- <paths>`: no shell quoting of a multi-line
  message, and the message stays in the run directory.
- The message carries no trailer and no mention of herdr-review. Whether a decision was the
  user's or autodecide's lives in the run's report only.

The orchestrator no longer dictates a commit message (`review: …`, `review(auto-decide): …`).
Phases 4 and 5 verify a fix commit by the hash from the fixer's report: that hash is HEAD, and
`git show --stat` of it lists exactly the files the report marks `done` and no path from
`uncommitted.txt`. A report that marks a fix `done` but names no commit gets one prompt to commit,
as today.

### 6. `herdr-review close`

`herdr-review close [DIR|latest] [--run DIR|latest] [--force] [--json]` closes everything a run
opened.

- The run resolves as for `status`: positional or `--run`; without either, `$HERDR_REVIEW_RUN`,
  else `latest` of the current repository.
- Layout `tabs`: the tabs of the reviewers, the fixer and the orchestrator. Layout `grid`: every
  agent is a pane in the orchestrator's tab, so closing that tab closes them all.
- While the run is in progress (phase neither `finished` nor `aborted`) the command refuses:
  `run <id> is still in phase <phase>; closing its tabs stops its agents — pass --force`.
- A tab the owner already closed is reported as already closed, not as an error. The output lists
  what was closed (`--json`: `closed`, `already_closed`, `failed`), and `status.json` records
  `closed_at`.
- `Runner.close(force)` shares the closing loop with `finish`. `finish` still closes only the
  reviewers and the fixer when `close_agents_on_finish` is true: the orchestrator is still
  printing its report at that point.
- `SKILL.md` §7: «закрой вкладки ревью» → `"$HR" close --run latest`, relay the output. On a
  refusal the launcher relays it and passes `--force` only when the user asks.

### 7. Documentation

- `README.md`:
  - Configure: `settings.scope`. The `args` rule says the plugin adds nothing — it now adds the
    `--settings` pair for kind `claude`, with the accepted risk from section 3.
  - Usage: what the change under review is, `scope=` and `--scope`, `--plan` as a path or free
    text, `close`. "During the run": the owner's files are protected, `scratch/`, fix commits in
    the repository's style.
  - Troubleshooting: the MCP dialog (the orchestrator failed to start, or a reviewer ` ✗` with
    that reason), the Codex and Grok trust dialogs, "my file is not in the review" → scope.
- `config.example.yaml`: `scope: auto` with a comment, and a note that claude profiles get the
  `--settings` pair unless they pass their own.
- `SKILL.md`: §3 `scope=`; §5 no questions about the working tree, `--plan` as free text (a
  `git show <sha>:<path>` reference when the plan lives only in history, rulings of earlier review
  rounds); §6 dialogs; §7 `close`.
- `CHANGELOG.md`: Unreleased, Added and Changed.
- `tests/SMOKE.md`: a dirty tree with unrelated files, a repository with an undecided `.mcp.json`
  server, `close` at the end.

### 8. Tests

Unit tests (`python -m unittest`):

- `test_gitutil`: committed changes against the merge-base; status lines with collapsed
  untracked directories and `status.showUntrackedFiles=no` in the repository config; untracked
  files with the binary, large and symlink marks.
- `test_config`: `settings.scope` values; `public_json` shows it.
- `test_launch`: `auto` → `commits` on a dirty tree (`run.json`, `uncommitted.txt`, the reviewer
  prompt's steps and list); `auto` → `worktree` when nothing is committed (the untracked list and
  its marks); `--scope commits` with nothing committed; the `--settings` pair for claude, none for
  a profile with its own `--settings` or for codex; scratch directories; an MCP refusal at the
  orchestrator; the summary fields.
- `test_dialogs`: both MCP variants (no key sent); Codex trust with the cursor on either option;
  Grok trust; Claude Code trust unchanged; trust followed by the MCP dialog; an unknown dialog; a
  wait failure.
- `test_runner_start`: an MCP refusal → `failed` with the reason and the screen.
- `test_runner_collect`: `finish` counts commits from `head` and falls back to the merge-base;
  `finish` deletes `scratch/`; `close` for `tabs` and `grid`, the refusal without `--force`, a tab
  already closed.
- `test_prompts`: placeholder sets and the key phrases of the new rules.
- `test_cli`: `--scope` and `close` parse and dispatch.

bats (`tests/bats`, fake herdr):

- `launch.bats`: `--scope` and the summary lines; the fake herdr log shows
  `-- --settings {"enableAllProjectMcpServers": true} --model opus`; an orchestrator stopped at
  the MCP dialog → exit 1 with the reason and no `send-keys`.
- `run.bats`: `finish` counts only commits made after `launch`; `close` before and after `finish`.

`tests/run.sh` passes.

## Files

| File | Change |
|---|---|
| `herdr_review/gitutil.py` | committed-changes check, status lines, untracked files with marks; `has_changes` goes |
| `herdr_review/config.py` | `settings.scope` |
| `herdr_review/launch.py` | scope, `uncommitted.txt`, `head`, scratch directories, `--settings` pair, rendered scope steps, refusal handling, summary fields |
| `herdr_review/dialogs.py` | dialog table, outcome, MCP refusal, Codex and Grok trust |
| `herdr_review/runner.py` | refusal → `failed`, commits from `head`, scratch cleanup, `close` |
| `herdr_review/cli.py` | `--scope`, `close`, summary lines |
| `prompts/reviewer.md`, `prompts/scope-commits.md`, `prompts/scope-worktree.md` | scope steps, hard rules, scratch directory |
| `prompts/fixer-auto.md`, `prompts/fixer-decision.md` | uncommitted files, commit message in the repository's style |
| `prompts/orchestrator.md` | run facts, ground rule, Phase 1–6 changes above |
| `skills/review/SKILL.md` | §3, §5, §6, §7 |
| `README.md`, `config.example.yaml`, `CHANGELOG.md`, `tests/SMOKE.md` | section 7 |
| `tests/unit/*`, `tests/bats/*`, `tests/fake-herdr/herdr` | section 8 |

## Risks and edge cases

- An MCP server that the branch under review adds to `.mcp.json` starts in every claude agent of
  the run without approval. Accepted: the owner reviews their own branches; the README says so.
- A future Claude Code may change what `enableAllProjectMcpServers` does. The MCP dialog is then
  refused with a clear reason, never answered.
- A trust dialog whose wording changes goes unrecognized and ends in `blocked-start`; the
  orchestrator's Phase 2 rule still names the answer.
- Paths with unusual characters appear quoted in `git status --short`; the lists are for reading,
  not for parsing.
- In `commits` scope the project's tests run against the working tree, the owner's uncommitted
  edits included.
- A run made by an older version has no `scope`, `head` or `uncommitted.txt`; the runner and the
  orchestrator prompt of that run keep working, and `finish` falls back to the merge-base.
