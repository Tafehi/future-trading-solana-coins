"""Paper futures arithmetic. Pure: no I/O, no clock, no settings beyond what is passed in.

- Sizes: a KuCoin futures order is a whole number of lots; one contract = `multiplier` coins.
  `contracts_for` rounds DOWN, so we never trade more than sized.
- Fills walk the real order-book snapshot level by level, so spread and slippage are paid. A book too
  thin for the order refuses it rather than inventing liquidity past level 20.
- Isolated-margin liquidation (KuCoin's formula without the tiny fee term):
    long:  liq = entry x (1 - 1/leverage + mmr)
    short: liq = entry x (1 + 1/leverage - mmr)
  A mark AT the liquidation price liquidates (`<=` for longs, `>=` for shorts).
- Funding: payment = qty x mark x rate. A positive rate means longs pay shorts. Returned signed from OUR
  side: positive is money received.
"""

import math
from dataclasses import dataclass

from app.data.kucoin import OrderBook

LONG = "long"
SHORT = "short"

# Exit reasons, stored on the position row.
WALLET_CLOSED = "WALLET_CLOSED"
STOP_LOSS = "STOP_LOSS"
TRAIL = "TRAIL"
MAX_HOLD = "MAX_HOLD"
LIQUIDATED = "LIQUIDATED"
ARB_FUNDING_FADED = "ARB_FUNDING_FADED"
ARB_REBALANCE = "ARB_REBALANCE"
DELISTED = "DELISTED"
REPLACED = "REPLACED"  # a higher-score wallet took this copy's slot (SCORE_WINDOW_DAYS)


# Decimal places kept before a percentage is compared with its limit.
ROUND = 9


class FillRefused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FillResult:
    avg_px: float
    size: float  # in book units (contracts for futures, coins for spot)
    slippage_bps: float  # vs mid, always >= 0 for a fill on the far side


def contracts_for(notional_usd: float, price: float, multiplier: float, lot_size: float) -> float:
    if price <= 0 or multiplier <= 0 or lot_size <= 0:
        return 0.0
    raw = notional_usd / (price * multiplier)
    lots = math.floor(raw / lot_size + 1e-9)
    return lots * lot_size


def walk(book: OrderBook, *, buy: bool, size: float, max_slippage_bps: float) -> FillResult:
    """Fill `size` against the snapshot. Raises FillRefused on a thin book or too much slippage."""
    if size <= 0:
        raise FillRefused("SIZE_ZERO", "order rounds to zero lots")
    levels = book.asks if buy else book.bids
    mid = book.mid
    if not levels or mid <= 0:
        raise FillRefused("NO_BOOK", f"{book.symbol}: empty book")
    left = size
    cost = 0.0
    for price, available in levels:
        take = min(left, available)
        cost += take * price
        left -= take
        if left <= 1e-12:
            break
    if left > 1e-12:
        raise FillRefused("THIN_BOOK", f"{book.symbol}: {size:g} wanted, book holds {size - left:g}")
    avg = cost / size
    slip = (avg - mid) / mid * 10_000.0 if buy else (mid - avg) / mid * 10_000.0
    if slip > max_slippage_bps:
        raise FillRefused("SLIPPAGE", f"{book.symbol}: {slip:.1f} bps > {max_slippage_bps:g}")
    return FillResult(avg, size, max(0.0, slip))


def liquidation_price(side: str, entry: float, leverage: float, mmr: float) -> float:
    if side == LONG:
        return max(0.0, entry * (1.0 - 1.0 / leverage + mmr))
    return entry * (1.0 + 1.0 / leverage - mmr)


def is_liquidated(side: str, mark: float, liq_px: float | None) -> bool:
    if liq_px is None or liq_px <= 0:
        return False
    return mark <= liq_px if side == LONG else mark >= liq_px


def unrealized(side: str, qty: float, entry: float, mark: float) -> float:
    move = (mark - entry) * qty
    return move if side == LONG else -move


def funding_received(side: str, qty: float, mark: float, rate: float) -> float:
    """Signed from our side: positive = we receive. Positive rate: longs pay, shorts receive."""
    payment = qty * mark * rate
    return -payment if side == LONG else payment


def price_move_pct(side: str, entry: float, price: float) -> float:
    """The move in OUR favour, in percent. Negative when the trade is losing."""
    if entry <= 0:
        return 0.0
    raw = (price - entry) / entry * 100.0
    return raw if side == LONG else -raw


def update_best(side: str, best: float, mark: float) -> float:
    return max(best, mark) if side == LONG else min(best, mark)


def exit_reason(
    *,
    side: str,
    entry: float,
    best_px: float,
    mark: float,
    liq_px: float | None,
    opened_at: float,
    now: float,
    stop_pct: float,
    trail_activate_pct: float,
    trail_giveback_pct: float,
    max_hold_hours: float,
) -> str:
    """The first exit that applies, or "". Liquidation is checked first: it happens on the exchange no
    matter what our own stop says."""
    if is_liquidated(side, mark, liq_px):
        return LIQUIDATED
    # Every percentage is rounded before it meets its limit: 3.6 / 90 is 3.9999999999999996% in floats, which
    # would let a trade that gave back exactly the limit stay open (LESSON.md L-1).
    if round(price_move_pct(side, entry, mark), ROUND) <= -stop_pct:
        return STOP_LOSS
    best_move = round(price_move_pct(side, entry, best_px), ROUND)
    if best_move >= trail_activate_pct and best_px > 0:
        # How far the price has come back from the best it reached, in percent of that best price.
        moved_back = (best_px - mark) if side == LONG else (mark - best_px)
        if round(moved_back / best_px * 100.0, ROUND) >= trail_giveback_pct:
            return TRAIL
    if now - opened_at >= max_hold_hours * 3600.0:
        return MAX_HOLD
    return ""


# An exit must always happen. When the 20-level snapshot is too thin for the whole size, the rest is
# priced at the last level moved a further 1% against us: pessimistic, never invented in our favour.
THIN_EXIT_PENALTY = 0.01


def exit_fill(book: OrderBook, *, buy: bool, size: float, fallback_px: float) -> float:
    """Average exit price for `size`. Never refuses. An empty book exits at `fallback_px` (the mark)."""
    levels = book.asks if buy else book.bids
    if size <= 0 or not levels:
        return fallback_px
    left = size
    cost = 0.0
    for price, available in levels:
        take = min(left, available)
        cost += take * price
        left -= take
        if left <= 1e-12:
            return cost / size
    last = levels[-1][0]
    cost += left * last * (1.0 + THIN_EXIT_PENALTY if buy else 1.0 - THIN_EXIT_PENALTY)
    return cost / size
