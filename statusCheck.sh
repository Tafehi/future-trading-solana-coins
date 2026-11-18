#!/bin/bash
#
# statusCheck.sh — is the box shut, is the bot alive, is it trading, is the data flowing?
#   sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh
#   sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh --evidence   # also upload the box's logs to S3 for
#                                         scripts/collect_evidence.sh (s3://<bucket>/diag/evidence-<UTC time>.tgz)
# Runs daily by itself (solana-future-coin-status.timer -> logs/status-daily.log). Exit code = number of FAILs.
# Ends with a REVIEW SUMMARY to paste to Claude. Every FAIL has a "-> remedy" line.

set -uo pipefail
BOT_DIR="/opt/solana-future-coin"
FAILS=0
SUMMARY=()

ok() { echo "  OK    $1"; }
warn() { echo "  WARN  $1"; SUMMARY+=("WARN $1"); }
fail() { echo "  FAIL  $1"; echo "        -> $2"; FAILS=$((FAILS + 1)); SUMMARY+=("FAIL $1"); }
age() { local f=$1; [ -f "$f" ] && echo $(( $(date -u +%s) - $(cat "$f") )) || echo 999999; }

echo "=== solana-future-coin statusCheck $(date -u +%FT%TZ) ==="

echo "0. box is shut"
if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -vqE '^(127\.0\.0\.1|\[::1\]|127\.0\.0\.53%lo):'; then
  fail "a port listens on a public interface: $(ss -ltnH | awk '{print $4}' | grep -vE '^(127|\[::1\])' | tr '\n' ' ')" \
    "find the process (ss -ltnp) and stop it; only 127.0.0.1:8080 should listen"
else
  ok "only loopback listeners"
fi
systemctl is-enabled sshd 2>/dev/null | grep -q masked && ok "sshd masked" || warn "sshd is not masked (re-run hostSetup.sh)"

echo "1. container"
STATE="$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}} restarts={{.RestartCount}}' solana-future-coin 2>/dev/null || echo missing)"
case "$STATE" in
  running\ healthy*) ok "solana-future-coin $STATE" ;;
  missing) fail "container solana-future-coin missing" "sudo systemctl restart solana-future-coin; journalctl -u solana-future-coin" ;;
  *) fail "solana-future-coin $STATE" "docker logs --tail 100 solana-future-coin" ;;
esac

echo "2. /health"
HEALTH="$(curl -sf --max-time 5 http://127.0.0.1:8080/health || echo '{}')"
echo "$HEALTH" | jq -c '{status, mode, account, loops, account_state}' 2>/dev/null || true
[ "$(echo "$HEALTH" | jq -r .status 2>/dev/null)" = "ok" ] && ok "every loop beating" \
  || fail "health not ok: $(echo "$HEALTH" | jq -r .status 2>/dev/null)" "docker logs --tail 200 solana-future-coin | grep -i error"
[ "$(echo "$HEALTH" | jq -r '.account_state.halted // 0' 2>/dev/null)" = "1" ] \
  && fail "account HALTED: $(echo "$HEALTH" | jq -r .account_state.halt_reason)" "ledger drift: read DEBUG.md before anything else"
[ "$(echo "$HEALTH" | jq -r '.account_state.paused // 0' 2>/dev/null)" = "1" ] \
  && fail "entries PAUSED: $(echo "$HEALTH" | jq -r .account_state.pause_reason)" "live reconcile mismatch: compare KuCoin positions with trade_log"

echo "3. config"
[ -f "${BOT_DIR}/.env" ] && ok ".env present ($(grep -c '=' "${BOT_DIR}/.env") keys)" || fail ".env missing" "sudo /usr/local/bin/solana-future-coin-env-refresh"
# The hourly refresh keeps the old .env when the secret cannot be read; say so, or a deleted/renamed secret goes unseen.
if systemctl is-failed --quiet solana-future-coin-env.service; then
  warn "secret solana-future-bot not readable: .env is the last good copy (sudo /usr/local/bin/solana-future-coin-env-refresh)"
else
  ok "secret solana-future-bot read by the last .env refresh"
fi
for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID KUCOIN_API_KEY KUCOIN_API_SECRET; do
  grep -q "^${k}=." "${BOT_DIR}/.env" 2>/dev/null && ok "$k set" || warn "$k not set in solana-future-bot"
done
grep -q '^KUCOIN_API_PASSPHRASE=.' "${BOT_DIR}/.env" 2>/dev/null && ok "KUCOIN_API_PASSPHRASE set" \
  || warn "KUCOIN_API_PASSPHRASE not set (needed before MODE=live)"
