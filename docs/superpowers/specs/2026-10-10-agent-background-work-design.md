# Agents busy with background work — design

Date: 2026-10-10. Branch: `fix/agent-background-work`. The owner approved each section of this design in
brainstorming; this document records it for the implementation plan. It is sub-project A of the analysis of run
`hrlwi8` (`docs/2026-10-10-grok-dialogs-and-background-tasks.md`, untracked); the grok permission dialogs (B) and the
other observations of that run (C) get designs of their own.

## Summary

herdr reports an agent `idle` or `done` while that agent waits for its own background work: a heavy command it
started through `herdr-review exclusive`, or a grok background task. The runner takes `idle`/`done` for settled, so
`collect` re-prompts the agent and, on a second miss, marks a reviewer that is still working `failed`. In run `hrlwi8`
the orchestrator then pressed Enter in the agent's tab, which made grok cut the agent's turn short.

The runner will keep such an agent `working` and name the work in a new field, `background`. Two signals tell it the
agent is busy: a registry of live wrappers, which `herdr-review exclusive` keeps per run for every agent kind, and,
for grok, the status line grok shows while background work runs. `collect` looks at an agent again right before it
re-prompts or fails it. The orchestrator learns to leave such an agent alone, and the agents' prompts ask them to wait
for their own commands.

## Background

### What happened in run `hrlwi8`

Reviewer `deepseek-flash-lanit` (grok 1.0.50, herdr 0.9.3). Times UTC, from the grok session
(`events.jsonl`, `chat_history.jsonl`), `runner.log` and the orchestrator's transcript:

| Time | Event |
|---|---|
| 21:43:37 | The model runs `"<runner>" exclusive --timeout 1500 -- sh -c '… go test -race …'` with `block_until_ms: 0`: a grok background task. |
| 21:44:19 | The wrapper gets the build queue after 42 s of waiting. |
| 21:45:04 | The model waits for that task: `get_command_or_subagent_output(task_ids=[…], timeout_ms=120000)`. Its turn goes on; the session has no `turn_ended` until 21:45:57. |
| 21:45:09 | The screen shows an empty input and, above it, `◉ 1 command still running · send a message to interrupt`. herdr reports `agent_status: done`. |
| 21:45:12 | The orchestrator runs `collect`. No review file: `collect` sends `retry_text` (`collect_retries` becomes 1). grok queues the text; herdr answers `agent_prompt_stalled … current status is done`. |
| 21:45:31 | herdr reports `idle`; the runner sets `idle`. The screen shows `#1 You have not written a valid review …` and `○ 1 command still running · 1 queued, Enter to send now`. By orchestrator.md (Phase 2, step 2) the next step is `collect`, which would mark the reviewer `failed` ("no valid review file"). The orchestrator did not call it. |
| 21:45:51 | The wrapper's command ends, exit 0; the model's wait returns. At 21:45:54 it starts its next background command (`npm test` through the wrapper). |
| 21:45:57 | The orchestrator runs `herdr agent send-keys … enter` ("Send the queued re-prompt to deepseek now"). grok sends the queued prompt at once: `turn_ended: cancelled, cancellation_category: mid_turn_abort, trigger: send_now`. |
| 21:50:11 | In the new turn the agent writes its review; `collect` takes it at 21:50:28. |

The model did what a reviewer should: it started its tests in the background, read code meanwhile, then waited for
them. The review survived only because the orchestrator skipped a `collect` the rules asked for, and because its
Enter landed at a harmless moment.

### grok 1.0.50

- `run_terminal_command` moves a command to the background when it outlives `block_until_ms`, **30 000 ms by
  default**; `0` starts it there at once. A test run through the wrapper, which waits up to 60 s for its turn, is
  therefore almost always a background task.
- `get_command_or_subagent_output` with `timeout_ms > 0` waits inside the turn for background commands, monitors or
  subagents. A notification of the end also wakes an agent whose turn already ended.
