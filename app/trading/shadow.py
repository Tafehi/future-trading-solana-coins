"""Shadow copies: what copying a wallet would have earned, without money (user, 2026-11-13, item 4).

- `open_shadow`: called by the copier for every OPEN of a GOLD/SILVER wallet (tracked or watched) that passed the
  market and probe gates — the same filters a real copy meets. Entry = the best ask (long) or bid (short) of the book
  the copier just read; one open shadow per (wallet, contract).
- `mark_and_exit`: our exits (`futures_paper.exit_reason`: liquidation at LEVERAGE, stop, trail, max hold) on the
  contracts' MARK prices from the market cache (refreshed every COLD_LOOP_SECONDS, so no extra KuCoin calls; a stop can
  fire up to a minute late — the same for every wallet, so the ranking stays fair). Exit at the mark.
- Return = the price move in our favour, in %, minus two taker fees; a liquidation = -100/LEVERAGE (the margin).
- `stats`: per wallet, closed shadow copies in SHADOW_WINDOW_DAYS and their mean return. It lives in
  `app/db/crossread.py` since D-30 (the scout reads it from the trader's file without importing trading code);
  `stats` here is the trader's own read of its own file.
"""

import time

import aiosqlite

from app.config import settings
from app.data.kucoin import Contract, OrderBook
from app.data.market import MarketCache
from app.db import crossread, store
from app.db.crossread import ShadowStats
from app.trading import futures_paper as fp
from app.utils.logging import get_logger

logger = get_logger(__name__)


def net_return_pct(side: str, entry: float, exit_px: float, taker_fee: float, reason: str) -> float:
    """Price % in our favour minus two taker fees. Pure."""
    if reason == fp.LIQUIDATED:
        return -100.0 / settings.leverage
    return fp.price_move_pct(side, entry, exit_px) - 2.0 * taker_fee * 100.0


async def open_shadow(
    conn: aiosqlite.Connection,
    *,
    wallet: str,
    source: str,
    tier: str,
    signal_id: int,
    contract: Contract,
    coin: str,
    side: str,
    book: OrderBook,
    now: float,
) -> int | None:
    async with conn.execute(
        "SELECT 1 FROM shadow_positions WHERE status = 'OPEN' AND wallet = ? AND symbol = ?", (wallet, contract.symbol)
    ) as cur:
        if await cur.fetchone():
            return None
    entry = book.best_ask if side == fp.LONG else book.best_bid
    if entry <= 0:
        return None
    liq = fp.liquidation_price(side, entry, settings.leverage, contract.mmr)
    cur = await conn.execute(
        "INSERT INTO shadow_positions (wallet, source, tier, signal_id, symbol, coin, side, entry_px, best_px, liq_px, "
        "taker_fee, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (wallet, source, tier, signal_id, contract.symbol, coin, side, entry, entry, liq, contract.taker_fee, now),
    )
    return int(cur.lastrowid or 0)


async def mark_and_exit(market: MarketCache, *, now: float | None = None) -> int:
    """Mark every open shadow copy and close what our exits close. Returns how many closed."""
    at = time.time() if now is None else now
    closed = 0
    async with store.session() as conn:
        async with conn.execute("SELECT * FROM shadow_positions WHERE status = 'OPEN'") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            c = market.contract(str(r["symbol"]))
            mark = c.mark_price if c else 0.0
            if mark <= 0:
                continue
            best = fp.update_best(str(r["side"]), float(r["best_px"]), mark)
            reason = fp.exit_reason(
                side=str(r["side"]), entry=float(r["entry_px"]), best_px=best, mark=mark, liq_px=r["liq_px"],
                opened_at=float(r["opened_at"]), now=at, stop_pct=settings.stop_loss_price_pct,
                trail_activate_pct=settings.trail_activate_price_pct,
                trail_giveback_pct=settings.trail_giveback_price_pct, max_hold_hours=settings.max_hold_hours,
            )  # fmt: skip
            if not reason:
                await conn.execute("UPDATE shadow_positions SET best_px = ? WHERE id = ?", (best, r["id"]))
                continue
            ret = net_return_pct(str(r["side"]), float(r["entry_px"]), mark, float(r["taker_fee"]), reason)
            await conn.execute(
                "UPDATE shadow_positions SET status = 'CLOSED', best_px = ?, exit_px = ?, closed_at = ?, reason = ?, "
                "ret_pct = ? WHERE id = ?",
                (best, mark, at, reason, ret, r["id"]),
            )
            closed += 1
    return closed


async def stats(*, now: float | None = None) -> dict[str, ShadowStats]:
    return await crossread.shadow_stats(now=now)
