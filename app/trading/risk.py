"""Everything that happens to open positions between signals (ported from ai-kucoin-future `trading/risk.py`).

- `mark_and_exit` (every RISK_LOOP_SECONDS): read each held contract's MARK price, update the best price, and close
  what must close: liquidation, the stop, the trailing stop, max hold (`futures_paper.exit_reason`).
- `settle_funding`: KuCoin's settled funding history for each held contract; every settlement after the position
  opened and after the last one applied. Exact rates, never estimated.
- `snapshot`: equity, peak equity and the ledger identity. A drift over `ledger.DRIFT_TOLERANCE` halts the account.
- `live_reconcile` (live only, every RECONCILE_SECONDS): our open positions against KuCoin's. A position the exchange
  stop closed is booked from that stop order's fills; any other mismatch pauses new entries and alarms.
- `kill_switch`: the STOP file closes everything.
"""

import os
import time
from collections import defaultdict

from app.config import settings
from app.data.http import DataError
from app.data.kucoin import KucoinFutures
from app.data.kucoin_private import KucoinPrivate
from app.data.market import MarketCache
from app.db import store
from app.trading import futures_paper as fp
from app.trading import ledger
from app.trading.copier import Closed, close_one
from app.trading.executor import Executor
from app.trading.kucoin_executor import sum_fills
from app.utils.logging import get_logger

logger = get_logger(__name__)

EXCHANGE_STOP = "STOP_LOSS_EXCHANGE"
KILL_SWITCH = "KILL_SWITCH"


async def mark_and_exit(
    kf: KucoinFutures, market: MarketCache, executor: Executor, *, now: float | None = None
) -> list[Closed]:
    at = time.time() if now is None else now
    async with store.session() as conn:
        held = await ledger.open_positions(conn, settings.account)
    by_symbol: dict[str, list[ledger.Position]] = defaultdict(list)
    for p in held:
        by_symbol[p.symbol].append(p)
    prices: dict[str, float] = {}
    for symbol in by_symbol:
        try:
            prices[symbol] = await kf.mark_price(symbol)
        except DataError as exc:
            logger.warning("risk: no mark for %s this pass: %s", symbol, exc)

    exits: list[tuple[ledger.Position, str, float]] = []
    async with store.session() as conn:
        for symbol, group in by_symbol.items():
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            for pos in group:
                await ledger.mark(conn, pos, price)
                reason = fp.exit_reason(
                    side=pos.side, entry=pos.entry_px, best_px=fp.update_best(pos.side, pos.best_px, price),
                    mark=price, liq_px=pos.liq_px, opened_at=pos.opened_at, now=at,
                    stop_pct=settings.stop_loss_price_pct, trail_activate_pct=settings.trail_activate_price_pct,
                    trail_giveback_pct=settings.trail_giveback_price_pct, max_hold_hours=settings.max_hold_hours,
                )  # fmt: skip
                if reason:
                    exits.append((pos, reason, price))
    closed: list[Closed] = []
    for pos, reason, price in exits:
        async with store.session() as conn:
            fresh = await ledger.get_position(conn, pos.id)
        if fresh is None or fresh.status != ledger.OPEN:
            continue
        try:
            closed.append(await close_one(executor, market, fresh, reason=reason, fallback_px=price, now=at))
        except DataError as exc:
            logger.error("risk: closing #%d %s failed: %s", pos.id, pos.symbol, exc)
    return closed


async def settle_funding(kf: KucoinFutures, *, now: float | None = None) -> int:
    """Apply settled funding to open positions. Returns how many settlements were applied."""
    at = time.time() if now is None else now
    async with store.session() as conn:
        held = await ledger.open_positions(conn, settings.account)
    by_symbol: dict[str, list[ledger.Position]] = defaultdict(list)
    for p in held:
        by_symbol[p.symbol].append(p)
    histories: dict[str, list[tuple[int, float]]] = {}
    for symbol, group in by_symbol.items():
        since = min(max(p.opened_at, p.last_funding_ts) for p in group)
        try:
            histories[symbol] = await kf.funding_history(symbol, start_ms=int(since * 1000), end_ms=int(at * 1000))
        except DataError as exc:
            logger.warning("funding: history for %s failed: %s", symbol, exc)
    applied = 0
    async with store.session() as conn:
        for symbol, group in by_symbol.items():
            for pos in group:
                after = max(pos.opened_at, pos.last_funding_ts)
                mark = pos.last_mark or pos.entry_px
                for t_ms, rate in histories.get(symbol, []):
                    t = t_ms / 1000.0
                    if t <= after or t > at:
                        continue
                    amount = fp.funding_received(pos.side, pos.qty, mark, rate)
                    await ledger.apply_funding(conn, pos, amount=amount, funding_ts=t)
                    after = t
                    applied += 1
    return applied