- "The Still-Running Status Line" (`~/.grok/docs/user-guide/20-background-tasks.md`): while background work runs
  and the agent looks idle — between turns, or in such a wait — grok shows above its input
  `◎ 1 command · 2 monitors · 1 loop · 1 subagent still running`; during a wait it adds
  `· send a message to interrupt`, or shows `◎ waiting · send a message to interrupt` when there is nothing to count.
  With a prompt in its queue the line reads `○ 1 command still running · 1 queued, Enter to send now`. The leading
  glyph changes (`◉`, `○`, `◎`).
- A prompt typed during such a wait goes into grok's queue and reaches the model after the turn, as `The user sent a
  message while you were working: …`. Enter on a queued prompt sends it at once and aborts the running turn.

### herdr 0.9.3

- Its grok integration installs only a `SessionStart` hook (`~/.grok/hooks/herdr.json`), which reports the session
  id. It reads the agent's state from the terminal; in the wait above there is no spinner, and herdr reports `done`,
  then `idle`.
- `agent prompt --until working` to such an agent returns `agent_prompt_stalled` although grok queued the text.

### herdr-review 0.5.0

- `STATE_CLASS` (`runner.py:26`) maps `idle` and `done` to `settled`; `_observe` (`runner.py:743`) copies herdr's
  status into the agent's state.
- `collect` (`runner.py:1050`) re-prompts an `idle`/`done` reviewer without a valid review file once
  (`collect_retries`) and marks it `failed` on the next miss. It decides on the state the last `wait` stored.
- `status.py` merges `collect_retries` with the copy on disk through `max()`, so that concurrent runner processes
  cannot reset the one-shot quota.
- `exclusive.json` names the holder of the build queue only; nothing records the wrappers still waiting for it.
  Every agent's environment carries `HERDR_REVIEW_RUN` and `HERDR_REVIEW_AGENT`, which the wrapper reads.
- Claude Code's Bash tool can run a command in the background too, and its turn can end while that command runs.

## Problem

1. **A working reviewer can be failed.** herdr's `idle`/`done` for an agent that waits for its own background work
   makes `collect` spend the agent's only re-prompt, and the next `collect` marks it `failed`.
2. **The prompt lands in a queue.** grok queues a prompt sent during such a wait, and herdr reports it stalled.
3. **The orchestrator interrupts the agent.** The rule for a stuck `working` agent ("an idle prompt line with nothing
   happening → esc, then `run prompt --retry`") and grok's own hint `Enter to send now` both lead it to send keys to an
   agent that is waiting correctly. Enter aborts the agent's turn.
4. **The fixer is exposed too.** The orchestrator stops waiting for the fixer at `idle`/`done` and reads its report,
   while the fixer's tests may still run.

## Goals

1. A reviewer or the fixer that waits for its own build-queue command, or for a grok background task, stays
   `working`: `run wait` keeps waiting for it, and `collect` neither re-prompts nor fails it.
2. The orchestrator can tell such an agent from a stuck one and never sends it keys or a prompt.
3. No new agent state and no change to the retry quotas; an agent without background work behaves as today.
4. The build-queue signal works for every agent kind. The grok screen signal only complements it, and has a bound.

## Design

### 1. The wrapper registry (`herdr_review/exclusive.py`)

Inside a review, every wrapper registers itself in `<run_dir>/wrappers/<pid>.json`, where `<run_dir>` comes from
`HERDR_REVIEW_RUN` (`Where.run_dir`) and `<pid>` is the wrapper's own pid.

- **Entry.** `{"pid": <int>, "agent": <HERDR_REVIEW_AGENT or null>, "run_id": <str or null>, "command": <command_text>,
  "phase": "waiting" | "running", "started_at": <now_iso>, "running_since": <now_iso or null>}`. `command` is the same
  text as in `exclusive.json`: one line, at most 200 characters, control characters escaped.
- **Writing.** Atomic, as `write_holder` writes `exclusive.json`: a per-process temporary file, mode 0600, then
  `os.replace`. The directory is created with mode 0700 on first use. A shared helper writes both files.
- **Lifecycle in `run()`.**
  - After the first `_taken_off` check and `_catch_stop_signals()`, before `_open_queue` and `_take_turn`: write the
    entry with `phase: "waiting"`.
  - Once the turn is taken and the second `_taken_off` check passed, before the holder file: rewrite it with
    `phase: "running"` and `running_since`.
  - An outer `finally` removes the entry on every way out: the command's end, `busy` (exit 75), a stop signal while
    waiting, a refusal after the wait, an error opening the queue or starting the command.
- **No entry** outside a review (`Where.run_dir is None`) and for a nested call (`HERDR_REVIEW_EXCLUSIVE`), which
  `exec`s into its command under the outer wrapper's entry.
- **Failure to write** never stops the command. The wrapper goes on and appends one line to the run's `runner.log`
  (`exclusive: <who> could not register in <dir>: <error>`).
- **Reading.** `live_wrappers(run_dir, alive=None) -> list[dict]`: the parsed entries whose `pid` is an `int` and
  passes `alive` — `is_wrapper`, looked up at call time so that tests can substitute it. An entry whose pid fails
  that check is deleted (a wrapper killed by SIGKILL leaves its entry behind); an unreadable or malformed file is
  skipped and left alone. Entries come back in `started_at` order.

A pid reused by another `herdr-review exclusive` process would keep a stale entry alive for that wrapper's lifetime;
the holder file accepts the same risk.

### 2. The grok status line (`herdr_review/dialogs.py`)

Next to the Codex update patterns; the module docstring widens to "agent screens the runner reads without an LLM".

- `GROK_STATUS_LINES = 20`.
- `GROK_BACKGROUND = re.compile(r"\b\d+ (?:command|monitor|loop|subagent)s?(?:\s*·\s*\d+ (?:command|monitor|loop|subagent)s?)*\s+still running\b")`.
- `GROK_WAITING = re.compile(r"\bwaiting\s*·\s*send a message to interrupt\b")`.
- `grok_background(screen: str) -> str | None`: flatten the last `GROK_STATUS_LINES` lines of `screen` with `_flat`
  (so a line a narrow pane wrapped still matches) and return the first match of `GROK_BACKGROUND`, else of
  `GROK_WAITING`, as text; `None` when neither matches.

Only the bottom of the screen is searched, where grok draws the line above its input; the transcript above it may
quote tool results such as "The command is still running in the background".

### 3. The runner (`herdr_review/runner.py`)

New constants: `BUSY_GRACE_SEC = 30`, `SCREEN_BUSY_LIMIT_SEC = 1800`.

New fields of an agent in `status.json`, all optional (`.get`, so older runs load): `background` (text or `null`),
`busy_seen_at` and `screen_busy_since` (wall-clock seconds, from the runner's `wall_clock`), `screen_busy_logged`
(boolean).

**`_background_work(name) -> str | None`.** What the agent waits for, or `None`. In this order:

1. **A live wrapper.** `exclusive.live_wrappers(self.run_dir)` filtered by `agent == name`; the first entry with
   `phase: "running"`, else the first one: `running its command: <command>` or `waiting for its turn in the build
   queue: <command>`. Sets `busy_seen_at = now` and clears `screen_busy_since`.
2. **The grok line**, for an agent of kind `grok` only. Read the visible screen
   (`herdr.agent_read(name, source="visible", lines=SCREEN_LINES)`) and apply `grok_background`.
   - A match: `screen_busy_since` is set when it is empty. While `now - screen_busy_since <= SCREEN_BUSY_LIMIT_SEC`,
     set `busy_seen_at = now` and return `grok: <match>`. Past the limit the match is ignored, and `runner.log` gets
     one line per such stretch: `<name>: grok has shown "<match>" for 30 minutes without a build-queue command; going
     by herdr's status`. A flag in the agent's entry, `screen_busy_logged`, keeps it to one line.
   - No match, or the screen could not be read: clear `screen_busy_since` and `screen_busy_logged`.
