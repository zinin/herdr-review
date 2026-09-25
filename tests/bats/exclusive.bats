#!/usr/bin/env bats
load helpers

setup() { setup_env; }
teardown() { teardown_env; }

@test "exclusive: two calls started together take turns" {
  cat > "$TMP/job.sh" <<'EOF'
#!/bin/sh
python3 -c 'import time; print(time.time())' > "$1.start"
sleep 1
python3 -c 'import time; print(time.time())' > "$1.end"
EOF
  chmod +x "$TMP/job.sh"
  "$HR" exclusive -- "$TMP/job.sh" "$TMP/a" &
  first=$!
  "$HR" exclusive -- "$TMP/job.sh" "$TMP/b" &
  second=$!
  wait "$first"
  wait "$second"
  python3 - "$TMP" <<'PY'
import sys
t = sys.argv[1]
read = lambda name: float(open(f"{t}/{name}").read())
a, b = (read("a.start"), read("a.end")), (read("b.start"), read("b.end"))
assert a[1] <= b[0] or b[1] <= a[0], (a, b)
PY
}
