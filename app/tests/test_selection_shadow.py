"""Items 4 and 5: the selection rule, shadow copies, SILVER at a small size, and the watch-list poll cadence."""

import pytest

from app.config import settings
from app.core import selection
from app.core import wallet_grader as wg
from app.core.selection import Candidate
from app.data.market import MarketCache
from app.db import store
from app.tests.helpers import FakeKucoin, TableCopyList, book, contract
from app.trading import futures_paper as fp
from app.trading import ledger, shadow
from app.trading.copier import Copier
from app.trading.paper_executor import PaperExecutor

NOW = 1_800_000_000.0


def C(addr: str, tier: str = "GOLD", score: float = 1.0, n: int = 0, mean: float | None = None) -> Candidate:
    return Candidate(addr, tier, score, n, mean)


def test_proven_first_failed_out_silver_capped() -> None:
    cands = [
        C("gold-high", score=50),
        C("gold-low", score=10),
        C("gold-failed", score=99, n=5, mean=0.0),  # 5 shadow copies, mean exactly at the line: copying loses
        C("silver-proven", tier="SILVER", score=1, n=5, mean=1.5),
        C("silver-a", tier="SILVER", score=9),
        C("silver-b", tier="SILVER", score=8),
        C("bronze", tier="BRONZE", score=100),
    ]
    tracked, watched = selection.pick(cands, max_total=4, max_silver=2)
    assert tracked == ["silver-proven", "gold-high", "gold-low", "silver-a"]
    assert watched == ["gold-failed", "silver-b"]  # watched in rank order; BRONZE never


def test_the_silver_cap_binds_at_its_value() -> None:
    """Room for 10, but at most 2 SILVER: the third SILVER is watched, not tracked."""
    cands = [C("g"), C("s1", tier="SILVER", score=3), C("s2", tier="SILVER", score=2), C("s3", tier="SILVER", score=1)]
    tracked, watched = selection.pick(cands, max_total=10, max_silver=2)
    assert tracked == ["g", "s1", "s2"] and watched == ["s3"]


def test_four_shadow_copies_are_not_a_verdict() -> None:
    tracked, _ = selection.pick([C("g", n=4, mean=-9.0)], max_total=5, max_silver=0)
    assert tracked == ["g"]  # SHADOW_MIN_TRADES is 5: four losing copies do not exclude it yet


def test_silver_off_and_shadow_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "copy_silver", False)
    assert selection.pick([C("s", tier="SILVER")], max_total=5, max_silver=5) == ([], [])
    monkeypatch.setattr(settings, "copy_silver", True)
    monkeypatch.setattr(settings, "shadow_enabled", False)
    tracked, watched = selection.pick([C("g", n=9, mean=-5.0), C("h", score=2)], max_total=5, max_silver=5)
    assert tracked == ["h", "g"] and watched == []  # without shadows the grade alone decides


def test_net_return_includes_two_fees_and_liquidation_is_the_margin() -> None:
    assert shadow.net_return_pct("long", 100.0, 106.0, 0.0006, fp.TRAIL) == pytest.approx(6.0 - 0.12)
    assert shadow.net_return_pct("short", 100.0, 94.0, 0.0006, fp.STOP_LOSS) == pytest.approx(6.0 - 0.12)
    assert shadow.net_return_pct("long", 100.0, 1.0, 0.0006, fp.LIQUIDATED) == pytest.approx(-100.0 / 3)


async def _wallet(addr: str, *, tier: str, tracked: int, watched: int, weight: int = 10) -> None:
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO wallets (address, source, tier, tracked, watched, copy_weight, score, account_value) "
            "VALUES (?, 'jupiter', ?, ?, ?, ?, 5, 1000000)",
            (addr, tier, tracked, watched, weight),
        )


async def _signal(addr: str, *, ts: float = NOW) -> int:
    async with store.session() as conn:
        cur = await conn.execute(
            "INSERT INTO signals (ts, wallet, source, coin, kind, side, tier, wallet_size, wallet_entry_px) "
            "VALUES (?, ?, 'jupiter', 'ETH', 'OPEN', 'long', 'GOLD', 50000, 2000)",
            (ts, addr),
        )
        return int(cur.lastrowid or 0)


def _copier() -> tuple[FakeKucoin, MarketCache, Copier]:
    kf = FakeKucoin(books={"ETHUSDTM": book(2000.0)}, marks={"ETHUSDTM": 2000.0})
    market = MarketCache()
    market.load([contract()])
    return kf, market, Copier(kf, market, PaperExecutor(kf, delay_s=0), TableCopyList())  # type: ignore[arg-type]


async def _rows(sql: str, args: tuple[object, ...] = ()) -> list[dict[str, object]]:
    async with store.session() as conn:
        async with conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def test_a_watched_wallet_gets_a_shadow_copy_only(fresh_db: str) -> None:
    await _wallet("0xw", tier="GOLD", tracked=0, watched=1)
    sid = await _signal("0xw")
    _, _, copier = _copier()
    assert await copier.handle_pending(now=NOW + 1) == []
    sig = (await _rows("SELECT refused FROM signals WHERE id = ?", (sid,)))[0]
    sh = await _rows("SELECT wallet, entry_px, status FROM shadow_positions")
    assert sig["refused"] == "SHADOW_ONLY"
    assert sh == [{"wallet": "0xw", "entry_px": pytest.approx(2000.1), "status": "OPEN"}]  # the best ask


async def test_a_tracked_wallet_gets_both_and_one_shadow_per_contract(fresh_db: str) -> None:
    await _wallet("0xt", tier="GOLD", tracked=1, watched=0)
    _, _, copier = _copier()
    await _signal("0xt")
    await _signal("0xt")  # a second open on the same contract: no second shadow, and the real copy is SYMBOL_HELD
    opened = await copier.handle_pending(now=NOW + 1)
    assert len(opened) == 1 and len(await _rows("SELECT id FROM shadow_positions")) == 1


async def test_silver_is_copied_at_weight_three_at_most(fresh_db: str) -> None:
    await _wallet("0xs", tier="SILVER", tracked=1, watched=0, weight=10)
    await _signal("0xs")
    _, _, copier = _copier()
    await copier.handle_pending(now=NOW + 1)
    async with store.session() as conn:
        pos = (await ledger.open_positions(conn, settings.account))[0]
    full = 10_000 * settings.position_fraction  # weight 10 margin
    assert pos.copy_weight == 3
    assert pos.margin == pytest.approx(full * wg.size_share(3), rel=0.05)


async def test_shadow_stop_fires_and_stats_count_it(fresh_db: str) -> None:
    await _wallet("0xw", tier="GOLD", tracked=0, watched=1)
    await _signal("0xw")
    kf, market, copier = _copier()
    await copier.handle_pending(now=NOW + 1)
    stop = 2000.1 * 0.94  # exactly -6% from the entry (the best ask)
    market.load([contract(mark=stop)])
    assert await shadow.mark_and_exit(market, now=NOW + 60) == 1
    st = (await shadow.stats(now=NOW + 120))["0xw"]
    assert st.n == 1 and st.wins == 0 and st.mean == pytest.approx(-6.0 - 0.12)