3. **The grace.** When `now - busy_seen_at < BUSY_GRACE_SEC`: `its background work ended moments ago`. It covers
   the seconds between the end of the work and the agent's next step, which herdr may report as `done`.

**`_observe`.** At its end, where `new` is computed from herdr's `live` status:

- `live` is `idle` or `done`: `background = _background_work(name)`; when it is set, `new = "working"`.
- Any other `live` status: `background = None`, and no screen is read.
- `background` is stored masked (`herdr.mask`), and the agent is marked dirty for this generation whenever
  `background`, `busy_seen_at`, `screen_busy_since` or `screen_busy_logged` changed, so the save that follows
  records them.
- `runner.log` gets a line when `background` changes from empty to set (`<name>: herdr reports <live>, kept working:
  <background>`) and when it is cleared; not on every poll.

`blocked` keeps priority: a dialog is answered whatever runs in the background. No new state: the label stays
` ⏳`, and the fixer, observed by the same `_observe`, is not marked ` ✓` while its tests run.

**`collect`.** In the branch for an `idle`/`done` reviewer whose review file is not valid, before it sends the review
prompt to an agent that never got one, before the re-prompt and before it marks the reviewer `failed`:

1. `self._observe(n)` once more. When it returns `False` (herdr could not be asked), the reviewer stays pending.
2. When the state is no longer `idle` or `done` — busy, or working again — the reviewer is pending: no prompt, and
   `collect_retries` stays as it is.
