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
        newest' 'pass
        newest' 'sends_one_window_once'
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
mut app/scout.py 'claim("jupiter_grade", interval_s=settings.jupiter_grade_minutes * 60)' 'claim("jupiter_grade", interval_s=3600)' 'wallet_list_intervals'

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
mut app/trading/copier.py 'if not w.tracked:' 'if False:' 'watched_wallet_gets'
mut app/trading/shadow.py 'return -100.0 / settings.leverage' 'return -100.0' 'net_return'

# The split (2026-11-14, D-19): the bot names itself in Telegram, records into its own folders, maps Jupiter's coins.
mut app/notifications/messages.py 'return f"[{mode()}] {esc(settings.bot_name)}"' 'return f"[{mode()}]"' 'names_the_bot'

# 2026-11-16: the go-live reminder (B-3) and the evidence report
mut app/config.py '        if not self.kucoin_key_dedicated:' '        if False:' 'shared_kucoin_key'
mut app/core/golive.py '            settings.kucoin_key_dedicated,' '            True,' 'reminds_about_the_shared_key'
mut app/tools/evidence_report.py 'f"file:{db}?mode=ro"' 'f"file:{db}?mode=rw"' 'read_only'
mut app/tools/evidence_report.py 'SUM(net_pnl > 0) AS won' 'SUM(net_pnl >= -100) AS won' 'summary_adds_up'
mut app/scout.py 'add_rows("solana-bot/grades"' 'add_rows("grades"' 'solana_bot_folders'
mut app/data/market.py 'coins = sorted(JUPITER_COINS)' 'coins = sorted(JUPITER_COINS)[1:]' 'jupiter_coins_map'

# 2026-11-16: Jupiter wallet history exported once each
mut app/record/episodes_export.py 'WHERE id > ? ORDER BY id' 'WHERE id >= ? ORDER BY id' 'exported_once'
mut app/record/episodes_export.py '            await meta.put(conn, KEY, max(int(r["id"]) for r in rows))' '            pass' 'exported_once'

# 2026-11-16: the Telegram report every 45 min, always
mut app/notifications/messages.py 'if not opened and not closed and not always:' 'if not opened and not closed:' 'reports_when_always'
mut app/notifications/digest.py 'always=settings.digest_always and newest' 'always=settings.digest_always' 'gap_sends_one'
mut app/config.py '        if 1440 % self.digest_minutes:' '        if False:' 'divide_a_day'
# 2026-11-18 (D-29): more wallets pass. Every owner's past from Jupiter's trade API; a stronger proof below 15 trades.
JA=app/data/jupiter_api.py
mut $JA 'if size <= max(FLAT_USD, FLAT_SHARE * peak) or' 'if size < max(FLAT_USD, FLAT_SHARE * peak) or' 'flat_at_exactly'
mut $JA ' or t.get("orderType") == "Liquidation":' ':' 'liquidation_closes_whatever'
mut $JA '                if when >= since:' '                if when > since:' 'window_starts_at_exactly'
mut $JA 'if t.get("owner") == owner and coin_of(t) in COINS' 'if coin_of(t) in COINS' 'other_owners'
mut $JA '                if key not in seen:' '                if True:' 'border_repeats'
mut $JA 'if len(rows) < PAGE or page * PAGE + len(rows) >= count:' 'if len(rows) < PAGE:' 'exactly_one_full_page'
mut $JA '        return History(trades, False)' '        return History(trades, True)' 'marked_cut'
mut $JA 'entry = entry_value / added if added else px' 'entry = px' 'open_add_partial'
mut $JA '                fees += fee
                entry_value += sz * px' '                entry_value += sz * px' 'open_add_partial or real_owner_lifetimes'
