"""Wallet evidence from raw Hyperliquid data. Pure functions, no I/O.

An *episode* is one position from flat to flat in one coin. It is the unit a copier experiences: we open
when the wallet opens and close when it closes, so a wallet is judged on its episodes, not on its fills.

- An episode starts at a fill whose `start_position` is 0 and ends at the fill that brings the position
  back to 0. A flip (long to short in one fill) ends one episode and starts the next.
- Fills for a coin before its first flat point are skipped: the history window cut into an episode we
  cannot price fully.
- Episode PnL = sum of `closed_pnl` minus every fee paid inside it.
"""

import statistics
from dataclasses import dataclass

from app.config import settings
from app.data.hyperliquid import Fill

EPS = 1e-9


@dataclass(frozen=True)
class Episode:
    coin: str
    side: str  # "long" | "short"
    opened_ms: int
    closed_ms: int
    pnl: float
    volume_usd: float

    @property
    def hold_s(self) -> float:
        return (self.closed_ms - self.opened_ms) / 1000.0


@dataclass(frozen=True)
class WalletStats:
    trades: int
    wins: int
    win_rate: float | None
    profit_factor: float | None
    total_pnl: float
    median_hold_s: float | None
    coverage: float | None  # share of traded USD volume in coins KuCoin lists
    biggest_win_share: float | None = None  # largest episode PnL / total PnL (None unless total > 0)
    last_fill_ms: int | None = None  # newest fill in the window: how recently the wallet traded


PF_CAP = 99.0  # a wallet with no losing episode gets this instead of infinity


def episodes(fills: list[Fill]) -> list[Episode]:
    out: list[Episode] = []
    open_state: dict[str, dict[str, float]] = {}
    for f in sorted(fills, key=lambda x: x.time_ms):
        after = f.start_position + f.signed_size
        state = open_state.get(f.coin)
        if state is None:
            if abs(f.start_position) > EPS:
                continue  # mid-episode at the start of the window
            if abs(after) <= EPS:
                continue
            open_state[f.coin] = {
                "opened": float(f.time_ms),
                "side": 1.0 if after > 0 else -1.0,
                "pnl": f.closed_pnl - f.fee,
                "vol": f.px * f.sz,
            }
            continue
        state["pnl"] += f.closed_pnl - f.fee
        state["vol"] += f.px * f.sz
        flipped = after * state["side"] < -EPS
        if abs(after) <= EPS or flipped:
            # On a flip, the closing part's PnL is already in `closed_pnl`; the fee is split by size.
            out.append(
                Episode(
                    coin=f.coin,
                    side="long" if state["side"] > 0 else "short",
                    opened_ms=int(state["opened"]),
                    closed_ms=f.time_ms,
                    pnl=state["pnl"],
                    volume_usd=state["vol"],
                )
            )
            del open_state[f.coin]
            if flipped:
                open_state[f.coin] = {
                    "opened": float(f.time_ms),
                    "side": 1.0 if after > 0 else -1.0,
                    "pnl": 0.0,
                    "vol": 0.0,
                }
    return out


def stats(eps: list[Episode], fills: list[Fill], listed_coins: set[str], *, equity: float = 0.0) -> WalletStats:
    """`equity` (the account value) sets the scratch line: a losing episode smaller than SCRATCH_LOSS_SHARE of
    it is a scratch, not a loss, for the win rate. Profit factor and total PnL still count every dollar."""
    wins = [e for e in eps if e.pnl > 0]
    losses = [e for e in eps if e.pnl < 0]
    scratch = settings.scratch_loss_share * equity
    real_losses = [e for e in losses if -e.pnl > scratch]
    total = sum(e.pnl for e in eps)
    gross_win = sum(e.pnl for e in wins)
    gross_loss = -sum(e.pnl for e in losses)
    pf: float | None
    if not eps:
        pf = None
    elif gross_loss <= EPS:
        pf = PF_CAP if gross_win > 0 else None
    else:
        pf = min(PF_CAP, gross_win / gross_loss)
    total_vol = sum(f.px * f.sz for f in fills)
    listed_vol = sum(f.px * f.sz for f in fills if f.coin in listed_coins)
    return WalletStats(
        trades=len(eps),
        wins=len(wins),
        win_rate=len(wins) / (len(wins) + len(real_losses)) if wins or real_losses else None,
        profit_factor=pf,
        total_pnl=total,
        median_hold_s=statistics.median(e.hold_s for e in eps) if eps else None,
        coverage=listed_vol / total_vol if total_vol > 0 else None,
        biggest_win_share=max(e.pnl for e in wins) / total if wins and total > 0 else None,
        last_fill_ms=max(f.time_ms for f in fills) if fills else None,
    )


