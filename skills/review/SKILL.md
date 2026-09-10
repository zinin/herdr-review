---
name: review
description: Launch a multi-agent code review inside herdr — reviewers, an orchestrator and a fixer run as visible interactive agents in their own tabs. Use when the user asks for a herdr review, invokes /herdr-review:review, or wants the current branch reviewed by several models inside herdr. Requires HERDR_ENV=1.
---

# herdr-review: launch a review run

You are the launcher. You collect the parameters, call `herdr-review launch`, show its summary, and end your turn. You never wait for the review and never call `herdr-review run …` yourself — that is the orchestrator's job, in its own tab.

## 1. Preconditions

```bash
test "${HERDR_ENV:-}" = 1 && echo inside-herdr
```

If this prints nothing, tell the user «сессия не внутри herdr — запустите её в панели herdr» and stop.

Find the runner: `HR="$(realpath "<this skill's base directory>/../../bin/herdr-review")"`. If that file does not exist, `HR="$(command -v herdr-review)"`. If neither exists, say so and stop.

## 2. Read the config

```bash
"$HR" profiles --json
```

Exit 1 → print its stderr to the user verbatim and stop. Never edit the config file: it is the user's.

## 3. Arguments

Recognise, in any order: `default` or another preset name from `presets`; `BASE_BRANCH=<ref>`; `autodecide`; `layout=tabs|grid`; `reviewers=a,b,c`; `orchestrator=<profile>`; `fixer=<profile>`. Anything else is free text: use it as the description.

With a preset or an explicit `reviewers=` there are no questions.

## 4. Without a preset — ask

Use the host's question tool: `AskUserQuestion` (Claude Code), `ask_user_question` (Grok); on a host without one, ask in plain text and take the next message as the answer. Four questions, one at a time:

1. Reviewers — multi-select over the profile names from `profiles --json`; at most 4 options per page (paginate; mark members of `presets.default.reviewers` with ★). «Other» is not a profile: re-ask.
2. Orchestrator — single choice (★ = `presets.default.orchestrator`).
3. Fixer — single choice (★ = `presets.default.fixer`).
4. Autodecide — «да / нет» (default from `settings.autodecide`).

## 5. Description and plan

If you know from this session what was implemented, pass `--description "<one or two sentences>"`. If a plan or spec file for this work exists, pass `--plan <path>`. Do not invent either; omit what you do not know.

## 6. Launch

```bash
"$HR" launch --preset default --description "…"
```

Compose the flags from the answers: `--preset <name>`, or `--reviewers a,b,c --orchestrator x --fixer y`; `--base` only when `BASE_BRANCH=` was given; `--autodecide` only when chosen, `--no-autodecide` when the user said no; `--layout` when given; `--plan` when a plan file is known.

Exit 0 → show the summary as printed (run dir, orchestrator agent, `herdr agent focus <name>`, `herdr-review status latest`) and end your turn.

Exit 1 → show stderr verbatim. Two cases you can help with:

- «failed to start … Tab … is left open»: `herdr agent read <name> --source visible --lines 60` shows why. A dialog → resolve it with `herdr agent send-keys <name> …`, then send the prompt from the message. Anything else → report and stop.
- «did not start working … Re-prompt by hand»: run the printed `herdr agent prompt …` once. If it stalls again, report and stop.

## 7. Later

«Как там ревью?» → `"$HR" status --run latest`, relay the output. `latest` is resolved per repository: run it from the repository under review, or pass that run's directory as `--run`. The orchestrator's tab is `rv-<run_id>: orch`; disputed issues are answered there.
