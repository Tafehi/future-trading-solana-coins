"""Wallet tier, trading style and standing. Pure; every threshold comes from `settings`.

Style (how the wallet trades):
- SCALPER: median episode under SCALPER_MAX_MEDIAN_HOLD_MIN. Our copy lands 10-30 s late and pays two
  taker fees, so a scalper's edge does not survive copying. BLACK. (The fill count is NOT used: big
  traders split one order into thousands of fills; see DEBUG.md.)
- MARKET_MAKER: month volume / account >= MM_MIN_TURNOVER with |month ROI| <= MM_MAX_ABS_ROI. BLACK.
- POSITION: median hold >= POSITION_MIN_MEDIAN_HOLD_HOURS.
- SWING: everything in between. The main copy target.

Tier (how good it is), on the graded window:
- BLACK: a copy-hostile style, a losing window, a losing month, or most volume in coins KuCoin lacks.
- GOLD / SILVER: enough episodes, profit factor and drawdown inside the thresholds.
- BRONZE: profitable but short of SILVER. WHITE: too little evidence.
Unknown evidence (None) never passes a threshold.

Ported from ai-kucoin-future `app/core/wallet_grader.py`. Removed here: SILVER watch/copy switches, standing,
earned tiers and the quality weight (this bot copies GOLD only, one account).
"""

from dataclasses import dataclass

from app.config import settings
from app.core.wallet_stats import Episode, LongTerm, WalletStats
from app.data.hyperliquid import AccountState

SCALPER = "SCALPER"
MARKET_MAKER = "MARKET_MAKER"
SWING = "SWING"
POSITION = "POSITION"
UNKNOWN = "UNKNOWN"

GOLD = "GOLD"
SILVER = "SILVER"
BRONZE = "BRONZE"
WHITE = "WHITE"
BLACK = "BLACK"
TIER_ORDER = (BLACK, WHITE, BRONZE, SILVER, GOLD)


@dataclass(frozen=True)
class Grade:
    style: str
    tier: str
    reason: str


def style_of(st: WalletStats, *, month_volume: float, account_value: float, month_roi: float) -> str:
    if account_value > 0 and month_volume / account_value >= settings.mm_min_turnover:
        if abs(month_roi) <= settings.mm_max_abs_roi:
            return MARKET_MAKER
    if st.median_hold_s is None:
        return UNKNOWN
    if st.median_hold_s < settings.scalper_max_median_hold_min * 60:
        return SCALPER
    if st.median_hold_s >= settings.position_min_median_hold_hours * 3600:
        return POSITION
    return SWING


def grade(
    st: WalletStats,
    *,
    drawdown: float | None,
    month_pnl: float,
    month_volume: float,
    account_value: float,
    month_roi: float,
    long: LongTerm | None = None,
    account: AccountState | None = None,
    now_ms: int | None = None,
) -> Grade:
    """`account` (clearinghouseState now) and `now_ms` feed the hard rejects; either unknown skips its check."""
    style = style_of(st, month_volume=month_volume, account_value=account_value, month_roi=month_roi)
    if style == MARKET_MAKER:
        return Grade(style, BLACK, "market maker: it earns the spread a copier pays")
    if style == SCALPER and not settings.copy_fast_traders:
        return Grade(style, BLACK, "scalper style cannot be copied with a delay (COPY_FAST_TRADERS is off)")
    if month_pnl <= 0:
        return Grade(style, BLACK, "losing month on the leaderboard")
    if account is not None and account.account_value > 0:
        if account.unrealized_pnl < -settings.max_unrealized_loss_share * account.account_value:
            share = -account.unrealized_pnl / account.account_value
            return Grade(style, BLACK, f"open positions down {share:.0%} of the account (hidden losses)")
        if account.leverage > settings.max_wallet_leverage:
            return Grade(style, BLACK, f"account runs {account.leverage:.1f}x leverage")
    if now_ms is not None and st.last_fill_ms is not None:
        idle_days = (now_ms - st.last_fill_ms) / 86_400_000
        if idle_days > settings.max_inactive_days:
            return Grade(style, WHITE, f"inactive: last trade {idle_days:.1f} days ago")
    if st.coverage is not None and st.coverage < settings.min_kucoin_coverage:
        return Grade(style, BLACK, f"only {st.coverage:.0%} of volume in coins KuCoin lists")
    if st.trades and st.total_pnl <= 0:
        return Grade(style, BLACK, f"lost money over {st.trades} closed trades")
    pf, dd = st.profit_factor, drawdown
    if pf is not None and pf < 1.0:
        return Grade(style, BLACK, f"profit factor {pf:.2f} below 1")
    if st.trades < settings.silver_min_trades or pf is None or dd is None or st.coverage is None:
        return Grade(style, WHITE, f"not enough evidence ({st.trades} closed trades)")
    figures = f"{st.trades} trades, PF {pf:.2f}, max DD {dd:.0%}"
    if st.biggest_win_share is not None and st.biggest_win_share > settings.max_biggest_win_share:
        return Grade(style, BRONZE, f"{figures}: one trade made {st.biggest_win_share:.0%} of the profit")
    if long is not None:
        figures += f", {long.days:.0f}d record {long.pnl:+,.0f} USD, {long.months_up}/{long.months} months up"
        if long.pnl <= 0:
            return Grade(style, BRONZE, f"{figures}: lost money over its long record")
    if not gold_blockers(
        trades=st.trades,
        profit_factor=pf,
        drawdown=dd,
        history_days=long.days if long else None,
        months=long.months if long else None,
        months_up=long.months_up if long else None,
    ):
        return Grade(style, GOLD, figures)
    if pf >= settings.silver_min_profit_factor and dd <= settings.silver_max_drawdown:
        return Grade(style, SILVER, figures)
    return Grade(style, BRONZE, figures)


