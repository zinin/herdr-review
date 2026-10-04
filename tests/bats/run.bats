#!/usr/bin/env bats
load helpers

setup() { setup_env; }
teardown() { teardown_env; }

@test "run: full pipeline through the fake herdr" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"
  echo '{"agent_get": {"*-claude-opus": ["working", "done"], "*-codex": ["working", "working", "done"]}}' > "$FAKE_HERDR_SCENARIO"

  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  json_has "$output" 'all(a["state"]=="working" for a in d["agents"].values())'
  [ "$(grep -c 'tab create' "$FAKE_HERDR_LOG")" -eq 3 ]
  grep -q "agent prompt hr.*-codex Read $RUN/prompts/codex.md and follow it exactly. --wait --until working --timeout 30000" "$FAKE_HERDR_LOG"
  grep -q 'tab rename w1:t4 rv-.*: codex ⏳' "$FAKE_HERDR_LOG"
  grep -q '"tree_hash_before": "' "$RUN/status.json"

  run "$HR" run wait
  [ "$status" -eq 0 ]
  json_has "$output" 'd["reason"]=="state_change" and d["pending"]==[n for n in d["agents"] if n.endswith("-codex")]'

  good_review "$RUN/reviews/claude-opus.md"; good_review "$RUN/reviews/codex.md"
  for i in 1 2 3 4 5; do
    run "$HR" run wait; [ "$status" -eq 0 ]
    run "$HR" run collect; [ "$status" -eq 0 ]
    [[ "$output" == *'"pending": []'* ]] && break
  done
  json_has "$output" 'len(d["collected"])==2 and d["pending"]==[] and d["failed"]=={} and d["drift"] is False'
  grep -q 'tab rename w1:t4 rv-.*: codex ✓' "$FAKE_HERDR_LOG"

  run "$HR" run phase aggregating
  [ "$status" -eq 0 ]
  run "$HR" run start-fixer
  [ "$status" -eq 0 ]
  json_has "$output" 'd["name"].endswith("-fixer") and d["state"]=="idle" and d["tab"]=="w1:t5"'
  grep -q "tab create --workspace w1 --cwd $REPO --label rv-.*: fixer --env HERDR_REVIEW_RUN=$RUN --env GIT_OPTIONAL_LOCKS=0 --env SECRET_TOKEN=s3cret-value --env HERDR_REVIEW_AGENT=hr.*-fixer --no-focus" "$FAKE_HERDR_LOG"
  FIXER="$(agent_name "$RUN" fixer)"
  echo "task" > "$RUN/fix-auto.md"
  run "$HR" run prompt "$FIXER" --file "$RUN/fix-auto.md"
  [ "$status" -eq 0 ]
  json_has "$output" 'd["state"]=="working"'
  grep -q "agent prompt $FIXER Read $RUN/fix-auto.md and follow it exactly." "$FAKE_HERDR_LOG"
  run "$HR" run prompt "$FIXER"
  [ "$status" -eq 1 ]

  run "$HR" run notify --title "herdr-review: нужен ответ" --body "1/1: x" --sound request
  [ "$status" -eq 0 ]
  json_has "$output" 'd["shown"] is True'
  grep -q 'tab rename w1:t2 rv-.*: orch ❓' "$FAKE_HERDR_LOG"
  run "$HR" status
  [[ "$output" == *"ожидает ответа"* ]]
  run "$HR" run phase disputed
  [ "$status" -eq 0 ]
  grep -q 'tab rename w1:t2 rv-.*: orch$' "$FAKE_HERDR_LOG"

  echo fixed > "$REPO/a.txt"; git -C "$REPO" commit -q -am "fix during the run"
  HEAD_FULL="$(git -C "$REPO" rev-parse HEAD)"
  run "$HR" run finish --commits abc123,def456
  [ "$status" -eq 0 ]
  json_has "$output" "d['phase']=='finished' and len(d['commits'])==1 and '$HEAD_FULL'.startswith(d['commits'][0]) and d['closed']==[]"
  grep -q 'notification show herdr-review: готово --body hr.*: отзывов 2, коммитов 1 --sound done' "$FAKE_HERDR_LOG"
  grep -q 'tab rename w1:t2 rv-.*: orch ✓' "$FAKE_HERDR_LOG"

  run "$HR" status --json
  [ "$status" -eq 0 ]
  json_has "$output" "d['phase']=='finished' and '$HEAD_FULL'.startswith(d['commits'][0]) and all('since_sec' in a for a in d['agents'].values())"
  run "$HR" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"phase: finished"* ]]
  [[ "$output" == *"collected"* ]]
  [[ "$output" == *"commits: ${HEAD_FULL:0:7}"* ]]
  unset HERDR_REVIEW_RUN
  run "$HR" status --run latest
  [ "$status" -eq 0 ]
  [[ "$output" == *"phase: finished"* ]]
}

