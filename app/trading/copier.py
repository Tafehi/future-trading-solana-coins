"""Turn copy signals into positions, and close positions. The same code in paper and live; only the executor differs.

`handle_pending` reads every unhandled `signals` row, oldest first:
- CLOSE: recorded only. Our own exits decide (ai-kucoin-future E-2/E-4: following the wallet's close lost money).
- OPEN: STALE -> NOT_TRACKED (not tracked or watched, or not GOLD/SILVER) -> NOT_LISTED -> market gate -> probe gate
  -> shadow copy (item 4; a watched wallet stops here, SHADOW_ONLY) -> account limits (with REPLACED for a clearly
  better wallet) -> sizing (SILVER weight capped at SILVER_MAX_WEIGHT, item 5) -> executor -> ledger -> (live) exchange
  stop order.

Network calls never run inside a database write: a write holds SQLite's lock until commit (ai-kucoin-future D-17).
"""

import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.core import gates, selection
from app.core import wallet_grader as wg
from app.data.http import DataError
from app.data.kucoin import Contract, KucoinFutures
from app.data.market import MarketCache
from app.db import store
from app.trading import futures_paper as fp
from app.trading import ledger, shadow
from app.trading.executor import Executor
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Closed:
    position_id: int
    symbol: str
    side: str
    net: float
    reason: str


def stop_price(side: str, entry: float) -> float:
    """The price at STOP_LOSS_PRICE_PCT against us. The bot's own stop and the exchange stop use the same number."""
    pct = settings.stop_loss_price_pct / 100.0
    return entry * (1.0 - pct) if side == fp.LONG else entry * (1.0 + pct)


async def close_one(
    executor: Executor, market: MarketCache, pos: ledger.Position, *, reason: str, fallback_px: float, now: float
) -> Closed:
    """Cancel the exchange stop, close at the executor's fill, write the ledger. DataError if a live close fails."""
    await executor.cancel_stop(pos)
    fill = await executor.close(pos, market.contract(pos.symbol), reason=reason, fallback_px=fallback_px)
    async with store.session() as conn:
        net = await ledger.close_position(conn, pos, exit_px=fill.avg_px, fee=fill.fee, reason=reason, now=now)
    logger.info("close #%d %s %s %s net %+.2f", pos.id, pos.side, pos.symbol, reason, net)
    return Closed(pos.id, pos.symbol, pos.side, net, reason)


