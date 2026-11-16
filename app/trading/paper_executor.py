"""Paper execution treated exactly like real money (user, 2026-11-13).

- The order is checked against KuCoin's contract rules (`executor.check_rules`) and refused the same way.
- After PAPER_FILL_DELAY_S (an order's round trip) a FRESH depth20 book is read and walked level by level; a thin
  book or slippage above MAX_SLIPPAGE_BPS refuses the order. The fee is the contract's own taker rate.
- A close walks a fresh book too and never refuses (`futures_paper.exit_fill`).
- Stops live in the bot (risk loop, by mark price). There is no exchange to hold them, so `place_stop` returns "".
"""

import asyncio
from collections.abc import Awaitable, Callable

from app.config import settings
from app.data.http import DataError
from app.data.kucoin import Contract, KucoinFutures, OrderBook
from app.trading import futures_paper as fp
from app.trading.executor import ExecFill, check_rules
from app.trading.ledger import Position
from app.utils.logging import get_logger

logger = get_logger(__name__)


class PaperExecutor:
    def __init__(
        self,
        kf: KucoinFutures,
        *,
        delay_s: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.kf = kf
        self.delay_s = settings.paper_fill_delay_s if delay_s is None else delay_s
        self._sleep = sleep

    async def _fresh_book(self, symbol: str) -> OrderBook:
        if self.delay_s > 0:
            await self._sleep(self.delay_s)
        return await self.kf.book(symbol)

    async def open(self, contract: Contract, *, side: str, contracts: float, leverage: int) -> ExecFill:
        check_rules(contract, contracts=contracts, leverage=leverage, price=contract.mark_price or 0.0)
        try:
            book = await self._fresh_book(contract.symbol)
        except DataError as exc:
            raise fp.FillRefused("NO_BOOK", f"{contract.symbol}: {exc}") from exc
        fill = fp.walk(book, buy=side == fp.LONG, size=contracts, max_slippage_bps=settings.max_slippage_bps)
        # The rules again at the real fill price: the risk-limit check depends on price.
        check_rules(contract, contracts=contracts, leverage=leverage, price=fill.avg_px)
        fee = contracts * contract.multiplier * fill.avg_px * contract.taker_fee
        return ExecFill(fill.avg_px, contracts, fee)

    async def close(self, pos: Position, contract: Contract | None, *, reason: str, fallback_px: float) -> ExecFill:
        taker = contract.taker_fee if contract else 0.0006
        if reason == fp.LIQUIDATED:
            # The exchange takes the position at the liquidation price; no fill of ours.
            px = pos.liq_px or fallback_px
            return ExecFill(px, pos.contracts, 0.0)
        px = fallback_px
        try:
            book = await self._fresh_book(pos.symbol)
            px = fp.exit_fill(book, buy=pos.side == fp.SHORT, size=pos.contracts, fallback_px=fallback_px)
        except DataError as exc:
            logger.warning("paper: no book for %s exit, using the mark: %s", pos.symbol, exc)
        return ExecFill(px, pos.contracts, pos.qty * px * taker)

    async def place_stop(self, pos: Position, *, stop_px: float) -> str:
        return ""

    async def cancel_stop(self, pos: Position) -> None:
        return None
