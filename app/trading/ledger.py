"""The money record of the account. All money moves through `ledger` rows; `positions` is the second record that
`reconcile` checks it against. Ported from ai-kucoin-future `app/trading/ledger.py` (one account instead of a
book matrix; spot legs removed).

Ledger kinds (amount signed from the account's cash):
- DEPOSIT  +start balance, once per account (`seed`).
- OPEN     -margin locked by a new position.
- CLOSE    +margin returned plus gross PnL. Isolated margin: a position can lose at most its margin, so the
           return is floored at 0 and `gross_pnl` is recorded as `returned - margin`.
- FEE      -taker fee, at open and at close.
- FUNDING  +/- funding settled on an open position.

The identity, checked by `reconcile`:
    cash + open margin == deposits + sum(closed gross) - sum(all fees) + sum(all funding)
A drift over DRIFT_TOLERANCE halts the account (`account_state.halted`).

Paper and live write the SAME rows (DECISIONS D-3). In live, the prices and fees are the exchange's own fills.
"""

import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.trading import futures_paper as fp
from app.utils.logging import get_logger

logger = get_logger(__name__)

DRIFT_TOLERANCE = 0.01

OPEN = "OPEN"
CLOSED = "CLOSED"


class InsufficientCash(Exception):
    pass


@dataclass(frozen=True)
class Position:
    id: int
    account: str
    symbol: str
    coin: str
    side: str
    contracts: float
    multiplier: float
    qty: float
    entry_px: float
    leverage: float
    margin: float
    mmr: float
    liq_px: float | None
    entry_fee: float
    funding: float
    best_px: float
    last_mark: float | None
    last_funding_ts: float
    opened_at: float
    status: str
    signal_id: int | None
    wallet: str
    source: str
    tier: str
    copy_weight: int
    score: float
    stop_order_id: str

    @classmethod
    def from_row(cls, row: Any) -> "Position":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    def unrealized(self, mark: float) -> float:
        """Floored at -margin: an isolated position cannot lose more than it locked."""
        return max(-self.margin, fp.unrealized(self.side, self.qty, self.entry_px, mark))


async def seed(conn: aiosqlite.Connection, account: str, balance: float, *, now: float | None = None) -> bool:
    """Create the account with its one DEPOSIT row. Idempotent: True only the first time."""
    at = time.time() if now is None else now
    async with conn.execute("SELECT 1 FROM account_state WHERE account = ?", (account,)) as cur:
        if await cur.fetchone():
            return False
    await conn.execute(
        "INSERT INTO account_state (account, created_at, peak_equity) VALUES (?, ?, ?)", (account, at, balance)
    )
    await conn.execute(
        "INSERT INTO ledger (account, ts, kind, amount, note) VALUES (?, ?, 'DEPOSIT', ?, 'start balance')",
        (account, at, balance),
    )
    return True


async def cash(conn: aiosqlite.Connection, account: str) -> float:
    async with conn.execute("SELECT COALESCE(SUM(amount), 0) AS c FROM ledger WHERE account = ?", (account,)) as cur:
        row = await cur.fetchone()
    return float(row["c"]) if row else 0.0


async def deposits(conn: aiosqlite.Connection, account: str) -> float:
    async with conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS d FROM ledger WHERE account = ? AND kind = 'DEPOSIT'", (account,)
    ) as cur:
        row = await cur.fetchone()
    return float(row["d"]) if row else 0.0


async def open_positions(conn: aiosqlite.Connection, account: str) -> list[Position]:
    async with conn.execute(
        "SELECT * FROM positions WHERE status = 'OPEN' AND account = ? ORDER BY id", (account,)
    ) as cur:
        return [Position.from_row(r) for r in await cur.fetchall()]


async def get_position(conn: aiosqlite.Connection, pid: int) -> Position | None:
    async with conn.execute("SELECT * FROM positions WHERE id = ?", (pid,)) as cur:
        row = await cur.fetchone()
    return Position.from_row(row) if row else None


