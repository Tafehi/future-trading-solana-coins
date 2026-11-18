"""Episodes, stats, drawdown, style, tier, standing. Thresholds tested at their exact values."""

from dataclasses import replace

import pytest

from app.config import settings
from app.core import wallet_grader as wg
from app.core import wallet_stats as ws
from app.data.hyperliquid import AccountState, Fill, parse_fills
from app.tests.conftest import fixture


def f(
    coin: str, side: str, sz: float, start: float, t: int, pnl: float = 0.0, fee: float = 0.0, px: float = 100.0
) -> Fill:
    return Fill(coin, px, sz, side, t, start, "", pnl, fee)


def test_episode_flat_to_flat_with_fees() -> None:
    eps = ws.episodes([f("ETH", "B", 1, 0, 1000, fee=0.1), f("ETH", "A", 1, 1, 61_000, pnl=5, fee=0.1)])
    assert len(eps) == 1 and eps[0].side == "long"
    assert eps[0].pnl == pytest.approx(4.8) and eps[0].hold_s == 60


def test_a_window_that_starts_mid_position_skips_that_episode() -> None:
    eps = ws.episodes([f("ETH", "A", 1, 2, 1000, pnl=3), f("ETH", "A", 1, 1, 2000, pnl=3)])
    assert eps == []


def test_a_flip_ends_one_episode_and_starts_the_next() -> None:
    eps = ws.episodes(
        [f("ETH", "B", 1, 0, 0), f("ETH", "A", 2, 1, 3_600_000, pnl=10), f("ETH", "B", 1, -1, 7_200_000, pnl=-2)]
    )
    assert [e.side for e in eps] == ["long", "short"]
    assert [e.pnl for e in eps] == [10, -2]


def test_recorded_fills_produce_episodes() -> None:
    fills = parse_fills(fixture("hl_fills.json"))
    st = ws.stats(ws.episodes(fills), fills, {"BTC", "ETH", "HYPE", "ENA", "SOL"})
    assert st.coverage is not None and 0 <= st.coverage <= 1


def test_profit_factor_and_no_loss_cap() -> None:
    eps = [ws.Episode("A", "long", 0, 1, 30, 1), ws.Episode("A", "long", 0, 1, -10, 1)]
    assert ws.stats(eps, [], set()).profit_factor == pytest.approx(3.0)
    assert ws.stats(eps[:1], [], set()).profit_factor == ws.PF_CAP
    assert ws.stats([], [], set()).profit_factor is None


def test_drawdown_measures_pnl_not_deposits() -> None:
    av = [(0, 1000.0), (1, 1100.0), (2, 5000.0), (3, 4900.0)]  # a 3,900 deposit at t=2
    pnl = [(0, 0.0), (1, 100.0), (2, 100.0), (3, 0.0)]
    # The deposit is not a result (PnL is flat), but it is money at stake: 100 lost of 5,000 held (D-16).
    assert ws.max_drawdown(av, pnl) == pytest.approx(100 / 5000)
    assert ws.max_drawdown(av, []) is None


def test_drawdown_after_profits_are_withdrawn_is_not_total() -> None:
    """Regression (DEBUG D-16, live wallet 0xf5996cfe…): the trader took the profit out, leaving 1,800 at the
    peak, refilled to 20,000 and then lost 2,000. Dividing by the account at the peak said 100%; the money at
    stake from the peak to the low was 20,000, so the drawdown is 10%."""
    av = [(0, 10_000.0), (1, 1_800.0), (2, 20_000.0), (3, 18_000.0)]
    pnl = [(0, 0.0), (1, 40_000.0), (2, 40_000.0), (3, 38_000.0)]
    assert ws.max_drawdown(av, pnl) == pytest.approx(0.10)