@test "run: Codex exits after updating, restarts once in its pane, and finishes its review" {
  run "$HR" launch --json --reviewers codex
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"
  CODEX="$(agent_name "$RUN" 0)"
  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_prompt": {"*-codex": {"code": "agent_prompt_stalled", "message": "no activity"}},
 "screens": {"*-codex": "Update available · 0.159.2 → 0.160.0\n› 1. Update now\n  2. Skip\n",
             "w1:p3": "user@host$ \n"}}
EOF
  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  json_has "$output" 'list(d["agents"].values())[0]["state"]=="prompt_stalled"'

  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_get": {"*-codex": ["gone", "working", "done"]},
 "screens": {"w1:p3": "user@host$ \nUpdate available · 0.159.2 → 0.160.0\n› 1. Update now\n  2. Skip\nUpdating Codex via `installer`...\n🎉 Update ran successfully! Please restart Codex.\nuser@host$ "}}
EOF
  run "$HR" run wait
  [ "$status" -eq 0 ]
  json_has "$output" 'list(d["agents"].values())[0]["state"]=="working" and list(d["agents"].values())[0]["update_restarts"]==1 and len(d["pending"])==1'
  [ "$(grep -c "agent start $CODEX --kind codex --pane w1:p3 " "$FAKE_HERDR_LOG")" -eq 2 ]
  [ "$(grep -c "agent prompt $CODEX Read $RUN/prompts/codex.md and follow it exactly." "$FAKE_HERDR_LOG")" -eq 2 ]
  [ "$(grep -c 'tab create' "$FAKE_HERDR_LOG")" -eq 2 ]

  good_review "$RUN/reviews/codex.md"
  run "$HR" run wait
  [ "$status" -eq 0 ]
  run "$HR" run collect
  [ "$status" -eq 0 ]
  json_has "$output" 'len(d["collected"])==1 and d["pending"]==[] and d["failed"]=={}'
}

@test "run: Codex running-update deadline persists across commands and honors a live blocked dialog" {
  run "$HR" launch --json --reviewers codex
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"
  CODEX="$(agent_name "$RUN" 0)"
  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_get": {"*-codex": ["idle"]},
 "screens": {"*-codex": "Updating Codex via `installer`...\nDownloading Codex CLI\n",
             "w1:p3": "user@host$ \n"}}
EOF
  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  json_has "$output" 'list(d["agents"].values())[0]["codex_update_pending"] is True'
  DEADLINE="$(python3 -c 'import json,sys; print(next(iter(json.load(open(sys.argv[1]))["agents"].values()))["codex_update_deadline"])' "$RUN/status.json")"
  run "$HR" run prompt "$CODEX" --retry
  [ "$status" -eq 0 ]
  json_has "$(cat "$RUN/status.json")" "all(a['codex_update_deadline']==$DEADLINE and a['retries']==0 and a['collect_retries']==0 for a in d['agents'].values())"

  python3 - "$RUN/status.json" <<'PY'
import json, sys, time
from pathlib import Path
p = Path(sys.argv[1]); d = json.loads(p.read_text())
next(iter(d["agents"].values()))["codex_update_deadline"] = time.time() - 1
p.write_text(json.dumps(d))
PY
  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_get": {"*-codex": ["blocked"]},
 "screens": {"*-codex": "Updating Codex via `installer`...\nAuthentication required\n",
             "w1:p3": "user@host$ \nUpdating Codex via `installer`...\nSECRET_TOKEN=s3cret-value\nAuthentication required\n"}}
EOF
  run "$HR" run wait
  [ "$status" -eq 0 ]
  json_has "$output" 'list(d["agents"].values())[0]["state"]=="blocked" and len(d["pending"])==1'
  run "$HR" run collect
  [ "$status" -eq 0 ]
  json_has "$output" 'len(d["pending"])==1 and d["failed"]=={}'

  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_get": {"*-codex": ["idle"]},
 "screens": {"*-codex": "Updating Codex via `installer`...\nInstalling package\n",
             "w1:p3": "user@host$ \nUpdating Codex via `installer`...\nSECRET_TOKEN=s3cret-value\nInstalling package\n"}}