async def open_position(
    conn: aiosqlite.Connection,
    *,
    account: str,
    symbol: str,
    coin: str,
    side: str,
    contracts: float,
    multiplier: float,
    entry_px: float,
    leverage: float,
    mmr: float,
    fee: float,
    now: float,
    signal_id: int | None = None,
    wallet: str = "",
    source: str = "",
    tier: str = "",
    copy_weight: int = 0,
    score: float = 0.0,
    order_id: str = "",
) -> int:
    """Lock margin and pay the entry fee. Raises InsufficientCash when the account cannot pay both."""
    qty = contracts * multiplier
    notional = qty * entry_px
    margin = notional / leverage
    available = await cash(conn, account)
    if margin + fee > available + 1e-9:
        raise InsufficientCash(f"{account}: needs {margin + fee:.2f}, has {available:.2f}")
    liq = fp.liquidation_price(side, entry_px, leverage, mmr)
    cur = await conn.execute(
        "INSERT INTO positions (account, symbol, coin, side, contracts, multiplier, qty, entry_px, leverage, margin, "
        "mmr, liq_px, entry_fee, best_px, last_mark, last_funding_ts, opened_at, signal_id, wallet, source, tier, "
        "copy_weight, score, entry_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?)",
        (
            account, symbol, coin, side, contracts, multiplier, qty, entry_px, leverage, margin,
            mmr, liq, fee, entry_px, entry_px, now, now, signal_id, wallet, source, tier,
            copy_weight, score, order_id,
        ),
    )  # fmt: skip
    pid = int(cur.lastrowid or 0)
    await conn.execute(
        "INSERT INTO ledger (account, ts, kind, amount, position_id, note) VALUES (?, ?, 'OPEN', ?, ?, ?)",
        (account, now, -margin, pid, f"{side} {symbol}"),
    )
    await conn.execute(
        "INSERT INTO ledger (account, ts, kind, amount, position_id, note) VALUES (?, ?, 'FEE', ?, ?, 'entry')",
        (account, now, -fee, pid),
    )
    return pid


async def close_position(
    conn: aiosqlite.Connection, pos: Position, *, exit_px: float, fee: float, reason: str, now: float
) -> float:
    """Release the margin with its PnL and pay the exit fee. Returns the position's net PnL."""
    gross = -pos.margin if reason == fp.LIQUIDATED else pos.unrealized(exit_px)
    returned = max(0.0, pos.margin + gross)
    gross = returned - pos.margin
    net = gross - pos.entry_fee - fee + pos.funding
    await conn.execute(
        "UPDATE positions SET status = 'CLOSED', exit_px = ?, exit_fee = ?, gross_pnl = ?, net_pnl = ?, "
        "closed_at = ?, reason = ?, last_mark = ? WHERE id = ? AND status = 'OPEN'",
        (exit_px, fee, gross, net, now, reason, exit_px, pos.id),
    )
    await conn.execute(
        "INSERT INTO ledger (account, ts, kind, amount, position_id, note) VALUES (?, ?, 'CLOSE', ?, ?, ?)",
        (pos.account, now, returned, pos.id, reason),
    )
    if fee:
        await conn.execute(
            "INSERT INTO ledger (account, ts, kind, amount, position_id, note) VALUES (?, ?, 'FEE', ?, ?, 'exit')",
            (pos.account, now, -fee, pos.id),
        )
    return net


async def apply_funding(conn: aiosqlite.Connection, pos: Position, *, amount: float, funding_ts: float) -> None:
    await conn.execute(
        "UPDATE positions SET funding = funding + ?, last_funding_ts = ? WHERE id = ?",
        (amount, funding_ts, pos.id),
    )
    await conn.execute(
        "INSERT INTO ledger (account, ts, kind, amount, position_id, note) VALUES (?, ?, 'FUNDING', ?, ?, ?)",
        (pos.account, funding_ts, amount, pos.id, pos.symbol),
    )


async def mark(conn: aiosqlite.Connection, pos: Position, price: float) -> None:
    best = fp.update_best(pos.side, pos.best_px, price)
    await conn.execute("UPDATE positions SET last_mark = ?, best_px = ? WHERE id = ?", (price, best, pos.id))


async def set_stop_order(conn: aiosqlite.Connection, pid: int, order_id: str) -> None:
    await conn.execute("UPDATE positions SET stop_order_id = ? WHERE id = ?", (order_id, pid))


async def reconcile(conn: aiosqlite.Connection, account: str) -> float:
    """Signed drift of the identity in the module docstring. 0 means the two records agree."""
    left = await cash(conn, account)
    async with conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN status = 'OPEN' THEN margin ELSE 0 END), 0) AS open_margin, "
        "COALESCE(SUM(CASE WHEN status != 'OPEN' THEN gross_pnl ELSE 0 END), 0) AS gross, "
        "COALESCE(SUM(entry_fee + exit_fee), 0) AS fees, COALESCE(SUM(funding), 0) AS funding "
        "FROM positions WHERE account = ?",
        (account,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    left += float(row["open_margin"])
    right = await deposits(conn, account) + float(row["gross"]) - float(row["fees"]) + float(row["funding"])
    return left - right


async def equity(conn: aiosqlite.Connection, account: str) -> float:
    """Cash plus every open position's margin and unrealized PnL at its last mark."""
    total = await cash(conn, account)
    for pos in await open_positions(conn, account):
        price = pos.last_mark if pos.last_mark else pos.entry_px
        total += pos.margin + pos.unrealized(price)
    return total


async def halt(conn: aiosqlite.Connection, account: str, reason: str) -> None:
    await conn.execute("UPDATE account_state SET halted = 1, halt_reason = ? WHERE account = ?", (reason, account))
    logger.error("ledger: account %s halted: %s", account, reason)


async def state(conn: aiosqlite.Connection, account: str) -> dict[str, Any]:
    async with conn.execute("SELECT * FROM account_state WHERE account = ?", (account,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}