async def snapshot(*, now: float | None = None) -> str:
    """Record equity and check the ledger. Returns the halt reason when this pass halted the account, else ""."""
    at = time.time() if now is None else now
    account = settings.account
    async with store.session() as conn:
        eq = await ledger.equity(conn, account)
        await conn.execute(
            "INSERT OR REPLACE INTO equity_snapshots (account, ts, equity) VALUES (?, ?, ?)", (account, at, eq)
        )
        await conn.execute(
            "UPDATE account_state SET peak_equity = MAX(peak_equity, ?) WHERE account = ?", (eq, account)
        )
        drift = await ledger.reconcile(conn, account)
        st = await ledger.state(conn, account)
        # Rounded before the comparison: a drift of exactly one cent is 0.010000000000218 in floats and would halt
        # AT the tolerance instead of above it (LESSON L-1, the same trap as ai-kucoin-future's L-1).
        if round(abs(drift), 6) > ledger.DRIFT_TOLERANCE and not st.get("halted"):
            reason = f"ledger drift {drift:+.4f} USD"
            await ledger.halt(conn, account, reason)
            return reason
    return ""


async def set_paused(paused: bool, reason: str = "") -> None:
    async with store.session() as conn:
        await conn.execute(
            "UPDATE account_state SET paused = ?, pause_reason = ? WHERE account = ?",
            (1 if paused else 0, reason, settings.account),
        )


async def live_reconcile(client: KucoinPrivate, *, now: float | None = None) -> list[str]:
    """Compare our open positions with KuCoin's. Returns the problems found (empty = in agreement)."""
    at = time.time() if now is None else now
    async with store.session() as conn:
        ours = await ledger.open_positions(conn, settings.account)
    theirs = {str(p["symbol"]): float(p.get("currentQty") or 0) for p in await client.positions()}
    problems: list[str] = []
    for pos in ours:
        want = pos.contracts if pos.side == fp.LONG else -pos.contracts
        got = theirs.pop(pos.symbol, 0.0)
        if abs(got - want) < 1e-9:
            continue
        if got == 0 and pos.stop_order_id:
            rows = await client.fills(pos.stop_order_id)
            lots, avg, fee = sum_fills(rows, pos.multiplier)
            if lots > 0:
                async with store.session() as conn:
                    await ledger.close_position(conn, pos, exit_px=avg, fee=fee, reason=EXCHANGE_STOP, now=at)
                logger.info("reconcile: #%d %s closed by the exchange stop at %g", pos.id, pos.symbol, avg)
                continue
        problems.append(f"#{pos.id} {pos.symbol}: ours {want:+g} lots, KuCoin {got:+g}")
    for symbol, qty in theirs.items():
        problems.append(f"{symbol}: KuCoin holds {qty:+g} lots the bot does not know")
    await set_paused(bool(problems), "; ".join(problems)[:500])
    return problems


def kill_switch_on() -> bool:
    return os.path.exists(settings.stop_file)


async def close_everything(kf: KucoinFutures, market: MarketCache, executor: Executor) -> list[Closed]:
    async with store.session() as conn:
        held = await ledger.open_positions(conn, settings.account)
    out: list[Closed] = []
    for pos in held:
        px = pos.last_mark or pos.entry_px
        try:
            px = await kf.mark_price(pos.symbol)
        except DataError:
            pass
        try:
            out.append(await close_one(executor, market, pos, reason=KILL_SWITCH, fallback_px=px, now=time.time()))
        except DataError as exc:
            logger.error("kill switch: closing #%d failed: %s", pos.id, exc)
    return out