3. Otherwise the existing logic runs unchanged.

Giving `collect_retries` back after an `agent_prompt_stalled` was considered and dropped: the `max()` merge in
`status.py` would undo it, and with the fresh look above `collect` no longer prompts a busy agent at all.

**Output.** `background` joins the per-agent output of `run wait` and `_summary`, which `run prompt`,
`start-reviewers` and `start-fixer` return. `herdr-review status --json` carries it as part of `status.json`; the text
view shows `фон: <background>` in the reason column when it is set.

### 4. Prompts

- **`prompts/exclusive.md`**, which the reviewers and the fixer share, gets a bullet: "You may start the wrapper in
  the background and go on working while it runs. Before you write your result and reply DONE, wait for every
  command you started, the wrapper's included, and read its output. Never end your turn while one of your commands
  still runs."
- **`prompts/orchestrator.md`**:
  - Phase 2, step 1: `run wait` also reports `background`: set while an agent that herdr shows idle still waits for its
    own work — its command in the build queue or a grok background task. The runner keeps such an agent `working`.
  - Phase 2, the rule for `working` with `screen_changed: false` on two consecutive waits: an agent with `background`
    set waits for its own work; keep waiting, and send it no keys and no prompt.
  - A new rule beside it: grok shows `N command(s) … still running` above its input while an agent's background
    task runs, and `N queued, Enter to send now` while a prompt waits in its queue. grok delivers that prompt by itself
    when the agent's turn ends; Enter would send it at once and cut the turn short. Never press Enter or any other key
    in such a tab.
  - `prompt_stalled` (the list of states): for a grok agent it usually means grok queued the prompt while the agent
    finished its background work.
  - Phase 5, the fixer loop: the runner keeps the fixer `working` while `background` is set.

### 5. Documentation

- **README.** The tab labels: ` ⏳` also while herdr shows an agent idle but its build-queue command or a grok
  background task still runs. Troubleshooting: "A grok reviewer or the fixer shows ` ⏳` while its tab looks idle,
  with `1 command still running` above the input — it waits for its own background command; `herdr-review status`
  shows it under `фон:`. Do not press Enter in that tab: grok would send its queued prompt at once and cut the
  agent's turn short."
- **CHANGELOG**, a new `[Unreleased]` section: Fixed — an agent that herdr reports idle or done while its
  build-queue command or a grok background task runs stays `working`, and `collect` no longer re-prompts or fails it;
  Added — the wrapper registry and the `background` field; Changed — the prompts.
- **The owner's analysis** (`docs/2026-10-10-grok-dialogs-and-background-tasks.md`, untracked, stays untracked):
  section 2, its row in the summary table and the order of work are corrected to the timeline and cause above.
- **herdr.** The texts of two issues are in the appendix; they are filed only when the owner says so.

## Testing

- **`tests/unit/test_exclusive.py`**, with real wrappers as today:
  - the command sees its own entry with `phase: "running"` (`cat "$RUN/wrappers/$PPID.json"`);
  - a wrapper waiting behind a holder has an entry with `phase: "waiting"`;
  - the entry is gone after a normal exit, after exit 75, after SIGTERM while waiting and after exit 127;
  - no entry outside a review and none for a nested call;
  - the entry's mode is 0600;
  - `live_wrappers` deletes the entry of a dead pid, skips a malformed file and keeps a live one.
