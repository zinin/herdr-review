---
name: auto-decide
description: Switch a running herdr-review review to automatic decisions — the orchestrator stops asking about disputed issues one by one and decides the rest itself, with a self-check. Use when the user says «дальше решай сам», invokes /herdr-review:auto-decide, or no longer wants to answer the orchestrator. Requires a run in progress.
---

# herdr-review: switch a run to automatic decisions

The orchestrator asks the user about every disputed issue when the run was launched without
`--autodecide`. This skill flips that for the rest of the run. The switch is one-way and covers
both disputed issues and the drift question.

## 1. Preconditions

```bash
test "${HERDR_ENV:-}" = 1 && echo inside-herdr
```

Nothing printed → «сессия не внутри herdr — запустите её в панели herdr», stop.

Find the runner: `HR="$(realpath "<this skill's base directory>/../../bin/herdr-review")"`, else
`HR="$(command -v herdr-review)"`. Neither exists → say so and stop.

## 2. Find the run

```bash
"$HR" status --run "${HERDR_REVIEW_RUN:-latest}" --json
```

`latest` resolves per repository, so run this from the repository under review; if the user names
a run directory, pass that instead. Exit 1 → show stderr verbatim and stop; «no runs for this
repository» usually means you are standing in the wrong checkout.

From the JSON take `run_dir`, `run_id`, `phase`, `autodecide`, `waiting_for_user` and
`orchestrator.name`. `phase: finished` → tell the user the run is already over and stop.

## 3. Flip the mode

```bash
"$HR" run autodecide --run "<run_dir>"
```

Exit 1 → show stderr and stop. The JSON answers `was`: `false` means you switched it, `true` means
it was already automatic.

## 4. Tell the orchestrator — do not skip this

The orchestrator does not poll its run state. When it is waiting for an answer it has ended its
turn, so nothing happens until something writes into its pane:

```bash
herdr agent prompt "<orchestrator.name>" "Режим прогона переключён на автоматический. Дальше решай всё сам, включая замечание, о котором спрашиваешь сейчас: разбор, «Проверка решения», пометка «под вопросом», если решение не устояло, правки через фиксера. Ко мне больше не обращайся, иди до конца Phase 6." --wait --until working --timeout 30000
```

Failed with `agent_not_found` → the orchestrator is gone; say so, and note that the mode is still
recorded for `status`.

## 5. Report

Print: run id, «режим переключён на автоматический» (or «уже был автоматическим»), the
orchestrator's tab, and `"$HR" status --run <run_dir>` for watching. Then end your turn — the
orchestrator drives the rest.
