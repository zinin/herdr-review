#!/usr/bin/env bash
# Run unit tests and bats tests. Usage: tests/run.sh [unit|bats]
set -euo pipefail
cd "$(dirname "$0")/.."
what="${1:-all}"
if [ "$what" = all ] || [ "$what" = unit ]; then
  python3 -m unittest discover -s tests/unit -t . -v
fi
if [ "$what" = all ] || [ "$what" = bats ]; then
  if [ -d tests/bats ] && command -v bats >/dev/null 2>&1; then
    bats tests/bats
  elif [ "$what" = bats ]; then
    echo "bats tests requested but cannot run (no tests/bats or bats not installed)" >&2
    exit 1
  else
    echo "bats tests skipped (no tests/bats or bats not installed)" >&2
  fi
fi
