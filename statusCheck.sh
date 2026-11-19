#!/bin/bash
#
# statusCheck.sh — is the box shut, are both programs alive, is it trading, is the data flowing?
#   sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh
#   sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh --evidence   # also upload the box's logs to S3 for
#                                         scripts/collect_evidence.sh (s3://<bucket>/diag/evidence-<UTC time>.tgz)
# Runs daily by itself (solana-future-coin-status.timer -> logs/status-daily.log). Exit code = number of FAILs.
# Ends with a REVIEW SUMMARY to paste to Claude. Every FAIL has a "-> remedy" line.
#
# Two containers from one image since D-30: the SCOUT (finds, grades, picks; /health on 8080) and the TRADER (feed,
# copies, exits, Telegram; /health on 8081). trade_log and golive_check run in the trader, wallets_report in the scout.

set -uo pipefail
BOT_DIR="/opt/solana-future-coin"
SCOUT="solana-future-coin-scout"
TRADER="solana-future-coin-trader"
OLD="solana-future-coin"
FAILS=0
SUMMARY=()

ok() { echo "  OK    $1"; }
warn() { echo "  WARN  $1"; SUMMARY+=("WARN $1"); }
fail() { echo "  FAIL  $1"; echo "        -> $2"; FAILS=$((FAILS + 1)); SUMMARY+=("FAIL $1"); }
age() { local f=$1; [ -f "$f" ] && echo $(( $(date -u +%s) - $(cat "$f") )) || echo 999999; }
mtime_age() { local f=$1; [ -f "$f" ] && echo $(( $(date -u +%s) - $(stat -c %Y "$f") )) || echo 999999; }

echo "=== solana-future-coin statusCheck $(date -u +%FT%TZ) ==="

echo "0. box is shut"
if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -vqE '^(127\.0\.0\.1|\[::1\]|127\.0\.0\.53%lo):'; then
  fail "a port listens on a public interface: $(ss -ltnH | awk '{print $4}' | grep -vE '^(127|\[::1\])' | tr '\n' ' ')" \
    "find the process (ss -ltnp) and stop it; only 127.0.0.1:8080 (scout) and :8081 (trader) should listen"
else
  ok "only loopback listeners"
fi
systemctl is-enabled sshd 2>/dev/null | grep -q masked && ok "sshd masked" || warn "sshd is not masked (re-run hostSetup.sh)"

echo "1. containers"
STATES=""
for C in "$SCOUT" "$TRADER"; do
  STATE="$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}} restarts={{.RestartCount}}' "$C" 2>/dev/null || echo missing)"
  STATES="${STATES}${C#solana-future-coin-}: ${STATE}; "
  case "$STATE" in
    running\ healthy*) ok "$C $STATE" ;;
    missing) fail "container $C missing" "sudo systemctl restart solana-future-coin; journalctl -u solana-future-coin" ;;
    *) fail "$C $STATE" "docker logs --tail 100 $C" ;;
  esac
done
# The single-bot container of before D-30 must be gone (docker-compose up --remove-orphans removes it): if it still
# ran next to the trader, two programs would trade one account.
if docker inspect "$OLD" >/dev/null 2>&1; then
  fail "the old single-bot container $OLD still exists" "sudo systemctl restart solana-future-coin (runs docker-compose up -d --remove-orphans)"
else
  ok "old container $OLD gone"
fi

echo "2. /health"
SH="$(curl -s --max-time 5 http://127.0.0.1:8080/health || true)"; [ -n "$SH" ] || SH='{}'
TH="$(curl -s --max-time 5 http://127.0.0.1:8081/health || true)"; [ -n "$TH" ] || TH='{}'
echo "  scout:  $(echo "$SH" | jq -c '{status, loops, feed_poll_age_s, pick_age_s, tracked, watched}' 2>/dev/null)"
echo "  trader: $(echo "$TH" | jq -c '{status, mode, account, loops, copy_list}' 2>/dev/null)"
echo "  feed:   $(echo "$TH" | jq -c '.feed | {connected, subscriptions, wanted, down_for_s, notifications, errors, refused, last_open_lag_s, signals}' 2>/dev/null)"
[ "$(echo "$SH" | jq -r .status 2>/dev/null)" = "ok" ] && ok "scout: every loop beating, feed polled, list picked" \
  || fail "scout health not ok: $(echo "$SH" | jq -r .status 2>/dev/null)" "docker logs --tail 200 $SCOUT | grep -iE 'error|warn'"
[ "$(echo "$TH" | jq -r .status 2>/dev/null)" = "ok" ] && ok "trader: every loop beating, copy list fresh, feed up" \
  || fail "trader health not ok: $(echo "$TH" | jq -r .status 2>/dev/null)" "docker logs --tail 200 $TRADER | grep -iE 'error|warn|ws:'"
SUBS="$(echo "$TH" | jq -r '.feed.subscriptions // 0' 2>/dev/null)"
WANT="$(echo "$TH" | jq -r '.feed.wanted // 0' 2>/dev/null)"
if [ "${WANT:-0}" -gt 0 ] && [ "${SUBS:-0}" -lt "${WANT:-0}" ]; then
  warn "websocket holds ${SUBS} of ${WANT} owner subscriptions (BUG B-27: Chainstack limits unverified; the poll covers the rest)"
else
  ok "websocket holds ${SUBS} of ${WANT} owner subscriptions"
fi
[ "$(echo "$TH" | jq -r '.account_state.halted // 0' 2>/dev/null)" = "1" ] \
  && fail "account HALTED: $(echo "$TH" | jq -r .account_state.halt_reason)" "ledger drift: read DEBUG.md before anything else"
