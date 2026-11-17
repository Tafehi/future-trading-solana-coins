#!/usr/bin/env bash
# precommit.sh — every check, locally, in one command. Run before every commit:
#   ./precommit.sh
# Exit code = number of failed stages; success prints ALL STAGES PASS. Offline: no network, no cloud credentials.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
PY=${PY:-.venv/bin/python}
FAILED=0

run() {
  local name=$1
  shift
  local out
  if out="$("$@" 2>&1)"; then
    echo "ok    $name"
  else
    echo "FAIL  $name"
    echo "$out" | tail -30 | sed 's/^/      /'
    FAILED=$((FAILED + 1))
  fi
}

run "repo checks self-test" "$PY" scripts/checks.py --self-test
run "repo checks" "$PY" scripts/checks.py
run "ruff check" bash -c "cd app && ../$PY -m ruff check ."
run "ruff format" bash -c "cd app && ../$PY -m ruff format --check ."
run "mypy" bash -c "cd app && ../$PY -m mypy . --ignore-missing-imports"
run "pytest" "$PY" -m pytest app/tests -q -p no:cacheprovider
run "the tracer CLI starts" "$PY" -m app.tools.wallet_trace --help
run "bash -n" bash -n scripts/mutants.sh scripts/verify_clone.sh precommit.sh
if command -v shellcheck >/dev/null; then
  run "shellcheck" shellcheck -S warning scripts/mutants.sh scripts/verify_clone.sh precommit.sh
fi
if [ "${MUTANTS:-1}" = "1" ]; then
  run "mutants" env PY="$PY" scripts/mutants.sh
fi

if [ "$FAILED" -eq 0 ]; then echo "ALL STAGES PASS"; else echo "$FAILED STAGE(S) FAILED"; fi
exit "$FAILED"