def st_(
    trades: int = 40, pf: float | None = 2.5, hold: float | None = 6 * 3600, cov: float | None = 0.9, pnl: float = 1000
) -> ws.WalletStats:
    return ws.WalletStats(trades, trades // 2, 0.5, pf, pnl, hold, cov)


def g(
    st: ws.WalletStats,
    dd: float | None = 0.1,
    *,
    volume: float = 1e6,
    av: float = 1e5,
    roi: float = 0.2,
    long: ws.LongTerm | None = ws.LongTerm(365.0, 100_000.0, 12, 10),
    account: AccountState | None = None,
    now_ms: int | None = None,
) -> wg.Grade:
    return wg.grade(
        st, drawdown=dd, month_pnl=1000, month_volume=volume, account_value=av, month_roi=roi, long=long,
        account=account, now_ms=now_ms,
    )  # fmt: skip


def test_a_scalper_never_reaches_gold_however_good_its_numbers() -> None:
    assert g(st_(pf=50, hold=60)).tier == wg.BLACK
    assert g(st_(pf=50, hold=60)).style == wg.SCALPER


def test_style_comes_from_hold_time_not_fill_count() -> None:
    # Regression: a whale that slices one order into thousands of fills was graded SCALPER because its
    # fills filled every page. `grade` no longer takes the page-truncation flag at all; a 20 h median
    # hold is a swing trader however many fills it took.
    assert g(st_(hold=20 * 3600)).style == wg.SWING


def test_scalper_boundary_is_exactly_the_setting() -> None:
    limit = settings.scalper_max_median_hold_min * 60
    assert g(st_(hold=limit - 1)).style == wg.SCALPER
    assert g(st_(hold=limit)).style == wg.SWING


def test_market_maker_by_turnover_and_small_roi() -> None:
    assert g(st_(), volume=200 * 1e5, roi=0.02).style == wg.MARKET_MAKER
    assert g(st_(), volume=200 * 1e5, roi=0.03).style != wg.MARKET_MAKER


def test_gold_and_silver_at_their_exact_thresholds() -> None:
    gold = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor)
    assert g(gold, dd=settings.gold_max_drawdown).tier == wg.GOLD
    assert g(gold, dd=settings.gold_max_drawdown + 0.001).tier == wg.SILVER
    silver = st_(trades=settings.silver_min_trades, pf=settings.silver_min_profit_factor)
    assert g(silver, dd=settings.silver_max_drawdown).tier == wg.SILVER
    assert g(st_(trades=settings.silver_min_trades - 1)).tier == wg.WHITE


def test_unknown_evidence_never_grants_a_tier() -> None:
    assert g(st_(pf=None)).tier == wg.WHITE
    assert g(st_(), dd=None).tier == wg.WHITE
    assert g(st_(cov=None)).tier == wg.WHITE


def test_low_coverage_and_losers_are_black() -> None:
    assert g(st_(cov=0.49)).tier == wg.BLACK
    assert g(st_(pf=0.9)).tier == wg.BLACK
    assert g(st_(pnl=-1)).tier == wg.BLACK


DAY_MS = 86_400_000


def test_long_term_counts_whole_months_and_the_year_pnl() -> None:
    # 400 days of history, +100 a day except a losing block of 30 days (days 40..70 back from the end).
    pts = []
    pnl = 0.0
    for d in range(401):
        back = 400 - d
        pnl += -150.0 if 40 < back <= 70 else 100.0
        pts.append((d * DAY_MS, pnl))
    lt = ws.long_term(pts, max_days=365)
    assert lt is not None and round(lt.days) == 365 and lt.months == 12
    assert lt.months_up == 11  # the losing block is exactly one whole month
    assert ws.long_term(pts[:1], max_days=365) is None
    assert ws.long_term(pts[:20], max_days=365) == ws.LongTerm(19.0, 1900.0, 0, 0)


def test_gold_needs_a_long_steady_record_at_its_boundaries() -> None:
    """Rules from LONG_HISTORY_DAYS / GOLD_MIN_HISTORY_DAYS / GOLD_MIN_MONTHS_UP_SHARE, tested AT the values."""
    gold = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor)
    days, share = settings.gold_min_history_days, settings.gold_min_months_up_share
    assert g(gold, long=ws.LongTerm(days, 1.0, 10, round(10 * share))).tier == wg.GOLD
    assert g(gold, long=ws.LongTerm(days - 1, 1.0, 10, 10)).tier == wg.SILVER  # too short a record
    assert g(gold, long=ws.LongTerm(days, 1.0, 100, round(100 * share) - 1)).tier == wg.SILVER  # too few up
    assert g(gold, long=None).tier == wg.SILVER  # unknown record never grants GOLD


def test_a_losing_long_record_is_never_copied() -> None:
    good = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor)
    assert g(good, long=ws.LongTerm(365, 0.0, 12, 6)).tier == wg.BRONZE  # exactly zero is not a profit
    assert g(good, long=ws.LongTerm(365, 0.01, 12, 8)).tier == wg.GOLD