mut app/data/jupiter_events.py '        fee += f["liquidation_fee_usd"]' '        pass' 'liquidation_fee_is_counted'
JU=app/jobs/jupiter.py
mut $JU 'if st.trades >= full or st.trades <= 0:' 'if st.trades > full or st.trades <= 0:' 'no_loss_yet'
mut $JU '        pf = float(st.trades)' '        pass' 'no_loss_yet or lucky_short_streak'
mut $JU '    return pf, math.sqrt(full / st.trades)' '    return pf, 1.0' 'below_15 or scaled_threshold'
mut $JU 'settings.gold_min_profit_factor * scale, settings.silver_min_profit_factor * scale' 'settings.gold_min_profit_factor, settings.silver_min_profit_factor' 'scaled_threshold'
mut $JU '        if st.trades < settings.jupiter_full_proof_episodes:  # a short record' '        if st.trades <= settings.jupiter_full_proof_episodes:  # a short record' 'short_record_copies_small'
mut $JU 'OR graded_at < ?)' 'OR graded_at <= ?)' 'redone_after_exactly'
mut app/config.py 'if self.jupiter_min_episodes > self.jupiter_full_proof_episodes:' 'if self.jupiter_min_episodes >= self.jupiter_full_proof_episodes:' 'above_full_proof_refuses'
mut app/config.py 'default=1_000.0, ge=0.0, alias="JUPITER_HISTORY_MIN_SIZE_USD"' 'default=10_000.0, ge=0.0, alias="JUPITER_HISTORY_MIN_SIZE_USD"' 'every_recorded_owner_is_queued'
mut app/config.py 'jupiter_min_episodes: int = Field(default=8,' 'jupiter_min_episodes: int = Field(default=15,' 'minimum_episodes_at_exactly_eight'
JH=app/jobs/jupiter_history.py
mut $JH 'if api is not None and settings.jupiter_history_source == "api":' 'if False:' 'replaces_feed_and_chain'
mut $JH 'replaced = ("feed", "api", "chain") if origin' 'replaced = ("feed",) if origin' 'replaces_feed_and_chain'
mut $JH 'history_from = since if hist.complete else max(since, oldest)' 'history_from = max(since, oldest)' 'replaces_feed_and_chain'
mut $JH 'next_owners(settings.jupiter_history_owners_per_pass)' 'next_owners(20)' 'owners_per_pass'
mut app/db/migrations.py 'UPDATE jup_owners SET backfilled_at = NULL WHERE backfilled_at IS NOT NULL;' '' 'migration_3'
# 2026-11-18: /health watches exactly the loops each program runs (DEBUG D-11; per program since D-30)
mut app/api/health.py '"grade": 600, "cold": 600}' '"grade": 600, "cold": 600, "poll": 300}' 'exactly_the_loops'
mut app/api/health.py '"feedpoll": 300}' '"feedpoll": 300, "discovery": 300}' 'exactly_the_loops'
# 2026-11-19 (D-30): scout + trader, the websocket feed, the copy list. Boundaries AT the value.
W=app/trading/watch.py
mut $W 'fresh = now - p.open_time <= settings.signal_max_age_s' 'fresh = now - p.open_time < settings.signal_max_age_s' 'freshness_at_its_boundary'
mut $W '        if prev.same_as(p):' '        if False:' 'duplicate_on_any_slot'
mut $W '        if (p.update_time, obs.slot) < prev.order:' '        if False:' 'never_moves_back'
mut $W 'was_open = prev is not None and prev.size_usd >= minimum' 'was_open = prev is not None and prev.size_usd > minimum' 'exactly_the_minimum_is_not_a_second_open'
mut $W 'if p.is_open and p.size_usd >= minimum and not was_open and fresh:' 'if p.is_open and p.size_usd > minimum and not was_open and fresh:' 'size_at_the_minimum'
mut $W '(not p.is_open or p.open_time != prev.open_time)' '(not p.is_open)' 'closed_and_opened_again'
mut $W '                if entry is None:' '                if False:' 'off_the_list'
mut $W '        self.states[obs.pubkey] = new' '        pass' 'websocket_then_poll'
mut app/db/migrations.py 'CREATE UNIQUE INDEX IF NOT EXISTS signals_once' 'CREATE INDEX IF NOT EXISTS signals_once' 'restart_never_signals_twice'
CL=app/trading/copylist.py
mut $CL 'return age is None or age > settings.copylist_max_age_s' 'return age is None or age >= settings.copylist_max_age_s' 'list_age_at_its_limit'
mut $CL 'return age is None or age > settings.copylist_max_age_s' 'return False or age > settings.copylist_max_age_s' 'list_age_at_its_limit'
mut $CL 'key=lambda e: (not e.tracked, -e.rank_score, e.address)' 'key=lambda e: (-e.rank_score, e.address)' 'ranks_tracked_first'
mut app/trading/copier.py '        if self.copylist.stale(at):' '        if False:' 'refuses_by_the_list'
mut app/trading/copier.py 'if w is None or w.tier not in tiers:' 'if w is None:' 'refuses_by_the_list'
mut app/db/crossread.py '    async with store.session(scout_db, read_only=True) as conn:
        await conn.execute("BEGIN")' '    async with store.session(scout_db) as conn:
        await conn.execute("BEGIN")' 'never_created'
