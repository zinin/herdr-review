#!/usr/bin/env bats
load helpers

setup() { setup_env; }
teardown() { teardown_env; }

@test "profiles: text and json" {
  run "$HR" profiles
  [ "$status" -eq 0 ]
  [[ "$output" == *"claude-opus"* ]]
  [[ "$output" == *"env=SECRET_TOKEN"* ]]
  [[ "$output" != *"s3cret-value"* ]]
  run "$HR" profiles --json
  [ "$status" -eq 0 ]
  json_has "$output" 'd["profiles"]["claude-opus"]["env_keys"]==["SECRET_TOKEN"] and d["presets"]["default"]["fixer"]=="claude-opus"'
}

@test "status: no runs yet" {
  run "$HR" status
  [ "$status" -eq 1 ]
  [[ "$output" == *"no runs"* ]]
}

@test "status: explicit run dir" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  run "$HR" status --run "$(run_dir_of)"
  [ "$status" -eq 0 ]
  [[ "$output" == *"phase: reviewing"* ]]
}

@test "status: positional latest" {
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  export HERDR_REVIEW_RUN="/nonexistent-run-dir"
  run "$HR" status latest
  [ "$status" -eq 0 ]
  [[ "$output" == *"phase: reviewing"* ]]
  run "$HR" status --run latest
  [ "$status" -eq 0 ]
  [[ "$output" == *"phase: reviewing"* ]]
}

@test "version flag" {
  run "$HR" --version
  [ "$status" -eq 0 ]
  [[ "$output" == "herdr-review 0.2.0" ]]
}
