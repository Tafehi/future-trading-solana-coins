"""Builders shared by tests: contracts, books and fake clients (ported from ai-kucoin-future, extended)."""

import base64
import json
import sqlite3
import struct
from contextlib import closing
from pathlib import Path
from typing import Any

from app.config import settings
from app.data.jupiter_perps import SOURCE
from app.data.kucoin import Contract, OrderBook
from app.db import crossread
from app.db.crossread import Entry
from app.trading.copylist import CopyList

_FIXTURES = Path(__file__).with_name("fixtures")


def position_account(owner_byte: int, *, size_usd: float = 5000.0, price: float = 150.0, opened: int = 1_800_000_000,
                     updated: int | None = None) -> bytes:  # fmt: skip
    """A whole 216-byte Jupiter Position account (SOL long): a real mainnet account from the fixtures with its owner
    (32 x `owner_byte`), times, price and size rewritten, so no real address is typed into a test."""
    template = base64.b64decode(json.loads((_FIXTURES / "jupiter_positions.json").read_text())["accounts"][0]["data"])
    raw = bytearray(template)
    raw[8:40] = bytes([owner_byte]) * 32
    struct.pack_into("<qq", raw, 136, opened, opened if updated is None else updated)
    struct.pack_into("<QQ", raw, 153, int(price * 1_000_000), int(size_usd * 1_000_000))
    return bytes(raw)


class TableCopyList(CopyList):
    """Test stand-in for the trader's copy list: every get() reads the test database's tracked / watched `wallets`
    rows, so a test can add a wallet at any point, and the list is always fresh. The real list is the scout's pick,
    read once a minute (`crossread.copy_list`); its age rule (LIST_STALE) is tested in test_copylist.py."""

    def age(self, now: float) -> float | None:
        return 0.0

    def stale(self, now: float) -> bool:
        return False

    def get(self, address: str) -> Entry | None:
        with closing(sqlite3.connect(settings.db_path)) as c:
            c.row_factory = sqlite3.Row
            r = c.execute(
                f"SELECT {crossread.ENTRY_COLUMNS} FROM wallets WHERE address = ? AND source = ? "  # noqa: S608
                "AND (tracked = 1 OR watched = 1)",
                (address, SOURCE),
            ).fetchone()
        return None if r is None else crossread.entry_of(r)


def contract(
    symbol: str = "ETHUSDTM",
    *,
    base: str = "ETH",
    multiplier: float = 0.01,
    lot: float = 1.0,
    taker: float = 0.0006,
    mmr: float = 0.005,
    funding: float = 0.0001,
    turnover: float = 50_000_000.0,
    interval_ms: int = 28_800_000,
    max_leverage: float = 100,
    max_order_qty: float = 1_000_000,
    max_risk_limit: float = 500_000,
    mark: float = 2000.0,
) -> Contract:
    return Contract(
        symbol=symbol, base=base, multiplier=multiplier, lot_size=lot, tick_size=0.01, max_leverage=max_leverage,
        taker_fee=taker, maker_fee=0.0002, mmr=mmr, funding_rate=funding, funding_interval_ms=interval_ms,
        next_funding_ms=0, mark_price=mark, turnover_24h=turnover, status="Open", max_order_qty=max_order_qty,
        max_risk_limit=max_risk_limit,
    )  # fmt: skip


def book(
    mid: float = 2000.0, *, spread: float = 0.2, depth: float = 1_000_000.0, symbol: str = "ETHUSDTM"
) -> OrderBook:
    """A deep, tight book: 20 levels each side, `depth` units per level."""
    half = spread / 2
    bids = [(mid - half - i * 0.1, depth) for i in range(20)]
    asks = [(mid + half + i * 0.1, depth) for i in range(20)]
    return OrderBook(symbol, bids, asks)


class FakeKucoin:
    """Stands in for KucoinFutures. Every call is recorded."""

    def __init__(self, *, books: dict[str, OrderBook] | None = None, marks: dict[str, float] | None = None):
        self.books = books or {}
        self.marks = marks or {}
        self.funding: dict[str, list[tuple[int, float]]] = {}
        self.calls: list[tuple[str, Any]] = []

    async def book(self, symbol: str) -> OrderBook:
        self.calls.append(("book", symbol))
        return self.books[symbol]

    async def mark_price(self, symbol: str) -> float:
        self.calls.append(("mark", symbol))
        return self.marks[symbol]

    async def funding_history(self, symbol: str, *, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        self.calls.append(("funding", symbol))
        return [(t, r) for t, r in self.funding.get(symbol, []) if start_ms <= t <= end_ms]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.enabled = True

    async def send(self, message: str, *, parse_mode: str = "HTML") -> bool:
        self.sent.append(message)
        return True

    async def send_blocks(self, blocks: list[str]) -> bool:
        self.sent.append("\n\n".join(blocks))
        return True

    async def close(self) -> None:
        return None
