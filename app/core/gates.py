"""The market gate, account limits and sizing. Pure.

The market gate never looks at the wallet, only at the market right now:
- NOT_LISTED: the coin has no open KuCoin contract.
- LOW_TURNOVER: the contract trades less than MIN_TURNOVER_USD a day.
- NO_BOOK / SPREAD: empty book, or wider than MAX_SPREAD_BPS.
- ENTRY_DRIFT: the KuCoin price is more than MAX_ENTRY_DRIFT_PCT from the wallet's entry.
- FUNDING_AGAINST: funding this interval would charge our side more than MAX_ADVERSE_FUNDING.

`probe_gate` is the one wallet check: a tiny opening trade is the wallet testing, not meaning it.
`account_refusal` is the one-account version of ai-kucoin-future's `book_refusal`. There is deliberately no
drawdown or daily-loss stop (user, 2026-11-13: review after 2-3 weeks of paper; BUG.md B-3).

Ported from ai-kucoin-future `app/core/gates.py`.
"""

from dataclasses import dataclass

from app.config import settings
from app.data.kucoin import Contract, OrderBook

LONG = "long"


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str


def market_gate(
    *, contract: Contract | None, book: OrderBook | None, side: str, wallet_entry_px: float
) -> Refusal | None:
    if contract is None or contract.status != "Open":
        return Refusal("NOT_LISTED", "no open KuCoin contract")
    if contract.turnover_24h < settings.min_turnover_usd:
        return Refusal("LOW_TURNOVER", f"24h turnover ${contract.turnover_24h:,.0f}")
    if book is None or book.mid <= 0:
        return Refusal("NO_BOOK", "empty order book")
    if book.spread_bps > settings.max_spread_bps:
        return Refusal("SPREAD", f"{book.spread_bps:.1f} bps > {settings.max_spread_bps:g}")
    if wallet_entry_px > 0:
        drift = abs(book.mid - wallet_entry_px) / wallet_entry_px * 100.0
        if drift > settings.max_entry_drift_pct:
            return Refusal("ENTRY_DRIFT", f"price {drift:.2f}% from the wallet's entry")
    rate = contract.funding_rate
    if (side == LONG and rate > settings.max_adverse_funding) or (
        side != LONG and rate < -settings.max_adverse_funding
    ):
        return Refusal("FUNDING_AGAINST", f"funding {rate:+.4%} per interval against a {side}")
    return None


def account_refusal(*, halted: bool, paused: bool, open_count: int, symbol_held: bool) -> Refusal | None:
    if halted:
        return Refusal("ACCOUNT_HALTED", "ledger drift halted the account")
    if paused:
        return Refusal("ENTRIES_PAUSED", "live reconcile mismatch; new entries paused")
    if symbol_held:
        return Refusal("SYMBOL_HELD", "already holding this contract")
    if open_count >= settings.max_open_positions:
        return Refusal("MAX_POSITIONS", f"{open_count} open")
    return None


def probe_gate(*, notional: float, account_value: float, tier: str, copy_weight: int) -> Refusal | None:
    """SMALL_PROBE when the wallet's own position is below PROBE_MIN_USD and below PROBE_MIN_ACCOUNT_SHARE of its
    account value. A GOLD wallet at PROBE_EXEMPT_WEIGHT or more is always copied."""
    if tier == "GOLD" and copy_weight >= settings.probe_exempt_weight:
        return None
    if notional >= settings.probe_min_usd:
        return None
    if account_value > 0 and notional >= account_value * settings.probe_min_account_share:
        return None
    return Refusal("SMALL_PROBE", f"the wallet put only ${notional:,.0f} in")


def margin_for(*, equity: float, share: float) -> float:
    """Margin to lock for one copy trade. Notional is this times LEVERAGE."""
    return max(0.0, equity * settings.position_fraction * share)