[ "$(echo "$TH" | jq -r '.account_state.paused // 0' 2>/dev/null)" = "1" ] \
  && fail "entries PAUSED: $(echo "$TH" | jq -r .account_state.pause_reason)" "live reconcile mismatch: compare KuCoin positions with trade_log"

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
# Jupiter is this bot's ONLY source (split 2026-11-14): without the RPC URL it sees no trader and trades nothing. The
# trader's websocket uses the same host and key (wss://) unless SOLANA_WS_URL is set.
grep -q '^SOLANA_RPC_URL=.' "${BOT_DIR}/.env" 2>/dev/null && ok "SOLANA_RPC_URL set (Jupiter on)" \
  || fail "SOLANA_RPC_URL not set: no source, the bot trades nothing" "add SOLANA_RPC_URL to the solana-future-bot secret"
[ -f "${BOT_DIR}/data/STOP" ] && fail "kill switch file present" "sudo rm ${BOT_DIR}/data/STOP when you want it to trade again"

echo "4. trades (trader)"
docker exec "$TRADER" python -m app.tools.trade_log --limit 15 2>/dev/null || warn "trade_log failed"
docker exec "$TRADER" python -m app.tools.golive_check 2>/dev/null | sed 's/^/  /' || true

echo "4b. wallet pool (scout: tracked / watched / shadow copies / Jupiter history)"
docker exec "$SCOUT" python -m app.tools.wallets_report 2>/dev/null | sed 's/^/  /' || warn "wallets_report failed"

echo "4c. copy speed (trader, last 24 h: seconds from the trader's open to our signal, by feed)"
sqlite3 -readonly "${BOT_DIR}/data/solana-future-coin.db" \
  "SELECT '  ' || via || ': ' || COUNT(*) || ' opens, ' || ROUND(AVG(ts - open_time), 1) || ' s average, ' ||
          ROUND(MAX(ts - open_time), 1) || ' s worst' FROM signals
   WHERE kind = 'OPEN' AND open_time IS NOT NULL AND ts > strftime('%s', 'now') - 86400 GROUP BY via" 2>/dev/null \
  || warn "copy speed not readable"

echo "5. data durability"
B=$(age "${BOT_DIR}/logs/last-backup"); [ "$B" -lt 7800 ] && ok "backup ${B}s ago" || fail "backup ${B}s old" "sudo /usr/local/bin/solana-future-coin-backup"
for NAME in solana-future-coin.db scout.db; do
  A=$(mtime_age "${BOT_DIR}/backup/${NAME}")
  [ "$A" -lt 7800 ] && ok "backup copy of ${NAME} ${A}s old" || fail "backup copy of ${NAME} ${A}s old" "sudo /usr/local/bin/solana-future-coin-backup"
done
S=$(age "${BOT_DIR}/logs/last-spool"); [ "$S" -lt 3600 ] && ok "spool upload ${S}s ago" \
  || fail "spool upload ${S}s old" "sudo /usr/local/bin/solana-future-coin-spool (AccessDenied = shared bucket policy lacks solana-future-coin-role)"
N=$(find "${BOT_DIR}/data/spool" -name '*.parquet' 2>/dev/null | wc -l)
[ "$N" -lt 50 ] && ok "$N files waiting in the spool" || warn "$N files waiting in the spool"
echo "  memory (limits: scout 450m, trader 300m; BUG B-16):"
docker stats --no-stream --format '    {{.Name}} {{.MemUsage}} {{.CPUPerc}}' "$SCOUT" "$TRADER" 2>/dev/null || true

echo "6. errors in the last 24 h"
for C in "$SCOUT" "$TRADER"; do
  docker logs --since 24h "$C" 2>&1 | grep -E 'ERROR|ALARM|Traceback' | tail -10 | sed "s/^/  ${C#solana-future-coin-}: /"
done

if [ "${1:-}" = "--evidence" ]; then
  echo "7. evidence for scripts/collect_evidence.sh"
  EV="$(mktemp -d)"
  {
    docker logs --since 72h "$SCOUT" >"${EV}/scout.log" 2>&1
    docker logs --since 72h "$TRADER" >"${EV}/trader.log" 2>&1
    journalctl --since "72 hours ago" -u 'solana-future-coin*' --no-pager >"${EV}/journal.log" 2>&1
    cp "${BOT_DIR}"/logs/status-daily.log "${BOT_DIR}"/logs/hostsetup.log "${EV}/" 2>/dev/null
    cp -r "${BOT_DIR}/logs/app" "${EV}/app-logs" 2>/dev/null
    # Key NAMES only, never values: the .env holds the solana-future-bot secret.
    cut -d= -f1 "${BOT_DIR}/.env" >"${EV}/env-keys.txt" 2>/dev/null
    for C in "$SCOUT" "$TRADER"; do
      docker inspect "$C" --format "$C {{.Config.Image}} {{.Image}} {{.State.StartedAt}}" >>"${EV}/image.txt" 2>&1
    done
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
echo "state: ${STATES}| scout: $(echo "$SH" | jq -c '{status}' 2>/dev/null) | trader: $(echo "$TH" | jq -c '{status,mode,account}' 2>/dev/null)"
for s in "${SUMMARY[@]+"${SUMMARY[@]}"}"; do echo "$s"; done
echo "VERDICT: $([ "$FAILS" -eq 0 ] && echo "ALL OK" || echo "$FAILS FAIL(S)")"
exit "$FAILS"
