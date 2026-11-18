"""Live execution on KuCoin Futures (MODE=live only; the user flips it after `golive_check` passes).

- `open`: the same `check_rules` as paper, then a market order (isolated, LEVERAGE). The price and fee written to the
  ledger are KuCoin's own fills, never our estimate.
- `close`: a reduce-only market order for the whole size. Retried; if it still fails the caller alarms.
- `place_stop`: every live position gets a reduce-only stop-market on KuCoin, triggered by MARK price at the same
  STOP_LOSS_PRICE_PCT the bot uses. If the bot dies, the exchange still stops the loss.

Fills are read with `GET /api/v1/fills?orderId=` until they cover the order size (a market order fills at once on
a liquid contract; FILL_WAIT_S bounds the wait).
"""

import asyncio
import uuid
from typing import Any

from app.data.http import DataError
from app.data.kucoin import Contract
from app.data.kucoin_private import KucoinPrivate
from app.trading import futures_paper as fp
from app.trading.executor import ExecFill, check_rules
from app.trading.ledger import Position
from app.utils.logging import get_logger

logger = get_logger(__name__)

FILL_WAIT_S = 10.0
FILL_POLL_S = 0.5


def _oid() -> str:
    return uuid.uuid4().hex


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def sum_fills(rows: list[dict[str, Any]], multiplier: float) -> tuple[float, float, float]:
    """(lots, average price, fee) from KuCoin fill rows. Pure."""
    lots = sum(_num(r, "size") for r in rows)
    value = sum(_num(r, "value") for r in rows)
    fee = sum(_num(r, "fee") for r in rows)
    avg = value / (lots * multiplier) if lots > 0 and multiplier > 0 else 0.0
    return lots, avg, fee


class KucoinExecutor:
    def __init__(self, client: KucoinPrivate) -> None:
        self.client = client
        self._isolated: set[str] = set()

    async def _ensure_isolated(self, symbol: str) -> None:
        if symbol in self._isolated:
            return
        if (await self.client.margin_mode(symbol)).upper() != "ISOLATED":
            await self.client.set_isolated(symbol)
        self._isolated.add(symbol)

    async def _filled(self, order_id: str, *, want: float, multiplier: float) -> ExecFill:
        waited = 0.0
        lots = avg = fee = 0.0
        while waited <= FILL_WAIT_S:
            lots, avg, fee = sum_fills(await self.client.fills(order_id), multiplier)
            if lots >= want - 1e-9:
                break
            await asyncio.sleep(FILL_POLL_S)
            waited += FILL_POLL_S
        if lots <= 0:
            raise fp.FillRefused("NOT_FILLED", f"order {order_id}: no fill after {FILL_WAIT_S:g}s")
        return ExecFill(avg, lots, fee, order_id)

    async def open(self, contract: Contract, *, side: str, contracts: float, leverage: int) -> ExecFill:
        check_rules(contract, contracts=contracts, leverage=leverage, price=contract.mark_price or 0.0)
        try:
            await self._ensure_isolated(contract.symbol)
            order_id = await self.client.market_order(
                client_oid=_oid(),
                symbol=contract.symbol,
                side="buy" if side == fp.LONG else "sell",
                size=int(round(contracts)),
                leverage=leverage,
            )
        except DataError as exc:
            raise fp.FillRefused("EXCHANGE_REFUSED", str(exc)[:200]) from exc
        return await self._filled(order_id, want=contracts, multiplier=contract.multiplier)

    async def close(self, pos: Position, contract: Contract | None, *, reason: str, fallback_px: float) -> ExecFill:
        last: Exception | None = None
        for attempt in range(3):
            try:
                order_id = await self.client.market_order(
                    client_oid=_oid(),
                    symbol=pos.symbol,
                    side="sell" if pos.side == fp.LONG else "buy",
                    size=int(round(pos.contracts)),
                    leverage=int(pos.leverage),
                    reduce_only=True,
                )
                return await self._filled(order_id, want=pos.contracts, multiplier=pos.multiplier)
            except (DataError, fp.FillRefused) as exc:
                last = exc
                logger.error("live: close %s attempt %d failed: %s", pos.symbol, attempt + 1, exc)
                await asyncio.sleep(1.0 + attempt)
        raise DataError(f"close {pos.symbol} failed 3 times: {last}")

    async def place_stop(self, pos: Position, *, stop_px: float) -> str:
        long = pos.side == fp.LONG
        return await self.client.stop_order(
            client_oid=_oid(),
            symbol=pos.symbol,
            side="sell" if long else "buy",
            stop="down" if long else "up",
            stop_px=stop_px,
        )

    async def cancel_stop(self, pos: Position) -> None:
        if pos.stop_order_id:
            try:
                await self.client.cancel(pos.stop_order_id)
            except DataError as exc:
                # Already triggered or gone: nothing left to cancel.
                logger.info("live: cancel stop %s for %s: %s", pos.stop_order_id, pos.symbol, exc)
