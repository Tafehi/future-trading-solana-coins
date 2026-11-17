"""Profile -> Verdict (PLAN §5). The first rule that matches wins; every verdict carries the numbers that decided it, so
the user can check it by hand, as in the case study. Every threshold is in `settings.py`.

Rule 4 (CASH-OUT) measures how long the wallet took from "money in" to "money out" (`in_to_exit_days`), not its whole
ledger life: a real cash-out wallet (case `0x921e…`) got an airdrop and made one small withdraw months later, which
made its ledger life 389 days although the cash-out itself took one hour.
"""

from datetime import UTC, datetime

from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.model import Profile, Verdict

HOLDER = "HOLDER"
PASS_THROUGH = "PASS-THROUGH"
CASH_OUT = "CASH-OUT"
ONE_SHOT = "ONE-SHOT BUYER"
LIKELY_BOT = "LIKELY BOT"
PERP_TRADER = "PERP TRADER"
SLOW_SELLER = "SLOW SELLER"
SPOT_TRADER = "SPOT TRADER"
INCOMPLETE = "INCOMPLETE"


def _d(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts else "-"


def activity(p: Profile) -> str:
    perp = f"{p.perp_fills} perp fills (${p.perp_volume:,.0f})"
    futures = f"{p.funding_events} funding" if p.chain == "hl" else f"{p.jup_episodes} Jupiter episodes"
    return f"{p.fills} fills, {p.orders} orders on {p.active_days} days; {perp}; {futures}"


def is_bot(p: Profile) -> bool:
    busy = (
        p.orders >= S.BOT_MIN_ORDERS
        and p.active_days >= S.BOT_MIN_ACTIVE_DAYS
        and p.fills_per_day >= S.BOT_MIN_FILLS_PER_DAY
    )
    many_swaps = p.swaps >= S.BOT_SOL_MIN_SWAPS and p.swap_tokens >= S.BOT_SOL_MIN_TOKENS
    return busy and (p.perp_share >= S.BOT_MIN_PERP_SHARE or many_swaps)


def is_cash_out(p: Profile) -> bool:
    fast = p.in_to_exit_days is not None and p.in_to_exit_days <= S.CASH_OUT_MAX_LIFETIME_DAYS
    short_life = p.lifetime_days <= S.CASH_OUT_MAX_LIFETIME_DAYS
    seller = p.spot_volume > 0 and p.sell_share >= S.CASH_OUT_SELL_SHARE
    withdrew = p.spot_sell_volume > 0 and p.withdraws_usd >= S.CASH_OUT_WITHDRAW_SHARE * p.spot_sell_volume
    return (fast or short_life or seller) and withdrew


def is_perp_trader(p: Profile) -> bool:
    if p.perp_share < S.PERP_MIN_SHARE:
        return False
    return p.funding_events > 0 if p.chain == "hl" else p.jup_episodes >= S.PERP_MIN_JUP_EPISODES


def is_slow_seller(p: Profile) -> bool:
    return (
        p.spot_volume > 0
        and p.sell_share >= S.SLOW_SELL_SHARE
        and p.round_lot_share >= S.SLOW_SELL_ROUND_SHARE
        and p.sell_days >= S.SLOW_SELL_MIN_DAYS
    )


def _rules(p: Profile) -> Verdict:
    if p.fills == 0:
        why = ["0 fills ever"]
        if p.staked_now:
            why.append(f"{p.staked_now:,.2f} HYPE staked now")
        if p.stake_lag_minutes is not None:
            why.append(f"staked {p.stake_lag_minutes:,.0f} min after receiving")
        if p.airdrops:
            why.append(f"{len(p.airdrops)} airdrops")
        return Verdict(HOLDER, why)
    if p.passthrough_share >= S.PASSTHROUGH_SHARE and p.fills < S.PASSTHROUGH_MAX_FILLS:
        lag = f" within {p.passthrough_lag_minutes:,.0f} min" if p.passthrough_lag_minutes is not None else ""
        return Verdict(
            PASS_THROUGH, [f"{p.passthrough_share:.0%} of a big incoming amount left again{lag}", f"{p.fills} fills"]
        )
    if is_cash_out(p):
        span = (
            f"in to out in {p.in_to_exit_days:.1f} days"
            if p.in_to_exit_days is not None
            else f"lives {p.lifetime_days:.1f} days"
        )
        return Verdict(CASH_OUT, [span, f"sells {p.sell_share:.0%} of spot volume",
                                  f"withdrew ${p.withdraws_usd:,.0f} of ${p.spot_sell_volume:,.0f} sold"])  # fmt: skip
    if p.one_shot:
        return Verdict(ONE_SHOT, ["money in -> only buys -> one transfer out of nearly all of it, inside one day",
                                  f"{p.fills} fills on {p.active_days} day(s)"])  # fmt: skip
    if is_bot(p):
        return Verdict(LIKELY_BOT, [activity(p), f"{p.fills_per_day:,.0f} fills per active day",
                                    f"perp share {p.perp_share:.0%}",
                                    f"{p.swaps} swaps over {p.swap_tokens} tokens"])  # fmt: skip
    if is_perp_trader(p):
        return Verdict(PERP_TRADER, [activity(p), f"perp share {p.perp_share:.0%}"])
    if is_slow_seller(p):
        return Verdict(SLOW_SELLER, [f"sells {p.sell_share:.0%} of spot volume",
                                     f"{p.round_lot_share:.0%} of sell orders in round lots",
                                     f"sells on {p.sell_days} days"])  # fmt: skip
    why = [activity(p), f"perp share {p.perp_share:.0%}", f"buys {p.buy_share:.0%} of spot volume",
           f"active {_d(p.first)} .. {_d(p.last)}"]  # fmt: skip
    if p.exit_door_pairs:
        why.append(
            f"{p.exit_door_pairs} perp moves were exits (withdraw within {S.EXIT_DOOR_MINUTES:.0f} min), not trading"
        )
    return Verdict(SPOT_TRADER, why)


def classify(p: Profile) -> Verdict:
    v = _rules(p)
    if not p.complete:
        gaps = ", ".join(f"{t} {g:+,.4f}" for t, g in sorted(p.balance_gaps.items())) or "fills cut by the API"
        return Verdict(
            INCOMPLETE,
            [f"history does not add up to the live balance ({gaps})", f"best guess: {v.line()}"],
            unsure=True,
        )
    if p.partial:
        return Verdict(
            v.label, v.reasons + ["only the newest transactions were read: counts are a lower bound"], unsure=True
        )
    return v
