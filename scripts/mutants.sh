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

# Jupiter Perps decoders (DEBUG D-2, D-5): the tracer reads Solana futures with them
mut app/data/jupiter_perps.py 'ACCOUNT_SIZE = 216' 'ACCOUNT_SIZE = 210' 'real_accounts or change_feed'
mut app/data/jupiter_perps.py 'side = SIDES.get(data[144])' 'side = SIDES.get(data[143])' 'decode_real_accounts'
mut app/data/jupiter_events.py 'digest()[:8][::-1]' 'digest()[:8]' 'event_tag or real_close'
mut app/data/jupiter_events.py 'final = next((e for e in reversed(evs) if e.closes), None)' 'final = evs[-1]' 'episodes_need_a_full_close'
# The Chainstack key in a --rpc-url is masked in logs
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
rm -f "$backup"
echo "mutants survived: $survived"
exit "$survived"