- **`tests/unit/test_dialogs.py`**: `grok_background` matches the three screens of run `hrlwi8` (the wait, the queued
  prompt, `send a message to interrupt`), the documented forms (`1 command · 2 monitors · 1 loop · 1 subagent still
  running`, `waiting · send a message to interrupt`) and a line wrapped in a narrow pane; it does not match an idle
  screen, a working screen with its spinner, `Task completed in 2m13s: …` or "The command is still running in the
  background" higher up in the transcript.
- **`tests/unit/test_runner_wait.py`**, with FakeHerdr, a substituted `wall_clock` and a substituted wrapper check:
  - `done` with a live wrapper → `working`, `background` names the command, and `wait` does not return `settled`;
  - `idle` with the grok line on screen → `working`; the same screen for an agent of kind `claude` → `idle`;
  - the grace: busy, then not, within 30 s → `working`; after 30 s → `done`;
  - the limit: the grok line alone for more than 30 minutes → `done`, with one line in `runner.log`;
  - `blocked` with a live wrapper → `blocked`;
  - `background` is cleared when herdr reports `working`;
  - the fixer with a live wrapper → `working` and the label ` ⏳`, not ` ✓`.
- **`tests/unit/test_runner_collect.py`**:
  - a busy reviewer without a review file → pending, no `agent_prompt`, `collect_retries` unchanged;
  - a busy reviewer after a first miss → pending, not `failed`;
  - a reviewer herdr reports `working` again by the time `collect` looks → no prompt;
  - `_observe` failing in that look → pending;
  - without background work, the first miss re-prompts and the second fails, as today.
- **`tests/unit/test_prompts.py`** pins the new text of `exclusive.md` and `orchestrator.md`.
- **`tests/bats/status.bats`**: the text view shows `фон: …` for an agent with `background`.

## Limitations

- The grok signal depends on the wording of grok's status line, checked against grok 1.0.50 and pinned by tests. A
  grok release that rewords it silently turns the signal off; the wrapper registry still covers build-queue
  commands.
- Background work outside the wrapper is seen for grok only. The prompts send every long command through the
  wrapper, so what remains for the other kinds is short.
- An agent that finishes right after its background work reaches `collect` up to 30 s later than today.
- A grok subagent or background command that runs longer than 30 minutes outside the wrapper counts as busy only for
  those 30 minutes.

## Out of scope

- The grok permission dialogs and the MiMo adapter (sub-project B), and the other observations of run `hrlwi8`
  (sub-project C).
- Fixes in herdr itself; the issues below report them.
- A machine-wide list of waiting wrappers in `herdr-review status`, and stopping a failed agent's waiting wrappers in
  `run fail`: once it gets its turn, such a wrapper refuses to run anyway (`_taken_off`).

## Appendix: herdr issues

**1. A grok agent that waits for a background task is reported `done`.**
herdr 0.9.3, grok 1.0.50. A grok agent runs a command in the background (`run_terminal_command` with
`block_until_ms: 0`, or any command longer than 30 s, which grok moves to the background) and then waits for it with
`get_command_or_subagent_output` (`timeout_ms` > 0). `herdr agent get` reports `agent_status: done`, later `idle`,
while the grok turn is still running: the session's `events.jsonl` has no `turn_ended`, and the screen shows
`◉ 1 command still running · send a message to interrupt` above an empty input, with no spinner. Expected: `working`
while the turn runs. Between turns, grok shows `N command(s) … still running` too and wakes the agent when the work
ends; reporting that as `working`, or giving a background-task count in `agent get`, would let callers tell such an
agent from a finished one.

**2. `agent prompt` reports `agent_prompt_stalled` when grok queues the prompt.**
`herdr agent prompt <agent> "<text>" --wait --until working` to the agent above returns `agent_prompt_stalled: agent
prompt produced no observed working or blocked state within 5000 ms; current status is done`. grok accepted the text
into its queue (`○ 1 command still running · 1 queued, Enter to send now`) and delivered it after the turn. A
distinct result such as `queued` would tell a caller to wait: a retry adds a second message, and Enter on the queued
prompt makes grok send it at once and abort the running turn (`turn_ended: cancelled, mid_turn_abort,
trigger: send_now`).
