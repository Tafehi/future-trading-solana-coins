"""The go-live gates (plan: "Go-live"). The bot never goes live by itself; `python -m app.tools.golive_check` prints
each gate so the user can decide. Every gate is from ai-kucoin-future's `method_check` final-choice rules.

1. GOLIVE_MIN_TRADES closed trades, on GOLIVE_MIN_DAYS different UTC days, from GOLIVE_MIN_WALLETS wallets.
2. Banked profit > 0 after fees and funding.
3. Banked profit minus the single best trade > 0 (one lucky trade is not a method).
4. Profit factor >= GOLIVE_MIN_PROFIT_FACTOR.
5. No liquidation and the account never halted.
6. This bot has its own KuCoin account and key (KUCOIN_KEY_DEDICATED; user, 2026-11-16, BUG B-3). Not a result of
   paper trading: the reminder that both futures bots share one KuCoin key today, shown at the moment of deciding.
"""

from dataclasses import dataclass

from app.config import settings
from app.trading import futures_paper as fp


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


def profit_factor(nets: list[float]) -> float | None:
    wins = sum(n for n in nets if n > 0)
    losses = -sum(n for n in nets if n < 0)
    if not nets:
        return None
    if losses <= 0:
        return float("inf") if wins > 0 else None
    return wins / losses


def gates(closed: list[dict[str, object]], *, halted: bool) -> list[Gate]:
    """`closed`: rows with net_pnl, closed_at, wallet, reason. Pure."""
    nets = [float(r["net_pnl"] or 0.0) for r in closed]  # type: ignore[arg-type]
    days = {int(float(r["closed_at"]) // 86400) for r in closed}  # type: ignore[arg-type]
    wallets = {str(r["wallet"]) for r in closed}
    banked = sum(nets)
    best = max(nets, default=0.0)
    pf = profit_factor(nets)
    liquidations = sum(1 for r in closed if r["reason"] == fp.LIQUIDATED)
    return [
        Gate(
            "sample",
            len(nets) >= settings.golive_min_trades
            and len(days) >= settings.golive_min_days
            and len(wallets) >= settings.golive_min_wallets,
            f"{len(nets)} trades (need {settings.golive_min_trades}), {len(days)} days "
            f"(need {settings.golive_min_days}), {len(wallets)} wallets (need {settings.golive_min_wallets})",
        ),
        Gate("banked > 0", banked > 0, f"banked ${banked:+,.2f} after fees and funding"),
        Gate("without best trade > 0", banked - best > 0, f"${banked - best:+,.2f} without the best (${best:+,.2f})"),
        Gate(
            "profit factor",
            pf is not None and pf >= settings.golive_min_profit_factor,
            f"PF {pf if pf is None else round(pf, 2)} (need {settings.golive_min_profit_factor:g})",
        ),
        Gate(
            "no liquidation, never halted",
            liquidations == 0 and not halted,
            f"{liquidations} liquidations, halted={halted}",
        ),
        Gate(
            "own KuCoin key",
            settings.kucoin_key_dedicated,
            "ok: this bot has its own KuCoin sub-account and key"
            if settings.kucoin_key_dedicated
            else "REMINDER: both futures bots share one KuCoin account and key. Before real money, create a KuCoin "
            "sub-account + API key for EACH bot (locked to its box's IP), then KUCOIN_KEY_DEDICATED=true (BUG B-3)",
        ),
    ]