def test_a_steady_record_ranks_higher() -> None:
    st = st_(trades=40, pf=2.0)
    assert wg.score(st, 0.2, 1.0) == 2 * wg.score(st, 0.2, None)
    assert wg.score(st, 0.2, 0.75) > wg.score(st, 0.2, 0.5)


def acct(av: float = 100_000.0, notional: float = 100_000.0, upnl: float = 0.0) -> AccountState:
    return AccountState(account_value=av, notional=notional, unrealized_pnl=upnl)


def test_hidden_open_losses_reject_at_the_limit() -> None:
    """Profit factor counts closed trades only: a trader who holds losers open looks perfect without this."""
    good = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor)
    limit = settings.max_unrealized_loss_share * 100_000.0
    assert g(good, account=acct(upnl=-limit)).tier == wg.GOLD  # exactly at the limit passes
    assert g(good, account=acct(upnl=-limit - 1)).tier == wg.BLACK


def test_account_leverage_rejects_above_the_limit() -> None:
    good = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor)
    at = settings.max_wallet_leverage * 100_000.0
    assert g(good, account=acct(notional=at)).tier == wg.GOLD
    assert g(good, account=acct(notional=at + 1)).tier == wg.BLACK
    assert g(good, account=None).tier == wg.GOLD  # unknown skips the check


def test_an_inactive_wallet_is_not_copied() -> None:
    now = 100 * DAY_MS
    edge = now - int(settings.max_inactive_days * DAY_MS)
    good = replace(st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor), last_fill_ms=edge)
    assert g(good, now_ms=now).tier == wg.GOLD
    assert g(replace(good, last_fill_ms=edge - 1), now_ms=now).tier == wg.WHITE


def test_one_lucky_trade_is_not_a_method() -> None:
    good = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor)
    share = settings.max_biggest_win_share
    assert g(replace(good, biggest_win_share=share)).tier == wg.GOLD
    assert g(replace(good, biggest_win_share=share + 0.001)).tier == wg.BRONZE


def test_stats_scratches_biggest_win_and_last_fill() -> None:
    def ep(pnl: float, t: int) -> ws.Episode:
        return ws.Episode("ETH", "long", t, t + 3_600_000, pnl, 1000.0)

    # Equity 100k: scratch line 500. Losses of 400 are scratches, the 600 loss is real.
    eps = [ep(3000, 1), ep(1000, 2), ep(-400, 3), ep(-400, 4), ep(-600, 5)]
    fills = [Fill("ETH", 1.0, 1.0, "B", 9_000, 0.0, "Open Long", 0.0, 0.0)]
    st = ws.stats(eps, fills, {"ETH"}, equity=100_000.0)
    assert st.win_rate == 2 / 3 and st.trades == 5
    assert st.profit_factor == 4000 / 1400  # every dollar still counts in PF
    assert st.biggest_win_share == 3000 / 2600
    assert st.last_fill_ms == 9_000