mut app/db/store.py '?mode=ro", uri=True)' '?mode=rwc", uri=True)' 'refuses_to_write or never_created'
mut $JU 'read_only=shadow_db is not None, now=at)' 'read_only=False, now=at)' 'keeps_the_old_pick'
mut $JU '        await meta.put(conn, PICKED_AT_KEY, at, now=at)' '        pass' 'writes_its_time'
mut $JU '        raise ShadowUnreadable(f"shadow record unreadable ({type(exc).__name__}); the pick is kept") from exc' '        shadows = {}' 'keeps_the_old_pick'
mut app/scout.py '            await jobclock.schedule("jupiter_grade", jupiter.SHADOW_RETRY_S)' '            pass' 'retries_an_unreadable'
mut app/scout.py 'from app.api import health
' 'from app.api import health
from app.trading import ledger  # noqa: F401
' 'scout_loads_no_trading'
mut app/trader.py '                    self.wake.set()' '                    pass' 'copies_an_open_its_poll_sees'
WS=app/data/solana_ws.py
mut $WS '                self._reset(None)' '                self._sock = None' 'subscribes_every_owner_again'
mut $WS 'if opened_at is not None and self._clock() - opened_at >= STABLE_S:' 'if opened_at is not None and self._clock() - opened_at > STABLE_S:' 'stable_minute'
mut $WS '            backoff = min(MAX_BACKOFF_S, backoff * 2)' '            backoff = backoff * 2' 'doubles_to_sixty'
mut $WS '                await asyncio.wait_for(waiter, self.pong_timeout_s)' '                await asyncio.sleep(0)' 'missing_pong'
mut $WS '                    logger.warning("ws: %s", why(exc))' '                    logger.warning("ws: %s", exc)' 'url_never_reaches'
mut $WS '            await self._unsubscribe(result)  # dropped' '            pass  # dropped' 'dropped_while_its_subscribe'
mut $WS '        if pos is None or pos.owner != owner:' '        if pos is None:' 'discriminator_or_owner'
mut app/data/jupiter_perps.py 'if len(data) != ACCOUNT_SIZE or data[:8] != discriminator():' 'if len(data) != ACCOUNT_SIZE:' 'discriminator_or_owner'
HE=app/api/health.py
mut $HE '    if list_age is None or list_age > settings.copylist_max_age_s:' '    if list_age is not None and list_age > settings.copylist_max_age_s:' 'copy_list_age_at_its_limit'
mut $HE 'and down is not None and down > FEED_DOWN_LIMIT_S:' 'and down is not None and down >= FEED_DOWN_LIMIT_S:' 'websocket_down_at_its_limit'
mut $HE '(pick_age is None or pick_age > settings.copylist_max_age_s)' '(pick_age is None or pick_age >= settings.copylist_max_age_s)' 'pick_age_at_the_copy_list_limit'
mut $HE '    if not worker_alive:' '    if False:' 'dead_feed_worker'
mut app/config.py 'if os.path.abspath(self.scout_db_path) == os.path.abspath(self.trader_db_path):' 'if self.scout_db_path == self.trader_db_path:' 'one_file_for_both'
mut app/config.py 'if self.copylist_max_age_s < 2 * self.jupiter_grade_minutes * 60:' 'if self.copylist_max_age_s <= 2 * self.jupiter_grade_minutes * 60:' 'cover_two_picks'
mut app/runtime.py '        waits.append(asyncio.ensure_future(wake.wait()))' '        pass' 'wake_starts_the_next_pass'
# The scout's one-time owner seed (a fresh scout.db must not forget the single bot's 1,331 owners)
mut app/scout.py '        await jupiter.seed_owners(settings.trader_db_path)' '        pass' 'seeds_at_boot'
mut app/db/crossread.py '    async with store.session(trader_db, read_only=True) as conn:
        async with conn.execute("SELECT owner, first_seen' '    async with store.session(trader_db) as conn:
        async with conn.execute("SELECT owner, first_seen' 'seeds_nothing'
mut $JU 'SET first_seen = MIN(jup_owners.first_seen, excluded.first_seen), ' 'SET first_seen = excluded.first_seen, ' 'seeds_the_owner_list_once'
mut $JU 'excluded.first_seen), "
            "max_open_usd = MAX(jup_owners.max_open_usd, excluded.max_open_usd)",' 'excluded.first_seen), "
            "max_open_usd = excluded.max_open_usd",' 'seeds_the_owner_list_once'
# 2026-11-17: the Chainstack key in SOLANA_RPC_URL is masked in logs
mut app/utils/logging.py '(re.compile(r"(\.chainstack\.com/)[A-Za-z0-9_-]+"), r"\1***"),' '' 'chainstack_key'

# 2026-11-17: the wallet tracer (PLAN.md). One mutant per classify rule, each AT its boundary, and per timing/data rule.
C=app/tools/wallet_trace/classify.py
mut $C '    if p.fills == 0:' '    if p.fills <= 1:' 'holder_is_zero'
mut $C 'p.passthrough_share >= S.PASSTHROUGH_SHARE and' 'p.passthrough_share > S.PASSTHROUGH_SHARE and' 'pass_through_share'
mut $C 'p.fills < S.PASSTHROUGH_MAX_FILLS' 'p.fills <= S.PASSTHROUGH_MAX_FILLS' 'pass_through_fills'
mut $C 'p.in_to_exit_days <= S.CASH_OUT_MAX_LIFETIME_DAYS' 'p.in_to_exit_days < S.CASH_OUT_MAX_LIFETIME_DAYS' 'cash_out_fast_exit'
mut $C 'p.lifetime_days <= S.CASH_OUT_MAX_LIFETIME_DAYS' 'p.lifetime_days < S.CASH_OUT_MAX_LIFETIME_DAYS' 'cash_out_short_life'
mut $C 'p.sell_share >= S.CASH_OUT_SELL_SHARE' 'p.sell_share > S.CASH_OUT_SELL_SHARE' 'cash_out_sell_share'
mut $C 'p.withdraws_usd >= S.CASH_OUT_WITHDRAW_SHARE' 'p.withdraws_usd > S.CASH_OUT_WITHDRAW_SHARE' 'cash_out_withdraw_share'
mut $C 'p.orders >= S.BOT_MIN_ORDERS' 'p.orders > S.BOT_MIN_ORDERS' 'bot_at_exactly_300'
mut $C 'p.active_days >= S.BOT_MIN_ACTIVE_DAYS' 'p.active_days > S.BOT_MIN_ACTIVE_DAYS' 'bot_at_exactly_300'
mut $C 'p.fills_per_day >= S.BOT_MIN_FILLS_PER_DAY' 'p.fills_per_day > S.BOT_MIN_FILLS_PER_DAY' 'bot_fills_per_day'
mut $C 'p.perp_share >= S.BOT_MIN_PERP_SHARE' 'p.perp_share > S.BOT_MIN_PERP_SHARE' 'bot_perp_share'
mut $C 'p.swaps >= S.BOT_SOL_MIN_SWAPS and p.swap_tokens >= S.BOT_SOL_MIN_TOKENS' 'p.swaps > S.BOT_SOL_MIN_SWAPS and p.swap_tokens >= S.BOT_SOL_MIN_TOKENS' 'swap_bot'
mut $C 'p.swap_tokens >= S.BOT_SOL_MIN_TOKENS' 'p.swap_tokens > S.BOT_SOL_MIN_TOKENS' 'swap_bot'
mut $C 'if p.perp_share < S.PERP_MIN_SHARE:' 'if p.perp_share <= S.PERP_MIN_SHARE:' 'perp_trader_share'
mut $C 'p.funding_events > 0 if' 'p.funding_events >= 0 if' 'perp_trader_needs_funding'
mut $C 'p.jup_episodes >= S.PERP_MIN_JUP_EPISODES' 'p.jup_episodes > S.PERP_MIN_JUP_EPISODES' 'jupiter_perp_trader'
mut $C 'p.sell_share >= S.SLOW_SELL_SHARE' 'p.sell_share > S.SLOW_SELL_SHARE' 'slow_seller'
mut $C 'p.round_lot_share >= S.SLOW_SELL_ROUND_SHARE' 'p.round_lot_share > S.SLOW_SELL_ROUND_SHARE' 'slow_seller'
mut $C 'p.sell_days >= S.SLOW_SELL_MIN_DAYS' 'p.sell_days > S.SLOW_SELL_MIN_DAYS' 'slow_seller'
mut $C '    if not p.complete:' '    if False:' 'incomplete_wins'
mut $C '    if p.partial:' '    if False:' 'partial_keeps'
PR=app/tools/wallet_trace/profile.py
mut $PR 'nxt_w - t <= door_ms' 'nxt_w - t < door_ms' 'exit_door_at_exactly'
mut $PR 'if nxt_f is not None and (nxt_w is None or nxt_f < nxt_w):' 'if False:' 'fill_before_the_withdraw'
mut $PR 'tin.time < x.time <= tin.time + window' 'tin.time < x.time < tin.time + window' 'passthrough_window'
mut $PR 'outs[0].amount < S.ONE_SHOT_OUT_SHARE * amount' 'outs[0].amount <= S.ONE_SHOT_OUT_SHARE * amount' 'one_shot_out_share'
mut $PR 'return outs[0].time - start <= S.ONE_SHOT_WINDOW_HOURS' 'return outs[0].time - start < S.ONE_SHOT_WINDOW_HOURS' 'one_shot_window'
mut $PR 'for e in raw.staking:' 'for e in []:' 'delegator_history or golden'
mut $PR 'if (ev[0] // 1000, ev[1], round(ev[2], 6)) not in have:' 'if True:' 'counted_once'
mut $PR '[x.get("fill") or {} for x in raw.twap_fills]' '[]' 'twap_slices'
mut $PR '            unknown[kind] += 1' '            pass' 'unknown_ledger_types'
mut $PR 'abs(gap) * price < S.BALANCE_USD_TOL' 'abs(gap) * price < 0' 'dust_gap'
mut $PR '        p.balance_gaps[token] = gap' '        pass' 'missing_movement or golden'
mut $PR '        if is_system(cp):' '        if False:' 'system_address_is_an_exit'
mut $PR 'return abs(steps - round(steps)) < 1e-6' 'return True' 'round_lot_sizes'
L=app/tools/wallet_trace/links.py
mut $L 'ts.score >= S.TWIN_MIN_SCORE and' 'ts.score > S.TWIN_MIN_SCORE and' 'exactly_the_minimum_score'
mut $L ' and any(c.kind != AIRDROP for c in ts.clues)' '' 'airdrops_alone_never_make_a_twin'
mut $L 'min(S.TWIN_AIRDROP_POINTS * len(shared), S.TWIN_AIRDROP_MAX_POINTS)' 'S.TWIN_AIRDROP_POINTS * len(shared)' 'airdrop_cap'
mut $L 'abs(y.time - x.time) <= S.TWIN_SMALL_SENDER_SECONDS' 'abs(y.time - x.time) < S.TWIN_SMALL_SENDER_SECONDS' 'airdrop_cap'
H=app/tools/wallet_trace/hl_source.py
mut $H 'if self.used >= self.max_calls:' 'if self.used > self.max_calls:' 'exactly_the_limit'
mut $H '                if key not in seen:' '                if True:' 'border_repeats'
mut $H 'start = last if last > start else start + 1' 'start = last' 'one_millisecond'
mut $H '    if a == HYPE_SYSTEM:
        return True' '    if False:
        return True' 'system_addresses'
mut app/tools/wallet_trace/cache.py '        if self.refresh:
            return None' '        if False:
            return None' 'zero_calls'
mut app/tools/wallet_trace/cache.py '        if not inside_audit(root):' '        if False:' 'outside_audit'
mut app/tools/wallet_trace/walk.py '                else "vault (protocol pool)" if cp in vaults' '                else "" if cp in vaults' 'labels_and_vaults'
mut app/tools/wallet_trace/sol_profile.py 'if raw.history_end and first_sol_in is not None:' 'if first_sol_in is not None:' 'transfers_and_the_funder'
mut app/tools/wallet_trace/sol_profile.py '                lam += float((tx.get("meta") or {}).get("fee") or 0)' '                pass' 'own_balances'
mut app/data/solana_rpc.py 'else type(cause).__name__ if cause else' 'else str(cause) if cause else' 'never_carries_the_url'
mut $PR '        if token == "USDC" and liquidated:' '        if False:' 'liquidation_gap'
mut $H '                if "429" not in str(exc) or attempt == S.HL_429_RETRIES:' '                if True:' 'lasting_429'
# 2026-11-17 (D-27): secrets from Secrets Manager and the per-run Telegram summary
SE=app/tools/wallet_trace/secrets.py
TG=app/tools/wallet_trace/telegram.py
mut $SE '    if use_aws and not all(vals.values()):' '    if use_aws:' 'env_alone_never_calls_aws'
mut $SE '                if not vals[k]:
                    vals[k] = _clean(data.get(k))' '                vals[k] = _clean(data.get(k))' 'env_wins'
mut $SE 'return "" if s.lower() in UNSET else s' 'return s' 'env_wins_and_unset'
mut $SE 'problem = f"could not read secret {name} ({type(exc).__name__}); public RPC, no Telegram"' 'problem = f"could not read secret {name} ({exc})"' 'leaks_nothing'
mut $TG 'return f"Telegram not reached ({type(exc).__name__})"' 'return f"Telegram not reached ({exc})"' 'never_show_the_token'
mut $TG 'links.short(s) for s in starts' 's for s in starts' 'summary_text'
mut $TG 'if len(text) <= MAX_TEXT else' 'if True else' 'length_limit'
mut app/tools/wallet_trace/__main__.py '    if sec.solana_rpc_url:' '    if False:' 'rpc_choice'
mut app/tools/wallet_trace/__main__.py '    if not a.no_telegram:' '    if True:' 'sends_one_summary'

rm -f "$backup"
echo "mutants survived: $survived"
exit "$survived"
