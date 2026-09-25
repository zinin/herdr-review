# Shared setup for bats tests: fake herdr, temp HOME/XDG, config, git repo.
HR="$BATS_TEST_DIRNAME/../../bin/herdr-review"

setup_env() {
  export TMP="$(mktemp -d)"
  export HOME="$TMP/home"; mkdir -p "$HOME"
  export XDG_CONFIG_HOME="$TMP/xdg"; mkdir -p "$XDG_CONFIG_HOME/herdr-review"
  export HERDR_ENV=1 HERDR_WORKSPACE_ID=w1 HERDR_TAB_ID=w1:t1 HERDR_PANE_ID=w1:p1
  export HERDR_SESSION=test HERDR_SOCKET_PATH="$TMP/herdr/sessions/test/herdr.sock"
  export HERDR_BIN="$BATS_TEST_DIRNAME/../fake-herdr/herdr"
  export FAKE_HERDR_LOG="$TMP/herdr.log" FAKE_HERDR_SCENARIO="$TMP/scenario.json" FAKE_HERDR_STATE="$TMP/herdr.state"
  export HERDR_REVIEW_POLL_SEC=0.01
  unset HERDR_REVIEW_RUN HERDR_REVIEW_CONFIG
  echo '{}' > "$FAKE_HERDR_SCENARIO"
  mkdir -p "$TMP/bin"
  for x in claude codex; do printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/$x"; chmod +x "$TMP/bin/$x"; done
  export PATH="$TMP/bin:$PATH"
  cat > "$XDG_CONFIG_HOME/herdr-review/config.yaml" <<EOF
profiles:
  claude-opus: {kind: claude, args: [--model, opus], env: {SECRET_TOKEN: s3cret-value}}
  codex: {kind: codex, args: [-m, gpt-5.5]}
  ghost: {kind: nosuchbinary}
presets:
  default: {reviewers: [claude-opus, codex], orchestrator: claude-opus, fixer: claude-opus}
settings: {runs_dir: $TMP/runs, checkin_sec: 1}
EOF
  chmod 600 "$XDG_CONFIG_HOME/herdr-review/config.yaml"
  make_repo
}

make_repo() {
  export REPO="$TMP/repo"; mkdir -p "$REPO"; cd "$REPO"
  git init -q -b master
  git config user.email t@example.com; git config user.name T
  echo one > a.txt; git add a.txt; git commit -q -m init
  git switch -q -c feat; echo two > a.txt; git commit -q -am change
}

teardown_env() { rm -rf "$TMP"; }

project_dir_of() {  # runs/<basename>-<first 6 hex of sha256 of the resolved repo path>
  local repo="$(cd "$REPO" && pwd -P)"
  printf '%s/runs/%s-%s' "$TMP" "$(basename "$repo")" "$(printf '%s' "$repo" | sha256sum | cut -c1-6)"
}

run_dir_of() { readlink -f "$(project_dir_of)/latest"; }

agent_name() {  # $1 = run dir, $2 = jq-like path: reviewer index or "fixer"
  python3 - "$1" "$2" <<'PY'
import json, sys
run = json.load(open(sys.argv[1] + "/run.json"))
key = sys.argv[2]
print(run["fixer"]["name"] if key == "fixer" else run["reviewers"][int(key)]["name"])
PY
}

good_review() {  # $1 = path
  printf '### Strengths\nx\n### Critical Issues\nNone.\n### Important Issues\nNone.\n### Minor Issues\nNone.\n### Assessment\n**Ready to merge:** Yes\n' > "$1"
}

json_has() {  # $1 = json text, $2 = python expression over d
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert eval(sys.argv[2]), d' "$1" "$2"
}