def gold_blockers(
    *,
    trades: int | None,
    profit_factor: float | None,
    drawdown: float | None,
    history_days: float | None,
    months: int | None,
    months_up: int | None,
) -> list[str]:
    """The GOLD rules a SILVER-quality wallet fails; empty = GOLD. The grader uses it, and the status report counts
    which rule holds SILVER wallets back, so the thresholds are tuned on data (user, 2026-10-20). Unknown fails."""
    out = []
    if trades is None or trades < settings.gold_min_trades:
        out.append("trades")
    if profit_factor is None or profit_factor < settings.gold_min_profit_factor:
        out.append("profit factor")
    if drawdown is None or drawdown > settings.gold_max_drawdown:
        out.append("drawdown")
    if history_days is None or history_days < settings.gold_min_history_days:
        out.append("history")
    if not months or months_up is None or months_up / months < settings.gold_min_months_up_share:
        out.append("months up")
    return out


def score(st: WalletStats, drawdown: float | None, months_up_share: float | None = None) -> float:
    """Ranks copyable wallets for the tracked list. Higher is better; unknowns rank last. A steady long
    record (most months up) lifts a wallet by up to 2x over one with no whole month on record."""
    if st.profit_factor is None or drawdown is None:
        return 0.0
    steady = 1.0 + (months_up_share if months_up_share is not None else 0.0)
    return min(st.profit_factor, 10.0) * (1.0 - min(drawdown, 0.99)) * min(st.trades, 100) ** 0.5 * steady


# Wallet score (precedence). Every constant is a stated choice, tested at its edges (LESSON E-14).
PRIOR_TRADES = 5.0  # every wallet starts as if it had 5 average trades ...
PRIOR_LOSS_RATE = 0.20  # ... of which 20% lost: few trades cannot look perfect
BEST_LOSS_RATE = 0.05  # adjusted loss rate at or below this = full reliability
WORST_LOSS_RATE = 0.50  # at or above this = none
RECENT_WEIGHT = 2.0  # trades closed in the last RECENT_DAYS count this many times
RECENT_DAYS = 7.0
BEST_PAYOFF = 1.0  # average loss / average win at or below this = full marks for loss size ...
WORST_PAYOFF = 3.0  # ... at or above this = none ("win small often, lose big once")
UNKNOWN_PAYOFF_PART = 0.75  # no real loss yet: loss size unproven, so not full marks
FULL_HISTORY_DAYS = 180.0  # a record this long counts fully
UNKNOWN_HISTORY_PART = 0.25
WEIGHTS = (0.45, 0.25, 0.30)  # reliability, loss size, history
SCORE_CAP_FEW_TRADES = 5.0  # fewer than SCORE_MIN_TRADES real trades: never above this


@dataclass(frozen=True)
class ScoreParts:
    score: float
    trades: int  # real trades in the window (scratches left out)
    losses: int
    loss_rate: float  # adjusted (shrunk, recent counted double)
    payoff: float | None  # average loss / average win; None without a real loss or win
    reliability: float
    loss_size: float
    history: float


def _line(value: float, best: float, worst: float) -> float:
    """1 at `best` or better, 0 at `worst` or beyond, a straight line between."""
    return max(0.0, min(1.0, (worst - value) / (worst - best)))