EOF
  run "$HR" run collect
  [ "$status" -eq 0 ]
  json_has "$output" 'd["pending"]==[] and list(d["failed"].values())==["Codex startup update did not finish within 600 seconds"]'
  json_has "$(cat "$RUN/status.json")" 'all(a["state"]=="failed" and a["codex_update_pending"] is False and a["last_screen"]=="user@host$ \nUpdating Codex via `installer`...\nSECRET_TOKEN=***\nInstalling package\n" and a["retries"]==0 and a["collect_retries"]==0 and a.get("update_restarts",0)==0 for a in d["agents"].values())'
  run "$HR" run wait
  [ "$status" -eq 0 ]
  json_has "$output" 'd["settled"] is True and list(d["agents"].values())[0]["state"]=="failed"'
  [ "$(grep -c "agent start $CODEX --kind codex --pane w1:p3 " "$FAKE_HERDR_LOG")" -eq 1 ]
  [ "$(grep -c "agent prompt $CODEX " "$FAKE_HERDR_LOG")" -eq 0 ]
  [ "$(grep -c "agent send-keys $CODEX " "$FAKE_HERDR_LOG")" -eq 0 ]
}

@test "run: old Codex update scrollback cannot recover a current ordinary startup failure" {
  run "$HR" launch --json --reviewers codex
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"
  CODEX="$(agent_name "$RUN" 0)"
  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_prompt": {"*-codex": {"code": "agent_prompt_stalled", "message": "startup stalled"}},
 "screens": {"*-codex": "Starting Codex...\n",
             "w1:p3": "user@host$ \nUpdating Codex via `installer`...\n🎉 Update ran successfully! Please restart Codex.\nuser@host$ \n"}}
EOF
  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  json_has "$output" 'list(d["agents"].values())[0]["state"]=="prompt_stalled"'

  cat > "$FAKE_HERDR_SCENARIO" <<'EOF'
{"agent_get": {"*-codex": ["gone"]},
 "screens": {"w1:p3": "user@host$ \nUpdating Codex via `installer`...\n🎉 Update ran successfully! Please restart Codex.\nuser@host$ \nStarting Codex...\nFatal: current startup failed\nuser@host$ "}}
EOF
  run "$HR" run wait
  [ "$status" -eq 0 ]
  json_has "$output" 'list(d["agents"].values())[0]["state"]=="gone" and d["pending"]==[] and list(d["agents"].values())[0]["update_restarts"]==0'
  json_has "$(cat "$RUN/status.json")" 'all(a["codex_update_seen"] is False and a["codex_update_pending"] is False and "Update ran successfully!" in a["last_screen"] and "Fatal: current startup failed" in a["last_screen"] for a in d["agents"].values())'
  [ "$(grep -c "agent start $CODEX --kind codex --pane w1:p3 " "$FAKE_HERDR_LOG")" -eq 1 ]
  [ "$(grep -c "agent prompt $CODEX Read $RUN/prompts/codex.md and follow it exactly." "$FAKE_HERDR_LOG")" -eq 1 ]
}

@test "run: grid layout splits the orchestrator pane" {
  run "$HR" launch --json --layout grid
  [ "$status" -eq 0 ]
  export HERDR_REVIEW_RUN="$(run_dir_of)"
  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  grep -q "pane split --pane w1:p2 --direction right --ratio 0.35 --cwd $REPO --env HERDR_REVIEW_RUN=$HERDR_REVIEW_RUN --env GIT_OPTIONAL_LOCKS=0 --env SECRET_TOKEN=s3cret-value --env HERDR_REVIEW_AGENT=hr.*-claude-opus --no-focus" "$FAKE_HERDR_LOG"
  grep -q 'pane split --pane w1:p3 --direction right --ratio 0.5' "$FAKE_HERDR_LOG"
  grep -q 'pane rename w1:p4 rv-.*: codex ⏳' "$FAKE_HERDR_LOG"
  [ "$(grep -c 'tab create' "$FAKE_HERDR_LOG")" -eq 1 ]
  run "$HR" run start-fixer
  [ "$status" -eq 0 ]
  grep -q 'pane split --pane w1:p2 --direction down --ratio 0.5' "$FAKE_HERDR_LOG"
}

