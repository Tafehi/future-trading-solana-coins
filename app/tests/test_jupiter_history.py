"""Item 1: Jupiter history from the chain. Decoder on REAL mainnet transactions, episodes, the backfill job."""

from typing import Any

import pytest

from app.config import settings
from app.core import wallet_grader as wg
from app.data import jupiter_events as je
from app.db import store
from app.jobs import jupiter, jupiter_history
from app.tests.conftest import fixture

PROG = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
T = 1_800_000_000.0


def test_event_tag_is_the_little_endian_anchor_constant() -> None:
    """DEBUG D-5: the bytes on the wire are e4 45 a5 2e 51 cb 9a 1d, the hash prefix reversed."""
    assert je.EVENT_IX.hex() == "e445a52e51cb9a1d"


def test_decode_a_real_close() -> None:
    """Values read off mainnet 2026-11-13: a $12,700 SOL short closed at 81.46, PnL matching the price move."""
    (ev,) = je.events_from_transaction(fixture("jupiter_close_tx.json")["tx"], PROG)
    assert ev.name == "InstantDecreasePositionEvent" and ev.closes
    assert (ev.owner, ev.position_key) == (
        "ahJ7Cy2pYph5U2y3URNc4bUE8hDgYCpcELFLRcWjoUb",
        "6Q7MMCA6ymhnV12XPMbkDQPdLowtSMmGKy9CiBj5geGM",
    )
    assert (ev.coin, ev.side) == ("SOL", "short")
    assert ev.size_delta_usd == pytest.approx(12_700.8501) and ev.size_after_usd == 0
    assert ev.pnl_usd == pytest.approx(90.363016) and ev.fee_usd == pytest.approx(8.920774)
    assert ev.price == pytest.approx(81.460385) and ev.position_price == pytest.approx(82.044106)
    # The PnL is the price move on the size: the layout is right, not just plausible.
    move = ev.size_delta_usd * (ev.position_price - ev.price) / ev.position_price
    assert ev.pnl_usd == pytest.approx(move, abs=0.05)


def test_more_real_transactions() -> None:
    eth, sol_swap, failed = fixture("jupiter_more_txs.json")["txs"]
    (e1,) = je.events_from_transaction(eth, PROG)
    assert (e1.coin, e1.side) == ("ETH", "short") and e1.pnl_usd == pytest.approx(61.76, abs=0.01)
    evs = je.events_from_transaction(sol_swap, PROG)
    assert [(e.coin, e.side) for e in evs] == [("SOL", "long")]  # the swap event next to it is ignored
    assert je.events_from_transaction(failed, PROG) == []  # a failed transaction carries no event


def ev(key: str, opened: int, t: int, *, after: float, delta: float, pnl: float, fee: float = 1.0,
       name: str = "InstantDecreasePositionEvent", owner: str = "O") -> je.CloseEvent:  # fmt: skip
    return je.CloseEvent(name, f"s{t}", t, key, owner, "SOL", "long", after, delta, pnl, fee, 110.0, opened, 100.0)


def test_episodes_need_a_full_close_and_sum_partial_decreases() -> None:
    events = [
        ev("A", 1000, 2000, after=5000, delta=5000, pnl=50),  # half closed
        ev("A", 1000, 3000, after=0, delta=5000, pnl=70),  # rest closed -> one episode
        ev("A", 4000, 5000, after=2000, delta=1000, pnl=10),  # a later lifetime, still open -> no episode
        ev("B", 1500, 2500, after=0, delta=800, pnl=-40, name=je.LIQUIDATION),
    ]
    eps = je.episodes(events)
    assert [(e.position_key, e.opened_at, e.closed_at) for e in eps] == [("B", 1500, 2500), ("A", 1000, 3000)]
    a = eps[1]
    assert a.size_usd == 10_000 and a.pnl == pytest.approx(50 + 70 - 2)