def max_drawdown(account_values: list[tuple[int, float]], pnl_history: list[tuple[int, float]]) -> float | None:
    """Largest fall in cumulative PnL from a peak, as a fraction of the most money in the account from that
    peak to the low.

    PnL rather than account value, because deposits and withdrawals move the account value without being
    trading results. The base is the largest account value since the peak, not the value AT the peak: good
    traders sweep profits out, and right after a sweep the account can hold a few percent of the money that
    later trades, so a normal dip read as a 100% loss (DEBUG D-16). `None` when there is no history.
    """
    if not pnl_history:
        return None
    av_by_t = dict(account_values)
    times = sorted(av_by_t)

    def av_at(t: int) -> float:
        got = av_by_t.get(t)
        if got is None and times:
            got = av_by_t[min(times, key=lambda x: abs(x - t))]
        return got or 0.0

    worst = 0.0
    peak_pnl = pnl_history[0][1]
    at_stake = av_at(pnl_history[0][0])
    for t, pnl in pnl_history:
        if pnl > peak_pnl:
            peak_pnl, at_stake = pnl, av_at(t)
            continue
        at_stake = max(at_stake, av_at(t))
        if at_stake > 0:
            worst = max(worst, (peak_pnl - pnl) / at_stake)
    # Above 1.0 would mean losing more than was ever in the account; 1.0 says "lost everything" and keeps
    # every threshold comparison meaningful.
    return min(worst, 1.0)


def recent_losses(eps: list[Episode], *, now_ms: int, days: float, equity: float) -> int:
    """Real losses (a loss bigger than SCRATCH_LOSS_SHARE of the account) closed in the last `days`."""
    since = now_ms - int(days * 86_400_000)
    scratch = settings.scratch_loss_share * equity
    return sum(1 for e in eps if e.closed_ms >= since and -e.pnl > scratch)


@dataclass(frozen=True)
class LongTerm:
    """The wallet's record from `portfolio` perpAllTime, over at most LONG_HISTORY_DAYS (one year).

    `months` counts whole 30-day blocks ending at the newest point; `months_up` those with PnL > 0.
    """

    days: float
    pnl: float
    months: int
    months_up: int

    @property
    def up_share(self) -> float | None:
        return self.months_up / self.months if self.months else None


def long_term(pnl_history: list[tuple[int, float]], *, max_days: float) -> LongTerm | None:
    """`None` without history. PnL, not account value, so deposits and withdrawals do not count as results."""
    if len(pnl_history) < 2:
        return None
    pts = sorted(pnl_history)
    end_t, end_pnl = pts[-1]
    start_t = max(pts[0][0], end_t - int(max_days * 86_400_000))

    def pnl_at(t: int) -> float:
        """Cumulative PnL at the last point at or before `t` (the first point when `t` is earlier)."""
        best = pts[0][1]
        for pt, pv in pts:
            if pt > t:
                break
            best = pv
        return best

    months = up = 0
    block_end = end_t
    while block_end - 30 * 86_400_000 >= start_t:
        block_start = block_end - 30 * 86_400_000
        months += 1
        up += pnl_at(block_end) - pnl_at(block_start) > 0
        block_end = block_start
    return LongTerm((end_t - start_t) / 86_400_000, end_pnl - pnl_at(start_t), months, up)