def test_a_fast_trader_is_graded_like_anyone_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Who trades (person or bot) does not matter, only whether copying pays. COPY_FAST_TRADERS lets a SCALPER
    reach GOLD/SILVER on the same rules; a market maker stays BLACK either way (copying it pays its spread)."""
    fast = st_(trades=settings.gold_min_trades, pf=settings.gold_min_profit_factor, hold=60)
    assert g(fast).tier == wg.BLACK
    monkeypatch.setattr(settings, "copy_fast_traders", True)
    assert g(fast).style == wg.SCALPER and g(fast).tier == wg.GOLD
    assert g(st_(), volume=200 * 1e5, roi=0.02).tier == wg.BLACK  # market maker


def test_recent_losses_counts_real_losses_inside_the_window() -> None:
    def ep(pnl: float, closed_days_ago: float) -> ws.Episode:
        t = int((100 - closed_days_ago) * DAY_MS)
        return ws.Episode("ETH", "long", t - 3_600_000, t, pnl, 1000.0)

    now = 100 * DAY_MS
    scratch = settings.scratch_loss_share * 100_000.0  # 500 on a 100k account
    eps = [ep(-600, 1), ep(-scratch, 2), ep(-scratch - 1, 3), ep(-900, 8), ep(+5000, 1), ep(-700, 31)]
    assert ws.recent_losses(eps, now_ms=now, days=7, equity=100_000.0) == 2  # exactly the scratch line is no loss
    assert ws.recent_losses(eps, now_ms=now, days=30, equity=100_000.0) == 3
    assert ws.recent_losses(eps, now_ms=now, days=31, equity=100_000.0) == 4  # AT the window edge counts


NOW = 100 * DAY_MS
YEAR = ws.LongTerm(365.0, 100_000.0, 12, 9)


def eps_(
    wins: int, losses: int, *, win: float = 1000.0, loss: float = 1000.0, days_ago: float = 10.0
) -> list[ws.Episode]:
    t = int(NOW - days_ago * DAY_MS)
    out = [ws.Episode("ETH", "long", t - 3_600_000, t, win, 1000.0) for _ in range(wins)]
    return out + [ws.Episode("ETH", "long", t - 3_600_000, t, -loss, 1000.0) for _ in range(losses)]


def sc(e: list[ws.Episode], long: ws.LongTerm | None = YEAR) -> wg.ScoreParts:
    return wg.wallet_score(e, now_ms=NOW, equity=100_000.0, long=long)


def test_score_parts_at_their_edges() -> None:
    # Reliability: adjusted loss rate AT 5% is full, AT 50% is none (straight line between).
    assert wg._line(wg.BEST_LOSS_RATE, wg.BEST_LOSS_RATE, wg.WORST_LOSS_RATE) == 1.0
    assert wg._line(wg.WORST_LOSS_RATE, wg.BEST_LOSS_RATE, wg.WORST_LOSS_RATE) == 0.0
    # Loss size: average loss AT 1x the average win is full, AT 3x none.
    assert sc(eps_(10, 2, loss=1000.0)).loss_size == 1.0
    assert sc(eps_(10, 2, loss=3000.0)).loss_size == 0.0
    assert sc(eps_(10, 0)).loss_size == wg.UNKNOWN_PAYOFF_PART  # no loss yet: unproven, not perfect
    # History: unknown record is a low constant; a full year with most months up nearly full.
    assert sc(eps_(10, 1), long=None).history == wg.UNKNOWN_HISTORY_PART
    assert sc(eps_(10, 1), long=ws.LongTerm(365.0, 1.0, 12, 12)).history == 13 / 14


def test_a_lucky_wallet_cannot_score_high() -> None:
    """3 trades, 0 losses, a perfect year: capped at 5. At exactly SCORE_MIN_TRADES the cap lifts."""
    perfect = ws.LongTerm(365.0, 1.0, 12, 12)
    few = settings.score_min_trades - 1
    assert sc(eps_(few, 0), long=perfect).score == wg.SCORE_CAP_FEW_TRADES
    assert sc(eps_(settings.score_min_trades, 0), long=perfect).score > wg.SCORE_CAP_FEW_TRADES
    # Shrinkage: 0 losses in 5 trades is not a 0% loss rate.
    assert sc(eps_(5, 0)).loss_rate == (0 + wg.PRIOR_TRADES * wg.PRIOR_LOSS_RATE) / (5 + wg.PRIOR_TRADES)


def test_big_losses_and_recent_losses_lower_the_score() -> None:
    same_count = sc(eps_(57, 3, loss=1000.0)).score
    assert sc(eps_(57, 3, loss=5000.0)).score < same_count - 2  # martingale: small wins, big losses
    old_losses = eps_(40, 0, days_ago=10.0) + eps_(0, 8, days_ago=20.0)
    new_losses = eps_(40, 0, days_ago=10.0) + eps_(0, 8, days_ago=2.0)  # inside RECENT_DAYS: counted double
    assert sc(new_losses).score < sc(old_losses).score


def test_small_wins_count_only_small_losses_are_scratches() -> None:
    """Regression (DEBUG D-21): the scratch line (SCRATCH_LOSS_SHARE of the account) was applied to wins too, so a
    big account's normal wins vanished — a $5M wallet lost every trade under $25k and showed "0 trades". Only a
    LOSS that small is a scratch; every win counts."""
    scratch = settings.scratch_loss_share * 100_000.0  # 500
    e = eps_(48, 0, win=scratch / 2) + eps_(2, 2, loss=scratch / 2)[2:] + eps_(0, 2, loss=scratch * 4)
    p = sc(e)
    assert p.trades == 50 and p.losses == 2  # 48 small wins + 2 real losses; the 2 tiny losses are scratches
