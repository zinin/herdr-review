#!/usr/bin/env bats
load helpers

setup() { setup_env; }
teardown() { teardown_env; }

@test "launch: happy path creates the run and starts the orchestrator" {
  run "$HR" launch --json --description "added the thing"
  [ "$status" -eq 0 ]
  json_has "$output" 'd["run_id"].startswith("hr") and d["reviewers"]==["claude-opus","codex"] and d["tab"]=="w1:t2" and d["pane"]=="w1:p2"'
  RUN="$(run_dir_of)"
  [ -f "$RUN/run.json" ]; [ -f "$RUN/orchestrator.md" ]; [ -f "$RUN/prompts/codex.md" ]; [ -f "$RUN/status.json" ]
  grep -q 'added the thing' "$RUN/prompts/codex.md"
  grep -q '"phase": "reviewing"' "$RUN/status.json"
  grep -q "tab create --workspace w1 --cwd $REPO --label rv-.*: orch --env HERDR_REVIEW_RUN=$RUN --env GIT_OPTIONAL_LOCKS=0 --env XDG_CONFIG_HOME=$XDG_CONFIG_HOME --env SECRET_TOKEN=s3cret-value --env HERDR_REVIEW_AGENT=hr.*-orch --no-focus" "$FAKE_HERDR_LOG"
  grep -q 'agent start hr.*-orch --kind claude --pane w1:p2' "$FAKE_HERDR_LOG"
  grep -qF -- '--timeout 300000 -- --settings {"enableAllProjectMcpServers": true, "attribution": {"commit": ""}} --model opus' "$FAKE_HERDR_LOG"
  grep -q "agent prompt hr.*-orch Read $RUN/orchestrator.md and follow it exactly. Do not stop until the run is finished. --wait --until working --timeout 60000" "$FAKE_HERDR_LOG"
  grep -q 'SECRET_TOKEN=\*\*\*' "$RUN/runner.log"
  [ "$(grep -c 's3cret-value' "$RUN/runner.log")" -eq 0 ]
  [ "$(grep -c 's3cret-value' "$RUN/run.json")" -eq 0 ]
  [ "$(grep -c 's3cret-value' "$RUN/orchestrator.md")" -eq 0 ]
}

@test "launch: text output names the run and the hints" {
  run "$HR" launch
  [ "$status" -eq 0 ]
  [[ "$output" == *"Запущен прогон hr"* ]]
  [[ "$output" == *"herdr agent focus hr"* ]]
  [[ "$output" == *"herdr-review status latest"* ]]
}

@test "launch: refuses outside herdr" {
  unset HERDR_ENV
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"HERDR_ENV"* ]]
}

@test "launch: missing config gives the copy hint" {
  rm "$XDG_CONFIG_HOME/herdr-review/config.yaml"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"config.example.yaml"* ]]
}

@test "launch: invalid config prints the validator messages" {
  echo 'profiles: {bad name: {kind: x}}' > "$XDG_CONFIG_HOME/herdr-review/config.yaml"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"profiles.bad name"* ]]
}

@test "launch: reviewer without a binary is skipped; all missing is an error" {
  run "$HR" launch --json --reviewers claude-opus,ghost
  [ "$status" -eq 0 ]
  json_has "$output" 'd["skipped"]==["ghost"] and d["reviewers"]==["claude-opus"] and any("ghost" in w for w in d["warnings"])'
  run "$HR" launch --reviewers ghost
  [ "$status" -eq 1 ]
  run "$HR" launch --orchestrator ghost
  [ "$status" -eq 1 ]
}

@test "launch: nothing to review on the base branch" {
  git switch -q master
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"nothing to review"* ]]
}

@test "launch: server down" {
  echo '{"server_down": true}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"not running"* ]]
}

@test "launch: stalled orchestrator prompt is retried, then reported with a manual command" {
  echo '{"agent_prompt": {"*-orch": {"code": "agent_prompt_stalled", "message": "no activity"}}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"Re-prompt by hand"* ]]
  [ "$(grep -c 'agent prompt' "$FAKE_HERDR_LOG")" -eq 2 ]
}

@test "launch: unknown startup dialog fails with the screen; trust dialog is answered" {
  echo '{"agent_start": {"*-orch": {"code": "agent_not_ready", "message": "blocked"}}, "screens": {"*-orch": "Please log in\n", "w1:p2": "login screen\n"}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"login screen"* ]]
  [[ "$output" == *"w1:t2"* ]]
  rm -f "$FAKE_HERDR_STATE"
  echo '{"agent_start": {"*-orch": {"code": "agent_not_ready", "message": "blocked"}}, "screens": {"*-orch": "❯ No, exit\n  Yes, I trust this folder\n"}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  grep -q 'agent send-keys hr.*-orch down enter' "$FAKE_HERDR_LOG"
}

@test "launch: second unfinished run warns" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  json_has "$output" 'any("not finished" in w for w in d["warnings"])'
}

@test "launch: the MCP approval dialog is never answered" {
  echo '{"agent_start": {"*-orch": {"code": "agent_not_ready", "message": "blocked"}}, "screens": {"*-orch": "2 new MCP servers found in this project\n❯ [✔] one\n  [✔] two\n", "w1:p2": "2 new MCP servers found in this project\n"}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"enableAllProjectMcpServers"* ]]
  [[ "$output" == *"w1:t2"* ]]
  ! grep -q 'agent send-keys' "$FAKE_HERDR_LOG"
}

@test "launch: the MCP dialog herdr calls idle is refused too" {
  echo '{"screens": {"*-orch": "2 new MCP servers found in this project\nSelect any you wish to enable.\n❯ [✔] one\n  [✔] two\nSpace to select · Esc to reject all\n"}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" launch
  [ "$status" -eq 1 ]
  [[ "$output" == *"failed to start: Claude Code asks to approve"* ]]
  [[ "$output" == *"Tab w1:t2 is left open"* ]]
  [ "$(grep -c 'agent send-keys' "$FAKE_HERDR_LOG")" -eq 0 ]
  [ "$(grep -c 'agent prompt' "$FAKE_HERDR_LOG")" -eq 0 ]
}

@test "launch: a dirty tree is left out of a review of the commits" {
  echo edited > a.txt; mkdir notes; echo x > notes/one.md
  run "$HR" launch
  [ "$status" -eq 0 ]
  [[ "$output" == *"объём:        коммиты ветки (master..HEAD)"* ]]
  [[ "$output" == *"изменённых: 1, неотслеживаемых: 1"* ]]
  [[ "$output" != *"uncommitted changes"* ]]
  RUN="$(run_dir_of)"
  grep -qx ' M a.txt' "$RUN/uncommitted.txt"
  grep -qx '?? notes/' "$RUN/uncommitted.txt"
  grep -q 'git diff .* HEAD --' "$RUN/prompts/codex.md"
}

@test "launch: --scope worktree hands the reviewers the untracked files" {
  echo new > new.txt
  run "$HR" launch --json --scope worktree
  [ "$status" -eq 0 ]
  json_has "$output" 'd["scope"]=="worktree" and d["untracked"]=={"files": 1, "skipped": 0}'
  grep -qF -- '- `new.txt` (4 B)' "$(run_dir_of)/prompts/codex.md"
}

@test "launch: --scope commits refuses a branch with nothing committed" {
  git switch -q master; git switch -q -c empty; echo wip > b.txt
  run "$HR" launch --scope commits
  [ "$status" -eq 1 ]
  [[ "$output" == *"nothing committed on this branch since master"* ]]
}