@test "run: blocked-start reviewer is prompted after the dialog is resolved" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  export HERDR_REVIEW_RUN="$(run_dir_of)"
  echo '{"agent_start": {"*-codex": {"code": "agent_not_ready", "message": "blocked during startup"}}, "agent_get": {"*-codex": ["blocked", "idle"], "*-claude-opus": ["working"]}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  json_has "$output" '[a["state"] for n,a in d["agents"].items() if n.endswith("-codex")]==["blocked-start"]'
  run "$HR" run wait
  json_has "$output" 'd["reason"]=="blocked"'
  run "$HR" run wait
  json_has "$output" '[a["state"] for n,a in d["agents"].items() if n.endswith("-codex")]==["idle"]'
  CODEX="$(agent_name "$HERDR_REVIEW_RUN" 1)"
  run "$HR" run prompt "$CODEX"
  [ "$status" -eq 0 ]
  json_has "$output" 'd["state"]=="working"'
  grep -q "agent prompt $CODEX Read $HERDR_REVIEW_RUN/prompts/codex.md and follow it exactly." "$FAKE_HERDR_LOG"
}

@test "run: reviewer without a review file is re-prompted once, then failed" {
  run "$HR" launch --json --reviewers codex
  [ "$status" -eq 0 ]
  export HERDR_REVIEW_RUN="$(run_dir_of)"
  echo '{"agent_get": {"*-codex": ["idle"]}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" run start-reviewers
  run "$HR" run wait
  json_has "$output" 'list(d["agents"].values())[0]["state"]=="idle"'
  run "$HR" run collect
  json_has "$output" 'len(d["pending"])==1 and d["failed"]=={}'
  grep -q 'You have not written a valid review to' "$FAKE_HERDR_LOG"
  run "$HR" run wait
  run "$HR" run collect
  json_has "$output" 'len(d["failed"])==1 and "file missing" in list(d["failed"].values())[0]'
  run "$HR" status
  [[ "$output" == *"failed"* ]]
}

@test "run: drift is detected when a reviewer changes the tree" {
  run "$HR" launch --json --reviewers codex
  [ "$status" -eq 0 ]
  export HERDR_REVIEW_RUN="$(run_dir_of)"
  run "$HR" run start-reviewers
  echo oops >> "$REPO/a.txt"
  run "$HR" run collect
  json_has "$output" 'd["drift"] is True and "a.txt" in d["drift_status"]'
}

@test "run: fail records the reason and the pane screen" {
  run "$HR" launch --json --reviewers codex
  export HERDR_REVIEW_RUN="$(run_dir_of)"
  echo '{"screens": {"w1:p3": "Error: quota exceeded\n"}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" run start-reviewers
  CODEX="$(agent_name "$HERDR_REVIEW_RUN" 0)"
  run "$HR" run fail "$CODEX" --reason "quota"
  json_has "$output" 'd["state"]=="failed"'
  grep -q 'quota exceeded' "$HERDR_REVIEW_RUN/status.json"
  grep -q 'tab rename w1:t3 rv-.*: codex ✗' "$FAKE_HERDR_LOG"
}

@test "run: missing run directory is an error" {
  run "$HR" run collect
  [ "$status" -eq 1 ]
  [[ "$output" == *"HERDR_REVIEW_RUN"* ]]
  run "$HR" run collect --run /nonexistent
  [ "$status" -eq 1 ]
}

@test "run: autodecide switches the run one way and clears the question" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"

  run "$HR" run notify --title "нужен ответ" --sound request
  [ "$status" -eq 0 ]
  grep -q '"waiting_for_user": true' "$RUN/status.json"

  run "$HR" run autodecide
  [ "$status" -eq 0 ]
  json_has "$output" 'd["autodecide"] is True and d["was"] is False and d["switched_at"]'
  grep -q '"waiting_for_user": false' "$RUN/status.json"
  grep -q '"autodecide": true' "$RUN/status.json"
  grep -q 'tab rename w1:t2 rv-.*: orch$' "$FAKE_HERDR_LOG"

  run "$HR" status --run "$RUN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"autodecide: on"* ]]

  run "$HR" run autodecide
  [ "$status" -eq 0 ]
  json_has "$output" 'd["was"] is True'
}

@test "close: refuses a run in progress, then closes every tab once it has finished" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"
  run "$HR" run start-reviewers --run "$RUN"
  [ "$status" -eq 0 ]
  run "$HR" close
  [ "$status" -eq 1 ]
  [[ "$output" == *"still in phase reviewing"* ]]
  [ "$(grep -c 'tab close' "$FAKE_HERDR_LOG")" -eq 0 ]
  run "$HR" run finish --run "$RUN"
  [ "$status" -eq 0 ]
  run "$HR" close --json
  [ "$status" -eq 0 ]
  json_has "$output" 'd["closed"]==["w1:t2","w1:t3","w1:t4"] and d["failed"]=={}'
}