def wallet_score(eps: list[Episode], *, now_ms: int, equity: float, long: LongTerm | None) -> ScoreParts:
    """Score 0-10 that a lucky wallet cannot reach: a shrunk, recency-weighted loss rate (45%), the size of losses
    against wins (25%) and the long record (30%); fewer than SCORE_MIN_TRADES real trades caps it at 5."""
    since = now_ms - int(settings.score_window_days * 86_400_000)
    recent = now_ms - int(RECENT_DAYS * 86_400_000)
    scratch = settings.scratch_loss_share * equity
    # Every win counts; only a LOSS smaller than the scratch line is a scratch (fees, a flat exit). Applying the
    # line to wins dropped a big account's normal trades (DEBUG D-21).
    real = [e for e in eps if e.closed_ms >= since and (e.pnl > 0 or -e.pnl > scratch)]
    wins = [e for e in real if e.pnl > 0]
    losses = [e for e in real if e.pnl < 0]

    def w(e: Episode) -> float:
        return RECENT_WEIGHT if e.closed_ms >= recent else 1.0

    n_w = sum(w(e) for e in real)
    l_w = sum(w(e) for e in losses)
    rate = (l_w + PRIOR_TRADES * PRIOR_LOSS_RATE) / (n_w + PRIOR_TRADES)
    reliability = _line(rate, BEST_LOSS_RATE, WORST_LOSS_RATE)

    payoff: float | None = None
    if wins and losses:
        avg_win = sum(e.pnl for e in wins) / len(wins)
        avg_loss = -sum(e.pnl for e in losses) / len(losses)
        payoff = avg_loss / avg_win
        loss_size = _line(payoff, BEST_PAYOFF, WORST_PAYOFF)
    elif losses:  # only losses
        loss_size = 0.0
    else:
        loss_size = UNKNOWN_PAYOFF_PART

    if long is None or long.months == 0:
        history = UNKNOWN_HISTORY_PART
    else:
        steady = (long.months_up + 1) / (long.months + 2)  # shrunk share of months up
        history = steady * (0.5 + 0.5 * min(1.0, long.days / FULL_HISTORY_DAYS))

    a, b, c = WEIGHTS
    score = 10.0 * (a * reliability + b * loss_size + c * history)
    if len(real) < settings.score_min_trades:
        score = min(score, SCORE_CAP_FEW_TRADES)
    return ScoreParts(round(score, 2), len(real), len(losses), rate, payoff, reliability, loss_size, history)


MIN_COPY_WEIGHT = 1
MAX_COPY_WEIGHT = 10


def losses_per_month(losses: int, covered_days: float) -> float | None:
    """Real losses scaled to 30 days. None when no time is covered (nothing to judge)."""
    if covered_days <= 0:
        return None
    return losses * 30.0 / covered_days


def copy_weight(per_month: float | None) -> int:
    """Weight 1-10 (user, 2026-10-19): 0 real losses a month = 10, WEIGHT_WORST_LOSSES_PER_MONTH or more = 1,
    a straight line between, rounded half up. Unknown = 1: no proof, smallest size."""
    if per_month is None:
        return MIN_COPY_WEIGHT
    worst = settings.weight_worst_losses_per_month
    raw = MAX_COPY_WEIGHT - (MAX_COPY_WEIGHT - MIN_COPY_WEIGHT) * min(max(per_month, 0.0), worst) / worst
    # Round half up in a way floats cannot tip: 8.5 must be 9 even when it is stored as 8.4999999.
    return max(MIN_COPY_WEIGHT, min(MAX_COPY_WEIGHT, int(round(raw, 9) + 0.5)))


MIN_SIZE_SHARE = 0.1  # the bottom of the curve (weight 1 on it); weights 1 and 2 are fixed below
# Weight 1 copies a full trade divided by 9, weight 2 divided by 8 (user, 2026-10-22; were 1/10 and ~1/7.7).
FIXED_SIZE_SHARE = {1: 1.0 / 9.0, 2: 1.0 / 8.0}


def size_share(weight: int | float | None) -> float:
    """The share of a full copy for a wallet of this weight. Weight 10 = 1.0; weight 1 = 1/9 and weight 2 = 1/8
    (FIXED_SIZE_SHARE); weights 3-9 on a geometric curve from 0.1 at weight 1, each step x1.29:
    11% 12.5% 17% 22% 28% 36% 46% 60% 77% 100%. A straight line (weight / 10) gave weight 5 half a trade; the user
    wants a low weight to trade far smaller than a weight-10 one, so the middle weights are pulled down. A missing
    weight copies at the smallest size."""
    w = MIN_COPY_WEIGHT if weight is None else max(MIN_COPY_WEIGHT, min(MAX_COPY_WEIGHT, float(weight)))
    if w in FIXED_SIZE_SHARE:
        return FIXED_SIZE_SHARE[int(w)]
    step = (w - MIN_COPY_WEIGHT) / (MAX_COPY_WEIGHT - MIN_COPY_WEIGHT)
    return float(MIN_SIZE_SHARE ** (1.0 - step))


def live_check(account: AccountState | None, *, last_fill_ms: int | None, now_ms: int) -> Grade | None:
    """The hourly check of a TRACKED wallet (the watcher's sweep), so a wallet that died or turned risky stops
    being copied within the hour instead of at the next daily round. None = still copyable. Same limits as the
    grader's hard rejects; an unknown value skips its check."""
    if account is not None:
        if account.account_value < settings.min_account_value_usd:
            return Grade(UNKNOWN, BLACK, f"account fell to ${account.account_value:,.0f} (emptied or blown up)")
        if account.unrealized_pnl < -settings.max_unrealized_loss_share * account.account_value:
            share = -account.unrealized_pnl / account.account_value
            return Grade(UNKNOWN, BLACK, f"open positions down {share:.0%} of the account (hidden losses)")
        if account.leverage > settings.max_wallet_leverage:
            return Grade(UNKNOWN, BLACK, f"account runs {account.leverage:.1f}x leverage")
    if last_fill_ms is not None:
        idle_days = (now_ms - last_fill_ms) / 86_400_000
        if idle_days > settings.max_inactive_days:
            return Grade(UNKNOWN, WHITE, f"inactive: last trade {idle_days:.1f} days ago")
    return None
