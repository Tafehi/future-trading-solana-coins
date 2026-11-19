"""The copy list (D-30): the trader's read-only view of the scout's picks, its age rule, and what the copier does
with it. Age boundary at the value: exactly COPYLIST_MAX_AGE_S is fresh."""

import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.core import wallet_grader as wg
from app.data.market import MarketCache
from app.db import crossread, meta, migrations, store
from app.db.crossread import CopyListRead, Entry
from app.tests.helpers import FakeKucoin, book, contract
from app.trading import futures_paper as fp
from app.trading import ledger, risk
from app.trading.copier import Copier, stop_price
from app.trading.copylist import CopyList
from app.trading.paper_executor import PaperExecutor

NOW = 1_800_000_000.0
W = "WALLET-1"


def entry(address: str = W, *, tier: str = wg.GOLD, tracked: bool = True, rank: float = 5.0) -> Entry:
    return Entry(address, tier, tracked, 10, 1_000_000.0, 8.0, rank)


def picked(*entries: Entry, at: float | None = NOW) -> CopyList:
    cl = CopyList()
    cl.replace(CopyListRead(list(entries), at, at), now=NOW)
    return cl


# ── read-only cross reads ─────────────────────────────────────────────────────────────────


async def test_a_missing_scout_file_raises_and_is_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "scout.db"
    with pytest.raises(sqlite3.OperationalError):
        await crossread.copy_list(str(missing))
    assert not missing.exists()


async def test_a_read_only_session_refuses_to_write(tmp_path: Path) -> None:
    path = str(tmp_path / "scout.db")
    await migrations.migrate(path)
    async with store.session(path, read_only=True) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await conn.execute("INSERT INTO meta (key, value, updated_at) VALUES ('x', '1', 0)")


async def test_the_copy_list_is_tracked_and_watched_with_the_pick_time(tmp_path: Path) -> None:
    path = str(tmp_path / "scout.db")
    await migrations.migrate(path)
    async with store.session(path) as conn:
        for address, tracked, watched in (("T", 1, 0), ("V", 0, 1), ("OFF", 0, 0)):
            await conn.execute(
                "INSERT INTO wallets (address, source, tier, tracked, watched, copy_weight, score, account_value, "
                "count_score) VALUES (?, 'jupiter', 'GOLD', ?, ?, 7, 3.5, 25000, 9.0)",
                (address, tracked, watched),
            )
        await meta.put(conn, crossread.PICKED_AT_KEY, NOW - 30)
        await meta.put(conn, crossread.LAST_POLL_KEY, NOW - 10)
    read = await crossread.copy_list(path)
    assert sorted((e.address, e.tracked) for e in read.entries) == [("T", True), ("V", False)]
    assert read.entries[0].copy_weight == 7 and read.entries[0].rank_score == 9.0
    assert (read.picked_at, read.scout_fed_at) == (NOW - 30, NOW - 10)


async def test_owner_keys_are_open_positions_and_history_accounts(tmp_path: Path) -> None:
    path = str(tmp_path / "scout.db")
    await migrations.migrate(path)
    async with store.session(path) as conn:
        await conn.execute(
            "INSERT INTO jup_positions (pubkey, owner, coin, side, size_usd, entry_px, open_time, seen_at) "
            "VALUES ('OPEN-KEY', 'O', 'SOL', 'long', 5000, 150, 1, 1)"
        )
        for key in ("HIST-KEY", None):
            await conn.execute(
                "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl, "
                "origin, position_key) VALUES ('O', 'SOL', 'long', 5000, 150, 160, 1, 2, 10, 'api', ?)",
                (key,),
            )
    assert await crossread.owner_keys(path, ["O", "NOBODY"]) == {"O": {"OPEN-KEY", "HIST-KEY"}, "NOBODY": set()}


# ── the list in memory ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("picked_at", "stale"), [(NOW - 600.0, False), (NOW - 600.1, True), (None, True)])
def test_the_list_age_at_its_limit(picked_at: float | None, stale: bool) -> None:
    assert settings.copylist_max_age_s == 600.0
    assert picked(entry(), at=picked_at).stale(NOW) is stale


def test_replace_reports_the_change_and_ranks_tracked_first() -> None:
    cl = picked(entry("A", rank=1.0), entry("B", tracked=False, rank=99.0))
    added, dropped = cl.replace(CopyListRead([entry("B", tracked=False, rank=99.0), entry("C", rank=2.0)], NOW, NOW),
                                now=NOW)  # fmt: skip
    assert (added, dropped) == ({"C"}, {"A"})
    assert cl.owners_ranked() == ["C", "B"] and cl.tracked == 1


# ── what the copier does with it ──────────────────────────────────────────────────────────


def setup(cl: CopyList) -> tuple[FakeKucoin, MarketCache, Copier]:
    kf = FakeKucoin(books={"ETHUSDTM": book(2000.0)}, marks={"ETHUSDTM": 2000.0})
    market = MarketCache()
    market.load([contract(mark=2000.0)])
    return kf, market, Copier(kf, market, PaperExecutor(kf, delay_s=0), cl)  # type: ignore[arg-type]


async def add_signal(wallet: str = W, *, ts: float = NOW) -> int:
    async with store.session() as conn:
        cur = await conn.execute(
            "INSERT INTO signals (ts, wallet, source, coin, kind, side, tier, wallet_size, wallet_entry_px) "
            "VALUES (?, ?, 'jupiter', 'ETH', 'OPEN', 'long', 'GOLD', 50000, 2000)",
            (ts, wallet),
        )
        return int(cur.lastrowid or 0)


async def refused(sid: int) -> str:
    async with store.session() as conn:
        async with conn.execute("SELECT refused FROM signals WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    return str(row["refused"])


@pytest.mark.parametrize(("cl", "code"), [
    (picked(entry(), at=NOW - 601), "LIST_STALE"),  # comes before NOT_TRACKED: an old list proves nothing
    (picked(entry(), at=None), "LIST_STALE"),
    (picked(entry("SOMEONE-ELSE")), "NOT_TRACKED"),
    (picked(entry(tier=wg.BRONZE)), "NOT_TRACKED"),
    (picked(entry(tracked=False)), "SHADOW_ONLY"),
])  # fmt: skip
async def test_the_copier_refuses_by_the_list(fresh_db: str, cl: CopyList, code: str) -> None:
    sid = await add_signal()
    _, _, copier = setup(cl)
    assert await copier.handle_pending(now=NOW + 1) == []
    assert await refused(sid) == code


async def test_a_wallet_that_left_the_list_keeps_its_exits(fresh_db: str) -> None:
    cl = picked(entry())
    kf, market, copier = setup(cl)
    await add_signal()
    assert len(await copier.handle_pending(now=NOW + 1)) == 1
    cl.replace(CopyListRead([], NOW + 60, NOW + 60), now=NOW + 60)  # the scout dropped it
    sid = await add_signal(ts=NOW + 60)
    await copier.handle_pending(now=NOW + 61)
    assert await refused(sid) == "NOT_TRACKED"  # no new copies of a dropped wallet ...
    async with store.session() as conn:
        pos = (await ledger.open_positions(conn, settings.account))[0]
    stop = stop_price(pos.side, pos.entry_px)
    kf.marks["ETHUSDTM"] = stop
    kf.books["ETHUSDTM"] = book(stop)
    closed = await risk.mark_and_exit(kf, market, copier.executor, now=NOW + 120)  # type: ignore[arg-type]
    assert [c.reason for c in closed] == [fp.STOP_LOSS]  # ... but its open copy still exits by our rules