class Copier:
    def __init__(self, kf: KucoinFutures, market: MarketCache, executor: Executor) -> None:
        self.kf = kf
        self.market = market
        self.executor = executor

    async def _mark(
        self, sid: int, *, refused: str = "", detail: str = "", symbol: str = "", copied: bool = False
    ) -> None:
        async with store.session() as conn:
            await conn.execute(
                "UPDATE signals SET handled = 1, copied = ?, refused = ?, detail = ?, symbol = ? WHERE id = ?",
                (1 if copied else 0, refused, detail[:300], symbol, sid),
            )
        if refused:
            logger.info("signal #%d refused %s: %s", sid, refused, detail)

    async def handle_pending(self, now: float | None = None) -> list[int]:
        """Handle every unhandled signal. Returns the ids of positions opened."""
        async with store.session() as conn:
            async with conn.execute("SELECT * FROM signals WHERE handled = 0 ORDER BY id") as cur:
                pending = [dict(r) for r in await cur.fetchall()]
        opened: list[int] = []
        for s in pending:
            at = time.time() if now is None else now
            if s["kind"] != "OPEN":
                await self._mark(int(s["id"]), refused="RECORD_ONLY", detail="our own exits decide")
                continue
            try:
                pid = await self._open(s, at)
            except DataError as exc:
                await self._mark(int(s["id"]), refused="DATA_ERROR", detail=str(exc))
                continue
            if pid:
                opened.append(pid)
        return opened

    async def _wallet(self, address: str) -> dict[str, Any] | None:
        async with store.session() as conn:
            async with conn.execute("SELECT * FROM wallets WHERE address = ?", (address,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def _open(self, s: dict[str, Any], at: float) -> int | None:
        sid = int(s["id"])
        if at - float(s["ts"]) > settings.signal_max_age_s:
            await self._mark(sid, refused="STALE", detail=f"{at - float(s['ts']):.0f}s old")
            return None
        w = await self._wallet(str(s["wallet"]))
        tiers = selection.eligible_tiers()
        if w is None or not (w["tracked"] or w["watched"]) or w["tier"] not in tiers:
            await self._mark(sid, refused="NOT_TRACKED", detail=f"wallet not a tracked or watched {'/'.join(tiers)}")
            return None
        mapping = self.market.mapping.get(str(s["coin"]))
        if mapping is None:
            await self._mark(sid, refused="NOT_LISTED", detail=f"{s['coin']} has no KuCoin contract")
            return None
        contract = self.market.contract(mapping.symbol) or mapping.contract
        symbol = contract.symbol
        side = str(s["side"])
        try:
            book = await self.kf.book(symbol)
        except DataError as exc:
            await self._mark(sid, refused="NO_BOOK", detail=str(exc), symbol=symbol)
            return None
        entry = float(s["wallet_entry_px"] or 0) * mapping.price_scale
        refusal = gates.market_gate(contract=contract, book=book, side=side, wallet_entry_px=entry)
        weight = int(w.get("copy_weight") or wg.MIN_COPY_WEIGHT)
        if w["tier"] == wg.SILVER:
            weight = min(weight, settings.silver_max_weight)  # item 5: SILVER copies stay small
        if refusal is None:
            refusal = gates.probe_gate(
                notional=float(s["wallet_size"] or 0),
                account_value=float(w.get("account_value") or 0),
                tier=str(w["tier"]),
                copy_weight=weight,
            )
        if refusal:
            await self._mark(sid, refused=refusal.code, detail=refusal.detail, symbol=symbol)
            return None

        # Item 4: the shadow copy, for every tracked or watched wallet whose open passed the gates a real copy meets.
        # Account limits (max positions, held contract, margin) do not stop it: it measures the wallet, not us.
        if settings.shadow_enabled:
            async with store.session() as conn:
                await shadow.open_shadow(
                    conn, wallet=str(s["wallet"]), source=str(s["source"]), tier=str(w["tier"]), signal_id=sid,
                    contract=contract, coin=str(s["coin"]), side=side, book=book, now=at,
                )  # fmt: skip
        if not w["tracked"]:
            await self._mark(sid, refused="SHADOW_ONLY", detail="watched wallet: shadow copy only", symbol=symbol)
            return None

        score = float(w.get("score") or 0.0)
        refusal = await self._make_room(symbol, score, at=at, fallback_px=book.mid)
        if refusal:
            await self._mark(sid, refused=refusal.code, detail=refusal.detail, symbol=symbol)
            return None

        async with store.session() as conn:
            equity = await ledger.equity(conn, settings.account)
            cash = await ledger.cash(conn, settings.account)
        margin = gates.margin_for(equity=equity, share=wg.size_share(weight))
        notional = margin * settings.leverage
        contracts = fp.contracts_for(notional, book.mid, contract.multiplier, contract.lot_size)
        real_notional = contracts * contract.multiplier * book.mid
        if real_notional < settings.min_order_usd:
            await self._mark(
                sid, refused="TOO_SMALL", detail=f"${real_notional:,.2f} after lot rounding", symbol=symbol
            )
            return None
        need = (
            real_notional / settings.leverage * (1 + settings.max_slippage_bps / 10_000)
            + real_notional * contract.taker_fee * 1.1
        )
        if need > cash:
            await self._mark(
                sid, refused="INSUFFICIENT_MARGIN", detail=f"needs ${need:,.2f}, has ${cash:,.2f}", symbol=symbol
            )
            return None
        try:
            fill = await self.executor.open(contract, side=side, contracts=contracts, leverage=settings.leverage)
        except fp.FillRefused as exc:
            await self._mark(sid, refused=exc.code, detail=exc.detail, symbol=symbol)
            return None
        async with store.session() as conn:
            pid = await ledger.open_position(
                conn, account=settings.account, symbol=symbol, coin=str(s["coin"]), side=side,
                contracts=fill.contracts, multiplier=contract.multiplier, entry_px=fill.avg_px,
                leverage=settings.leverage, mmr=contract.mmr, fee=fill.fee, now=at, signal_id=sid,
                wallet=str(s["wallet"]), source=str(s["source"]), tier=str(w["tier"]), copy_weight=weight,
                score=score, order_id=fill.order_id,
            )  # fmt: skip
        await self._mark(sid, copied=True, symbol=symbol, detail=f"position #{pid}")
        logger.info("open #%d %s %s %g lots @ %g (signal #%d)", pid, side, symbol, fill.contracts, fill.avg_px, sid)
        await self._place_stop(pid, contract)
        return pid

    async def _place_stop(self, pid: int, contract: Contract) -> None:
        async with store.session() as conn:
            pos = await ledger.get_position(conn, pid)
        if pos is None:
            return
        try:
            order_id = await self.executor.place_stop(pos, stop_px=stop_price(pos.side, pos.entry_px))
        except DataError as exc:
            # The bot's own stop still runs every RISK_LOOP_SECONDS; the alarm comes from the reconcile loop.
            logger.error("stop order for #%d %s failed: %s", pid, contract.symbol, exc)
            return
        if order_id:
            async with store.session() as conn:
                await ledger.set_stop_order(conn, pid, order_id)

    async def _make_room(self, symbol: str, score: float, *, at: float, fallback_px: float) -> gates.Refusal | None:
        async with store.session() as conn:
            st = await ledger.state(conn, settings.account)
            held = await ledger.open_positions(conn, settings.account)
        same = [p for p in held if p.symbol == symbol]
        refusal = gates.account_refusal(
            halted=bool(st.get("halted")), paused=bool(st.get("paused")), open_count=len(held), symbol_held=bool(same)
        )
        if refusal is None or refusal.code not in ("SYMBOL_HELD", "MAX_POSITIONS"):
            return refusal
        victims = same if same else held
        victim = min(victims, key=lambda p: p.score)
        if score < victim.score + settings.replace_min_score_gap:
            return refusal
        try:
            await close_one(
                self.executor, self.market, victim, reason=fp.REPLACED,
                fallback_px=victim.last_mark or fallback_px, now=at,
            )  # fmt: skip
        except DataError as exc:
            return gates.Refusal("REPLACE_FAILED", str(exc))
        return None
