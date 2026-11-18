"""One interface for paper and live order execution (DECISIONS D-3).

Gates, sizing, exits and the ledger are identical in both modes. Only the class behind `Executor` changes:
- `PaperExecutor` (`paper_executor.py`): checks KuCoin's own contract rules, waits PAPER_FILL_DELAY_S, reads a FRESH
  depth20 book and walks it with the contract's taker fee.
- `KucoinExecutor` (`kucoin_executor.py`): real signed orders; prices and fees come from KuCoin's fills.

`check_rules` is the same pure check for both: what KuCoin would reject, paper rejects the same way.
"""

import math
from dataclasses import dataclass
from typing import Protocol

from app.data.kucoin import Contract
from app.trading.futures_paper import FillRefused
from app.trading.ledger import Position


@dataclass(frozen=True)
class ExecFill:
    avg_px: float
    contracts: float
    fee: float
    order_id: str = ""


class Executor(Protocol):
    async def open(self, contract: Contract, *, side: str, contracts: float, leverage: int) -> ExecFill:
        """Open a position. Raises `FillRefused` when the order cannot be placed or filled."""
        ...

    async def close(self, pos: Position, contract: Contract | None, *, reason: str, fallback_px: float) -> ExecFill:
        """Close a whole position. Must not refuse: an exit always happens."""
        ...

    async def place_stop(self, pos: Position, *, stop_px: float) -> str:
        """Place a reduce-only stop on the exchange. Returns its order id ("" in paper)."""
        ...

    async def cancel_stop(self, pos: Position) -> None: ...


def check_rules(contract: Contract, *, contracts: float, leverage: int, price: float) -> None:
    """Raise `FillRefused` for an order KuCoin would refuse. Pure."""
    if contract.status != "Open":
        raise FillRefused("NOT_OPEN", f"{contract.symbol} status {contract.status}")
    lot = contract.lot_size if contract.lot_size > 0 else 1.0
    if contracts < lot - 1e-12:
        raise FillRefused("SIZE_ZERO", f"{contracts:g} lots is below the minimum of {lot:g}")
    if abs(contracts / lot - round(contracts / lot)) > 1e-9 or not math.isfinite(contracts):
        raise FillRefused("BAD_LOT", f"{contracts:g} is not a whole number of {lot:g}-lots")
    if contract.max_order_qty > 0 and contracts > contract.max_order_qty:
        raise FillRefused("MAX_ORDER_QTY", f"{contracts:g} lots > max {contract.max_order_qty:g}")
    if leverage > contract.max_leverage:
        raise FillRefused("MAX_LEVERAGE", f"{leverage}x > max {contract.max_leverage:g}x")
    notional = contracts * contract.multiplier * price
    if contract.max_risk_limit > 0 and notional > contract.max_risk_limit:
        raise FillRefused("RISK_LIMIT", f"${notional:,.0f} > first risk-limit level ${contract.max_risk_limit:,.0f}")
