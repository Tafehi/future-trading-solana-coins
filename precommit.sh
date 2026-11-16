#!/usr/bin/env bash
# precommit.sh — every check CI runs, locally, in one command. Run before every commit:
#   ./precommit.sh
# Exit code = number of failed stages; success prints ALL STAGES PASS. Never touches AWS: terraform validate runs with
# -backend=false in a throw-away TF_DATA_DIR and no credentials (global CLAUDE.md: never use local cloud credentials).
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
run "settings.toml boots in paper" env SETTINGS_FILE=app/settings.toml "$PY" -c \
  "from app.config import settings; assert settings.mode == 'paper' and settings.paper_start_balance == 10000"
run "live without passphrase refuses" bash -c \
  "! MODE=live KUCOIN_API_KEY=k KUCOIN_API_SECRET=s KUCOIN_API_PASSPHRASE=unset LIVE_START_BALANCE=1 $PY -c 'import app.config' 2>/dev/null"
run "bash -n" bash -n hostSetup.sh statusCheck.sh infra/user_data.sh scripts/mutants.sh scripts/verify_clone.sh scripts/collect_evidence.sh precommit.sh
run "shellcheck" shellcheck -S warning hostSetup.sh statusCheck.sh infra/user_data.sh scripts/mutants.sh scripts/verify_clone.sh scripts/collect_evidence.sh precommit.sh
run "terraform fmt" terraform -chdir=infra fmt -check -recursive
TFD="$(mktemp -d)"
run "terraform validate (offline)" env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
  AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null TF_DATA_DIR="$TFD" \
  bash -c "terraform -chdir=infra init -backend=false -input=false >/dev/null && terraform -chdir=infra validate"
rm -rf "$TFD"
if [ "${MUTANTS:-1}" = "1" ]; then
  run "mutants" env PY="$PY" scripts/mutants.sh
fi

if [ "$FAILED" -eq 0 ]; then echo "ALL STAGES PASS"; else echo "$FAILED STAGE(S) FAILED"; fi
exit "$FAILED"
