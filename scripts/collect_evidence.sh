#!/usr/bin/env bash
# Collect what is needed to understand the Solana bot's trades and Jupiter WALLETS into one local folder, then analyse
# it offline.
#
# READ-ONLY. Run it YOURSELF, on your own machine, with your own AWS login (CLAUDE.md rule 1: the assistant never uses
# AWS credentials). Its only AWS calls are: sts get-caller-identity, s3 ls, and s3 cp FROM a bucket TO a local folder.
# It never writes to AWS and never reads the secret (scripts/checks.py `evidence-read-only` checks every aws line).
#
# Step 1 (optional), on the box (Session Manager), so the box's logs are in S3 too:
#   sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh --evidence
# Step 2, here:
#   ./scripts/collect_evidence.sh               # into audit/<DD.MM.YYYY UTC>/ (git ignores audit/)
#   LEGACY=1 ./scripts/collect_evidence.sh      # the OLD futcoins box's backup: the Jupiter wallet history recorded
#                                               # before the 2026-11-14 split (jup_* tables, Jupiter grades)
#   DAYS=7 ./scripts/collect_evidence.sh        # recorded Parquet of the last 7 days (default 3)
#
# What it downloads:
#   live/<db>              the hourly backup of the TRADER's file: signals, positions, shadow copies, ledger, feed
#   live/scout.db          the hourly backup of the SCOUT's file (D-30): Jupiter owners, episodes, grades, picks
#   daily/ listing         which daily backups exist
#   diag/                  the NEWEST evidence-*.tgz from step 1 (container log, journal, app logs), unpacked
#   futures/ (shared)      the listing, and the last DAYS days of solana-bot/{grades,positions,shadow}. Not
#                          jupiter/positions: it is a snapshot every minute and large (the listing shows it)
# Then, with no AWS call, using the repo's .venv: evidence_report (every table as CSV + summary.md by wallet, source,
# reason, tier, day; the wallet pool; the shadow record), trade_log, golive_check, wallets_report, and
# criteria_backtest (analysis/criteria/criteria.md: which wallet rules pick the most wallets that stay good). INDEX.md at the top
# lists what came, what failed, and the folder to hand to the assistant.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
REPO="$PWD"
REGION=eu-central-1
ACCOUNT_WANTED=267850879774
if [ "${LEGACY:-0}" = 1 ]; then
  DATA="futcoins-bot-data-${ACCOUNT_WANTED}"
  DB_KEY="live/futcoins.db"
else
  DATA="solana-future-coin-data-${ACCOUNT_WANTED}"
  DB_KEY="live/solana-future-coin.db"
fi
SHARED="bigdata-shared-history-${ACCOUNT_WANTED}"
DAYS="${DAYS:-3}"
DAY="$(date -u +%d.%m.%Y)"
OUT="${1:-$REPO/audit/$DAY}"
mkdir -p "$OUT"/{bucket,diag,listing,parquet,analysis} || exit 1
OUT="$(cd "$OUT" && pwd)"
PY="${PY:-$REPO/.venv/bin/python}"
FAILED=()

say() { echo "── $*"; }
# run FILE CMD...: the command's output into FILE; its errors into FILE.err (kept only when it failed).
run() {
  local f="$1"
  shift
  if "$@" >"$f" 2>"$f.err"; then
    rm -f "$f.err"
  else
    echo "   failed: $(head -c 300 "$f.err")"
    FAILED+=("${f#"$OUT"/}")
    return 1
  fi
}

# The first AWS CLI on this machine that runs (an old Intel /usr/local/bin/aws can fail with "Bad CPU type").
AWS=""
for c in $(type -ap aws) /opt/homebrew/bin/aws /usr/local/bin/aws; do
  if [ -x "$c" ] && "$c" --version >/dev/null 2>&1; then
    AWS="$c"
    break
  fi
done
[ -n "$AWS" ] || { echo "no working aws CLI found"; exit 1; }

say "who am I"
GOT="$("$AWS" sts get-caller-identity --query Account --output text 2>/dev/null || true)"
[ "$GOT" = "$ACCOUNT_WANTED" ] || { echo "logged in to '${GOT:-nothing}', expected $ACCOUNT_WANTED"; exit 1; }

say "database backup s3://$DATA/$DB_KEY"
run "$OUT/listing/bucket.txt" "$AWS" s3 ls "s3://$DATA/" --recursive --region "$REGION"
DB="$OUT/bucket/$(basename "$DB_KEY")"
run "$OUT/listing/db-copy.txt" "$AWS" s3 cp "s3://$DATA/$DB_KEY" "$DB" --region "$REGION" --only-show-errors
# D-30: the wallet pool is in the scout's own file. A backup from before the split into two programs has none.
SCOUT_DB=""
if [ "${LEGACY:-0}" != 1 ] && grep -q ' live/scout\.db$' "$OUT/listing/bucket.txt" 2>/dev/null; then
  SCOUT_DB="$OUT/bucket/scout.db"
  run "$OUT/listing/scout-db-copy.txt" "$AWS" s3 cp "s3://$DATA/live/scout.db" "$SCOUT_DB" --region "$REGION" \
    --only-show-errors || SCOUT_DB=""