@test "close: a tab closed by hand is not an error; a failed close is" {
  run "$HR" launch --json
  RUN="$(run_dir_of)"
  run "$HR" run start-reviewers --run "$RUN"
  run "$HR" run finish --run "$RUN"
  "$HERDR_BIN" tab close w1:t3                        # by hand
  echo '{"close": {"w1:t4": {"code": "server_error", "message": "boom"}}}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" close
  [ "$status" -eq 1 ]
  [[ "$output" == *"уже закрыты: w1:t3"* ]]
  [[ "$output" == *"не удалось закрыть w1:t4: server_error: boom"* ]]
  echo '{}' > "$FAKE_HERDR_SCENARIO"
  run "$HR" close --json
  [ "$status" -eq 0 ]
  json_has "$output" 'd["closed"]==["w1:t4"] and "w1:t3" in d["already_closed"] and d["failed"]=={}'
}

@test "close: refuses a caller in another herdr session and leaves another run's tab open" {
  run "$HR" launch --json
  RUN="$(run_dir_of)"
  run "$HR" run start-reviewers --run "$RUN"
  run "$HR" run finish --run "$RUN"
  HERDR_SESSION=other HERDR_SOCKET_PATH="$TMP/herdr/sessions/other/herdr.sock" run "$HR" close
  [ "$status" -eq 1 ]
  [[ "$output" == *"was started in herdr session 'test'; run close from a pane of that session"* ]]
  [ "$(grep -c 'tab close' "$FAKE_HERDR_LOG")" -eq 0 ]
  echo '{"labels": {"w1:t3": "build"}}' > "$FAKE_HERDR_SCENARIO"   # the ID now names someone else's tab
  run "$HR" close --json
  [ "$status" -eq 0 ]
  json_has "$output" 'd["left_open"]==["w1:t3"] and d["already_closed"]==[] and "w1:t4" in d["closed"] and d["failed"]=={}'
  [ "$(grep -c 'tab close w1:t3' "$FAKE_HERDR_LOG")" -eq 0 ]
  run "$HR" close                                     # again, in text: the run's own tabs are closed by now
  [ "$status" -eq 0 ]
  [[ "$output" == *"оставлены открытыми (ID теперь у чужой вкладки): w1:t3"* ]]
  [[ "$output" == *"уже закрыты: w1:t2, w1:t4"* ]]
  [ "$(grep 'уже закрыты' <<< "$output" | grep -c 'w1:t3')" -eq 0 ]
}

@test "run: an opencode reviewer starts with its own server and the run's session config" {
  printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/opencode"; chmod +x "$TMP/bin/opencode"
  cat > "$XDG_CONFIG_HOME/herdr-review/config.yaml" <<EOF
profiles:
  claude-opus: {kind: claude, args: [--model, opus]}
  mimo: {kind: opencode, env: {OPENCODE_CONFIG_CONTENT: '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'}}
presets:
  default: {reviewers: [mimo], orchestrator: claude-opus, fixer: claude-opus}
settings: {runs_dir: $TMP/runs, checkin_sec: 1}
EOF
  chmod 600 "$XDG_CONFIG_HOME/herdr-review/config.yaml"
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"
  [ -f "$RUN/opencode.json" ]
  json_has "$(cat "$RUN/opencode.json")" "d['permission']=={'external_directory': {'$RUN/*': 'allow'}, 'read': {'*.env': 'deny', '*.env.*': 'deny', '*.env.example': 'allow'}, 'question': 'deny'}"
  [ "$(grep -c 'label rv-.*: orch .*OPENCODE_CONFIG' "$FAKE_HERDR_LOG")" -eq 0 ]   # a claude orchestrator gets nothing of OpenCode's

  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  grep -q "tab create --workspace w1 --cwd $REPO --label rv-.*: mimo --env HERDR_REVIEW_RUN=$RUN --env GIT_OPTIONAL_LOCKS=0 --env OPENCODE_CONFIG=$RUN/opencode.json --env OPENCODE_DISABLE_PROJECT_CONFIG=1 --env OPENCODE_CONFIG_CONTENT=.* --env HERDR_REVIEW_AGENT=hr.*-mimo --no-focus" "$FAKE_HERDR_LOG"
  grep -q 'agent start hr.*-mimo --kind opencode --pane w1:p3 --timeout 300000 -- --standalone$' "$FAKE_HERDR_LOG"
}
