#!/usr/bin/env bash
# Mutation check: break each key rule on purpose and prove its test goes red, then restore the file.
# A test that has never failed proves nothing (global CLAUDE.md, "The Bug Loop Rule").
#
#   scripts/mutants.sh            run every mutant; exit code = number that SURVIVED (0 = all killed)
#
# PYTHONDONTWRITEBYTECODE=1 is required: two mutants of one file with the same size written in the same second
# leave a stale .pyc that Python trusts, and the second mutant then "survives" without ever running (DEBUG D-1).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv/bin/python}
export PYTHONDONTWRITEBYTECODE=1
survived=0
backup=$(mktemp)

mut() { # file old new pytest-k-expression
  local file=$1 old=$2 new=$3 sel=$4
  cp "$file" "$backup"
  if ! "$PY" -c 'import sys;p,o,n=sys.argv[1:];s=open(p).read();assert o in s;open(p,"w").write(s.replace(o,n,1))' \
      "$file" "$old" "$new"; then
    echo "STALE MUTANT (text not found): $file :: $old"; survived=$((survived + 1)); return
  fi
  find app -name '__pycache__' -type d -prune -exec rm -rf {} +
  if "$PY" -m pytest app/tests -q -p no:cacheprovider -k "$sel" >/dev/null 2>&1; then
    echo "SURVIVED: $file :: $old"; survived=$((survived + 1))
  else
    echo "killed:   $sel"
  fi
  cp "$backup" "$file"
}

mut app/trading/risk.py 'round(abs(drift), 6) > ledger' 'abs(drift) > ledger' 'exactly_the_tolerance'
mut app/trading/risk.py 'round(abs(drift), 6) > ledger.DRIFT_TOLERANCE' 'round(abs(drift), 6) > 1.0' 'cent_of_drift'
mut app/config.py '("KUCOIN_API_PASSPHRASE", self.kucoin_api_passphrase),' '' 'live_without_a_credential'
mut app/config.py 'if self.live_start_balance <= 0:' 'if self.live_start_balance < 0:' 'live_without_a_balance'
mut app/trading/executor.py 'notional > contract.max_risk_limit:' 'notional > contract.max_risk_limit * 2:' 'risk_limit_boundary'
mut app/trading/executor.py 'if leverage > contract.max_leverage:' 'if leverage > contract.max_leverage + 1:' 'max_leverage_boundary'
mut app/trading/executor.py 'if contracts < lot - 1e-12:' 'if contracts < 0:' 'refuse_what_kucoin'
mut app/trading/paper_executor.py 'await self._sleep(self.delay_s)' 'pass' 'fresh_book'
mut app/data/kucoin_private.py 'ts + method.upper() + endpoint + body' 'ts + method.upper() + endpoint' 'prehash or pinned'
mut app/data/kucoin_private.py '"KC-API-PASSPHRASE": sign(secret, passphrase)' '"KC-API-PASSPHRASE": passphrase' 'prehash'
mut app/notifications/digest.py 'await meta.put(conn, LAST_END_KEY, end)
        text' 'text' 'sends_one_window_once'
mut app/notifications/digest.py 'if text is None:
            continue' 'if text is None:
            text = "empty"' 'sends_one_window_once'
mut app/notifications/digest.py 'ALARM_REPEAT_S:' 'ALARM_REPEAT_S - 1:' 'alarm_repeats'
mut app/trading/risk.py 'if t <= after or t > at:' 'if t > at:' 'funding_is_applied_once'
mut app/jobs/jupiter.py 'and p.open_time > last - 5' '' 'old_position_seen_late'
# (The update_time skip in process_updates is NOT mutated: it only saves rewrites; `old is None` is what stops a
#  second OPEN, and that is the mutant below.)
mut app/jobs/jupiter.py 'fresh_open = old is None and' 'fresh_open =' 'size_increase'
mut app/data/jupiter_perps.py 'ACCOUNT_SIZE = 216' 'ACCOUNT_SIZE = 210' 'real_accounts or change_feed'
mut app/data/jupiter_perps.py 'side = SIDES.get(data[144])' 'side = SIDES.get(data[143])' 'decode_the_slice'
mut app/trading/copier.py 'if score < victim.score + settings.replace_min_score_gap:' \
    'if score <= victim.score + settings.replace_min_score_gap:' 'much_better_wallet_replaces'