fi

say "newest box evidence (statusCheck.sh --evidence)"
NEWEST="$(awk '$4 ~ /^diag\/evidence-.*\.tgz$/ {print $4}' "$OUT/listing/bucket.txt" 2>/dev/null | sort | tail -1)"
if [ -n "$NEWEST" ]; then
  if run "$OUT/listing/diag-copy.txt" "$AWS" s3 cp "s3://$DATA/$NEWEST" "$OUT/diag/" --region "$REGION" \
    --only-show-errors; then
    tar -xzf "$OUT/diag/$(basename "$NEWEST")" -C "$OUT/diag" 2>/dev/null || FAILED+=("diag unpack")
  fi
else
  echo "   none yet: run statusCheck.sh --evidence on the box first if you want the logs"
fi

say "shared bucket futures/ (listing + the last $DAYS days of this bot's solana-bot/ rows)"
run "$OUT/listing/futures.txt" "$AWS" s3 ls "s3://$SHARED/futures/" --recursive --region "$REGION"
for i in $(seq 0 $((DAYS - 1))); do
  D="$(date -u -v-"${i}"d +%F 2>/dev/null || date -u -d "-${i} day" +%F)"
  for ds in solana-bot/grades solana-bot/positions solana-bot/shadow solana-bot/episodes; do
    "$AWS" s3 cp "s3://$SHARED/futures/$ds/day=$D/" "$OUT/parquet/$ds/day=$D/" --recursive --region "$REGION" \
      --only-show-errors >/dev/null 2>&1 || true
  done
done

say "offline analysis (no AWS)"
if [ -s "$DB" ] && [ -x "$PY" ]; then
  if [ -s "$SCOUT_DB" ]; then
    run "$OUT/analysis/evidence_report.txt" "$PY" -m app.tools.evidence_report --db "$DB" --scout-db "$SCOUT_DB" \
      --out "$OUT/analysis"
  else
    run "$OUT/analysis/evidence_report.txt" "$PY" -m app.tools.evidence_report --db "$DB" --out "$OUT/analysis"
  fi
  ACCOUNTS="$("$PY" -c "import sqlite3,sys;c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);print(' '.join(r[0] for r in c.execute('SELECT DISTINCT account FROM positions')))" "$DB" 2>/dev/null)"
  for A in $ACCOUNTS; do
    run "$OUT/analysis/trade_log-$A.txt" "$PY" -m app.tools.trade_log --db "$DB" --account "$A" --limit 500
    "$PY" -m app.tools.golive_check --db "$DB" --account "$A" >"$OUT/analysis/golive-$A.txt" 2>&1 || true
  done
  # One file before D-30 (the pool and the shadow copies both in it), two after.
  run "$OUT/analysis/wallets_report.txt" "$PY" -m app.tools.wallets_report --scout-db "${SCOUT_DB:-$DB}" \
    --trader-db "$DB"
  # Which wallet rules pick the most wallets that stay good (walk-forward, no look-ahead; the fuller file is used).
  if [ -s "$SCOUT_DB" ]; then
    run "$OUT/analysis/criteria_backtest.txt" "$PY" -m app.tools.criteria_backtest --db "$DB" --db "$SCOUT_DB" \
      --out "$OUT/analysis/criteria"
  else
    run "$OUT/analysis/criteria_backtest.txt" "$PY" -m app.tools.criteria_backtest --db "$DB" \
      --out "$OUT/analysis/criteria"
  fi
else
  echo "   skipped: no database downloaded, or no $PY (python3.12 -m venv .venv && .venv/bin/pip install -r app/requirements-dev.txt)"
  FAILED+=("analysis")
fi

{
  echo "# Evidence $DAY (UTC)"
  echo
  echo "- Bucket: \`$DATA\` (database \`$DB_KEY\`)$([ "${LEGACY:-0}" = 1 ] && echo ' — LEGACY futcoins box')"
  echo "- Hand this folder to the assistant: \`$OUT\`"
  echo "- Start with \`analysis/summary.md\`, then \`analysis/tables/scout-wallets.csv\` (the pool; \`wallets.csv\` in a backup from before D-30) and \`analysis/tables/positions.csv\`."
  echo "- Box logs: \`diag/\` ($([ -n "$NEWEST" ] && echo "$NEWEST" || echo 'none: run statusCheck.sh --evidence'))"
  echo
  echo "## Failed steps"
  if [ ${#FAILED[@]} -eq 0 ]; then echo "none"; else printf -- '- %s\n' "${FAILED[@]}"; fi
} >"$OUT/INDEX.md"
say "done: $OUT/INDEX.md"
exit "${#FAILED[@]}"