class FakeRpc:
    """Position accounts, signature pages and transactions, all from memory. Counts every call."""

    def __init__(self, sigs: dict[str, list[dict[str, Any]]], txs: dict[str, Any]) -> None:
        self.sigs, self.txs, self.calls = sigs, txs, 0

    async def owner_position_keys(self, owner: str) -> list[str]:
        self.calls += 1
        return list(self.sigs)

    async def signatures(self, address: str, *, before: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        self.calls += 1
        rows = self.sigs[address]
        start = 0 if before is None else next(i for i, r in enumerate(rows) if r["signature"] == before) + 1
        return rows[start : start + limit]

    async def transaction(self, signature: str) -> Any:
        self.calls += 1
        return self.txs[signature]


def _real_setup() -> tuple[FakeRpc, str]:
    tx = fixture("jupiter_close_tx.json")["tx"]
    owner = "ahJ7Cy2pYph5U2y3URNc4bUE8hDgYCpcELFLRcWjoUb"
    when = tx["blockTime"]
    sigs = {
        "6Q7MMCA6ymhnV12XPMbkDQPdLowtSMmGKy9CiBj5geGM": [
            {"signature": "close", "blockTime": when, "err": None},
            {"signature": "failed", "blockTime": when - 60, "err": {"InstructionError": [5, {"Custom": 3012}]}},
            {"signature": "too-old", "blockTime": when - 400 * 86400, "err": None},
        ]
    }
    return FakeRpc(sigs, {"close": tx}), owner


async def test_backfill_stores_chain_episodes_and_replaces_feed_ones(fresh_db: str) -> None:
    rpc, owner = _real_setup()
    now = float(fixture("jupiter_close_tx.json")["tx"]["blockTime"]) + 60
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES (?, ?, 50000)", (owner, now)
        )
        await conn.execute(
            "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl) "
            "VALUES (?, 'SOL', 'short', 12700, 82, 81.5, 1, ?, 85)",
            (owner, now - 10),
        )
    assert await jupiter_history.next_owners(5) == [owner]
    n = await jupiter_history.backfill_owner(rpc, owner, jupiter_history.Budget(now=now), now=now)  # type: ignore[arg-type]
    assert n == 1
    assert rpc.calls == 1 + 1 + 1  # owner accounts, one signature page, one transaction (failed and too old skipped)
    async with store.session() as conn:
        async with conn.execute("SELECT origin, pnl FROM jup_episodes WHERE owner = ?", (owner,)) as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        async with conn.execute("SELECT backfilled_at, history_from FROM jup_owners WHERE owner = ?", (owner,)) as cur:
            o = await cur.fetchone()
    assert rows == [("chain", pytest.approx(90.363016 - 8.920774))]  # the feed estimate is replaced
    assert o is not None and o["backfilled_at"] == now
    assert o["history_from"] == pytest.approx(now - settings.jupiter_history_days * 86400)  # the whole window read
    assert await jupiter_history.next_owners(5) == []


async def test_out_of_budget_writes_nothing(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    rpc, owner = _real_setup()
    monkeypatch.setattr(settings, "jupiter_history_calls_per_day", 2)
    async with store.session() as conn:
        await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES (?, ?, 50000)", (owner, T))
    with pytest.raises(jupiter_history.OutOfBudget):
        await jupiter_history.backfill_owner(rpc, owner, jupiter_history.Budget(now=T), now=T)  # type: ignore[arg-type]
    assert rpc.calls == 2 and await jupiter_history.calls_today(now=T) == 2
    assert await jupiter_history.next_owners(5) == [owner]  # still to do


async def test_a_backfilled_owner_is_graded_on_day_one(fresh_db: str) -> None:
    """First seen today, but 90 days of chain history: graded now, not after JUPITER_MIN_RECORD_DAYS."""
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO jup_owners (owner, first_seen, history_from, backfilled_at, max_open_usd) "
            "VALUES ('W', ?, ?, ?, 20000)",
            (T, T - 90 * 86400, T),
        )
        for i in range(20):
            await conn.execute(
                "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl, "
                "origin, position_key) VALUES ('W', 'SOL', 'long', 10000, 100, 103, ?, ?, ?, 'chain', ?)",
                (T - (80 - i * 3) * 86400, T - (80 - i * 3) * 86400 + 7200, 300.0 if i % 4 else -100.0, f"k{i}"),
            )
    await jupiter.grade_owners(now=T)
    async with store.session() as conn:
        async with conn.execute("SELECT tier, history_days FROM wallets WHERE address = 'W'") as cur:
            w = await cur.fetchone()
    assert w is not None and w["tier"] == wg.GOLD and w["history_days"] == pytest.approx(90.0)