mut app/trading/copier.py 'if at - float(s["ts"]) > settings.signal_max_age_s:' 'if False:' 'refusals'
mut app/trading/futures_paper.py 'if round(price_move_pct(side, entry, mark), ROUND) <= -stop_pct:' \
    'if round(price_move_pct(side, entry, mark), ROUND) < -stop_pct:' 'six_percent'
mut app/record/recorder.py 'del rows[: len(rows) - MAX_BUFFER_ROWS]' 'pass' 'failing_spool'
mut app/record/recorder.py 'os.replace(path + ".tmp", path)' 'pass' 'shared_bucket_layout'
mut app/settings.toml 'JUPITER_GRADE_MINUTES = 5.0' 'JUPITER_GRADE_MINUTES = 60.0' 'settings_file_loads'
mut app/main.py 'claim("jupiter_grade", interval_s=settings.jupiter_grade_minutes * 60)' 'claim("jupiter_grade", interval_s=3600)' 'wallet_list_intervals'

# Items 1-5 (2026-11-13, more wallets)
mut app/data/jupiter_events.py 'digest()[:8][::-1]' 'digest()[:8]' 'event_tag or real_close'
mut app/data/jupiter_events.py 'final = next((e for e in reversed(evs) if e.closes), None)' 'final = evs[-1]' 'full_close'
mut app/jobs/jupiter_history.py 'if used >= settings.jupiter_history_calls_per_day:' \
    'if used > settings.jupiter_history_calls_per_day:' 'out_of_budget'
mut app/jobs/jupiter_history.py 'history_from = since if full else oldest' 'history_from = oldest' 'replaces_feed'
mut app/jobs/jupiter.py 'COALESCE(o.history_from, o.first_seen)' 'o.first_seen' 'day_one'
mut app/core/selection.py 'and c.shadow_mean <= settings.shadow_min_mean_return_pct' \
    'and c.shadow_mean < settings.shadow_min_mean_return_pct' 'proven_first'
mut app/core/selection.py 'c.shadow_n >= settings.shadow_min_trades
        and c.shadow_mean is not None
        and c.shadow_mean <= settings' 'c.shadow_n > settings.shadow_min_trades
        and c.shadow_mean is not None
        and c.shadow_mean <= settings' 'proven_first'
mut app/core/selection.py 'if silver >= max_silver:' 'if silver > max_silver:' 'silver_cap'
mut app/trading/copier.py 'weight = min(weight, settings.silver_max_weight)' 'pass' 'silver_is_copied'
mut app/trading/copier.py 'if not w["tracked"]:' 'if False:' 'watched_wallet_gets'
mut app/trading/shadow.py 'return -100.0 / settings.leverage' 'return -100.0' 'net_return'

# The split (2026-11-14, D-19): the bot names itself in Telegram, records into its own folders, maps Jupiter's coins.
mut app/notifications/messages.py 'return f"[{mode()}] {esc(settings.bot_name)}"' 'return f"[{mode()}]"' 'names_the_bot'

# 2026-11-16: the go-live reminder (B-3) and the evidence report
mut app/config.py '        if not self.kucoin_key_dedicated:' '        if False:' 'shared_kucoin_key'
mut app/core/golive.py '            settings.kucoin_key_dedicated,' '            True,' 'reminds_about_the_shared_key'
mut app/tools/evidence_report.py 'f"file:{db}?mode=ro"' 'f"file:{db}?mode=rw"' 'read_only'
mut app/tools/evidence_report.py 'SUM(net_pnl > 0) AS won' 'SUM(net_pnl >= -100) AS won' 'summary_adds_up'
mut app/main.py 'add_rows("solana-bot/grades"' 'add_rows("grades"' 'solana_bot_folders'
mut app/data/market.py 'coins = sorted(JUPITER_COINS)' 'coins = sorted(JUPITER_COINS)[1:]' 'jupiter_coins_map'

rm -f "$backup"
echo "mutants survived: $survived"
exit "$survived"