# Jupiter is this bot's ONLY source (split 2026-11-14): without the RPC URL it sees no trader and trades nothing.
grep -q '^SOLANA_RPC_URL=.' "${BOT_DIR}/.env" 2>/dev/null && ok "SOLANA_RPC_URL set (Jupiter on)" \
  || fail "SOLANA_RPC_URL not set: no source, the bot trades nothing" "add SOLANA_RPC_URL to the solana-future-bot secret"
[ -f "${BOT_DIR}/data/STOP" ] && fail "kill switch file present" "sudo rm ${BOT_DIR}/data/STOP when you want it to trade again"

echo "4. trades"
docker exec solana-future-coin python -m app.tools.trade_log --limit 15 2>/dev/null || warn "trade_log failed"
docker exec solana-future-coin python -m app.tools.golive_check 2>/dev/null | sed 's/^/  /' || true

echo "4b. wallet pool (tracked / watched / shadow copies / Jupiter history)"
docker exec solana-future-coin python -m app.tools.wallets_report 2>/dev/null | sed 's/^/  /' || warn "wallets_report failed"

echo "5. data durability"
B=$(age "${BOT_DIR}/logs/last-backup"); [ "$B" -lt 7800 ] && ok "backup ${B}s ago" || fail "backup ${B}s old" "sudo /usr/local/bin/solana-future-coin-backup"
S=$(age "${BOT_DIR}/logs/last-spool"); [ "$S" -lt 3600 ] && ok "spool upload ${S}s ago" \
  || fail "spool upload ${S}s old" "sudo /usr/local/bin/solana-future-coin-spool (AccessDenied = shared bucket policy lacks solana-future-coin-role)"
N=$(find "${BOT_DIR}/data/spool" -name '*.parquet' 2>/dev/null | wc -l)
[ "$N" -lt 50 ] && ok "$N files waiting in the spool" || warn "$N files waiting in the spool"

echo "6. errors in the last 24 h"
docker logs --since 24h solana-future-coin 2>&1 | grep -E 'ERROR|ALARM|Traceback' | tail -15 | sed 's/^/  /'

if [ "${1:-}" = "--evidence" ]; then
  echo "7. evidence for scripts/collect_evidence.sh"
  EV="$(mktemp -d)"
  {
    docker logs --since 72h solana-future-coin >"${EV}/container.log" 2>&1
    journalctl --since "72 hours ago" -u 'solana-future-coin*' --no-pager >"${EV}/journal.log" 2>&1
    cp "${BOT_DIR}"/logs/status-daily.log "${BOT_DIR}"/logs/hostsetup.log "${EV}/" 2>/dev/null
    cp -r "${BOT_DIR}/logs/app" "${EV}/app-logs" 2>/dev/null
    # Key NAMES only, never values: the .env holds the solana-future-bot secret.
    cut -d= -f1 "${BOT_DIR}/.env" >"${EV}/env-keys.txt" 2>/dev/null
    docker inspect solana-future-coin --format '{{.Config.Image}} {{.Image}} {{.State.StartedAt}}' >"${EV}/image.txt" 2>&1
    find "${BOT_DIR}/data" -maxdepth 3 -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' >"${EV}/data-files.txt" 2>/dev/null
  } || true
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  REGION="$(/usr/local/bin/solana-future-coin-region)"
  BUCKET="solana-future-coin-data-$(/usr/local/bin/solana-future-coin-account)"
  if tar -czf "/tmp/evidence-${TS}.tgz" -C "${EV}" . && aws s3 cp "/tmp/evidence-${TS}.tgz" \
    "s3://${BUCKET}/diag/evidence-${TS}.tgz" --region "${REGION}" --only-show-errors; then
    ok "evidence uploaded: s3://${BUCKET}/diag/evidence-${TS}.tgz"
  else
    fail "evidence upload failed" "check the instance role's s3-data policy; the tarball is /tmp/evidence-${TS}.tgz"
  fi
  rm -rf "${EV}"
fi

echo
echo "=== REVIEW SUMMARY (paste to Claude) ==="
echo "state: $STATE | health: $(echo "$HEALTH" | jq -c '{status,mode,account}' 2>/dev/null)"
for s in "${SUMMARY[@]+"${SUMMARY[@]}"}"; do echo "$s"; done
echo "VERDICT: $([ "$FAILS" -eq 0 ] && echo "ALL OK" || echo "$FAILS FAIL(S)")"
exit "$FAILS"
