"""More wallets pass (user, 2026-11-18, D-29): every owner's past from Jupiter's trade API, a smaller trade minimum
with a stronger proof below 15 trades, a size cap for short records, and a daily re-grade. Boundaries tested AT the
value (rule 7)."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.core import wallet_grader as wg
from app.core import wallet_stats as ws
from app.data.http import DataError
from app.data.jupiter_events import ChainEpisode
from app.db import store
from app.jobs import jupiter, jupiter_history
from app.tests.conftest import fixture

T = 1_800_000_000.0
DAY = 86_400


def ep(pnl: float, i: int) -> ws.Episode:
    return ws.Episode("SOL", "long", i * 86_400_000, i * 86_400_000 + 7_200_000, pnl, 10_000.0)


def grade(eps: list[ws.Episode]) -> wg.Grade:
    g, _, _ = jupiter.grade_episodes(eps, max_size=10_000.0, recorded_days=90.0)
    return g


# --- the proof rule -------------------------------------------------------------------------------------------------
def test_no_change_from_15_episodes_on() -> None:
    st = ws.stats([ep(100, i) for i in range(12)] + [ep(-100, 20 + i) for i in range(3)], [], set(), equity=0.0)
    assert st.trades == 15 and jupiter.proof(st) == (st.profit_factor, 1.0)


def test_below_15_the_needed_pf_grows_with_the_square_root() -> None:
    st = ws.stats([ep(100, i) for i in range(10)] + [ep(-100, 20 + i) for i in range(4)], [], set(), equity=0.0)
    pf, scale = jupiter.proof(st)
    assert st.trades == 14 and pf == st.profit_factor and scale == pytest.approx((15 / 14) ** 0.5)


def test_no_loss_yet_counts_as_one_average_loss() -> None:
    small = ws.stats([ep(100, i) for i in range(9)], [], set(), equity=0.0)
    assert jupiter.proof(small)[0] == 9.0  # not the 99 cap
    full = ws.stats([ep(100, i) for i in range(15)], [], set(), equity=0.0)
    assert jupiter.proof(full)[0] == ws.PF_CAP


def test_gold_at_exactly_the_scaled_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 episodes with FULL_PROOF 32: scale is exactly 2, so GOLD needs PF 3.6 and SILVER 2.8."""
    monkeypatch.setattr(settings, "jupiter_full_proof_episodes", 32)
    wins = [ep(120, i) for i in range(6)]
    assert grade(wins + [ep(-100, 10), ep(-100, 11)]).tier == wg.GOLD  # PF 720 / 200 = 3.6
    assert grade([ep(119.8, i) for i in range(6)] + [ep(-100, 10), ep(-100, 11)]).tier == wg.SILVER  # PF 3.594
    assert grade([ep(93.4, i) for i in range(6)] + [ep(-100, 10), ep(-100, 11)]).tier == wg.SILVER  # PF 2.802
    assert grade([ep(93.2, i) for i in range(6)] + [ep(-100, 10), ep(-100, 11)]).tier == wg.BRONZE  # PF 2.796


def test_minimum_episodes_at_exactly_eight() -> None:
    assert settings.jupiter_min_episodes == 8
    assert grade([ep(100, i) for i in range(7)] + [ep(-10, 30)]).tier != wg.WHITE  # 8: graded
    assert grade([ep(100, i) for i in range(6)] + [ep(-10, 30)]).tier == wg.WHITE  # 7: still recording


def test_a_lucky_short_streak_is_not_gold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Was: no loss = PF 99 = GOLD whatever the count. Now 8 straight wins count PF 8 against a need of 2.46."""
    g = grade([ep(100, i) for i in range(8)])
    assert g.tier == wg.GOLD and "few trades" in g.reason
    monkeypatch.setattr(settings, "jupiter_full_proof_episodes", 200)  # need 1.8 x 5 = 9 > 8
    assert grade([ep(100, i) for i in range(8)]).tier != wg.GOLD


# --- grade_owners: the weight cap and the daily re-grade --------------------------------------------------------
async def _owner(conn: Any, name: str, n: int, *, graded_at: float | None = None) -> None:
    await conn.execute("INSERT INTO jup_owners (owner, first_seen, history_from, backfilled_at, max_open_usd) "
                       "VALUES (?, ?, ?, ?, 20000)", (name, T, T - 90 * DAY, T))  # fmt: skip
    for i in range(n):
        await conn.execute(
            "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl, "
            "origin, position_key) VALUES (?, 'SOL', 'long', 10000, 100, 103, ?, ?, 300, 'api', ?)",
            (name, T - (80 - i) * DAY, T - (80 - i) * DAY + 7200, f"{name}{i}"),
        )
    if graded_at is not None:
        await conn.execute("INSERT INTO wallets (address, source, tier, graded_at) VALUES (?, 'jupiter', 'GOLD', ?)",
                           (name, graded_at))  # fmt: skip


async def test_a_short_record_copies_small(fresh_db: str) -> None:
    async with store.session() as conn:
        await _owner(conn, "SHORT", 10)
        await _owner(conn, "FULL", 15)
    await jupiter.grade_owners(now=T)
    async with store.session() as conn:
        async with conn.execute("SELECT address, tier, copy_weight FROM wallets ORDER BY address") as cur:
            rows = {r["address"]: (r["tier"], r["copy_weight"]) for r in await cur.fetchall()}
    assert rows["FULL"] == (wg.GOLD, 10)  # no loss in 15: full weight, as before
    assert rows["SHORT"] == (wg.GOLD, settings.jupiter_small_sample_max_weight)  # 10 straight wins: capped


async def test_a_grade_is_redone_after_exactly_the_regrade_hours(fresh_db: str) -> None:
    """No new close since its grade: re-graded once the grade is JUPITER_REGRADE_HOURS old, not before."""
    hours = settings.jupiter_regrade_hours * 3600
    async with store.session() as conn:
        await _owner(conn, "OLD", 15, graded_at=T - hours - 1)
        await _owner(conn, "EDGE", 15, graded_at=T - hours)
        await _owner(conn, "FRESH", 15, graded_at=T - hours + 1)
    todo = {o for o, _ in await jupiter._owners_to_grade(T)}
    assert todo == {"OLD"}


# --- history: the API first, every owner, no Chainstack budget ---------------------------------------------------
class FakeApi:
    def __init__(self, trades: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.trades, self.fail = trades or [], fail
        self.asked: list[str] = []

    async def history(self, owner: str) -> Any:
        self.asked.append(owner)
        if self.fail:
            raise DataError("jupiter trades: HTTP 503")
        from app.data.jupiter_api import History

        return History([dict(t, owner=owner) for t in self.trades], True)


async def test_every_recorded_owner_is_queued(fresh_db: str) -> None:
    async with store.session() as conn:
        await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES ('SMALL', ?, 1000)", (T,))
        await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES ('DUST', ?, 999)", (T,))
    assert await jupiter_history.next_owners(10) == ["SMALL"]  # the feed's own $1,000 minimum, was $10,000


async def test_api_history_replaces_feed_and_chain_and_spends_no_chainstack_call(fresh_db: str) -> None:
    trades = fixture("jupiter_api_trades.json")["trades"]
    newest = max(t["createdTime"] for t in trades)
    now = float(newest) + 60
    async with store.session() as conn:
        await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES ('W', ?, 50000)", (now,))
        for origin, key in (("feed", None), ("chain", "old")):
            await conn.execute(
                "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl, "
                "origin, position_key) VALUES ('W', 'SOL', 'long', 1, 1, 1, 1, ?, 999, ?, ?)", (now - 10, origin, key),
            )  # fmt: skip
    api = FakeApi(trades)
    n = await jupiter_history.backfill_owner(None, "W", jupiter_history.Budget(now=now), now=now, api=api)  # type: ignore[arg-type]
    async with store.session() as conn:
        async with conn.execute(
            "SELECT origin, COUNT(*) n, ROUND(SUM(pnl), 2) pnl FROM jup_episodes GROUP BY origin"
        ) as c:
            rows = {r["origin"]: (r["n"], r["pnl"]) for r in await c.fetchall()}
        async with conn.execute("SELECT backfilled_at, history_from FROM jup_owners WHERE owner = 'W'") as c:
            o = await c.fetchone()
    assert n == 20 and rows == {"api": (20, pytest.approx(-2828.53, abs=0.01))}  # feed and chain rows replaced
    assert o is not None and o["backfilled_at"] == now
    assert o["history_from"] == pytest.approx(now - settings.jupiter_history_days * DAY)
    assert await jupiter_history.calls_today(now=now) == 0  # no Chainstack call
    again = await jupiter_history.backfill_owner(None, "W", jupiter_history.Budget(now=now), now=now, api=api)  # type: ignore[arg-type]
    async with store.session() as conn:
        async with conn.execute("SELECT COUNT(*) n FROM jup_episodes") as c:
            row = await c.fetchone()
    assert again == 20 and row is not None and row["n"] == 20  # a re-read does not duplicate


async def test_api_failure_falls_back_to_the_chain(fresh_db: str) -> None:
    from app.tests.test_jupiter_history import _real_setup

    rpc, owner = _real_setup()
    now = float(fixture("jupiter_close_tx.json")["tx"]["blockTime"]) + 60
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES (?, ?, 50000)", (owner, now)
        )
    api = FakeApi(fail=True)
    n = await jupiter_history.backfill_owner(rpc, owner, jupiter_history.Budget(now=now), now=now, api=api)  # type: ignore[arg-type]
    async with store.session() as conn:
        async with conn.execute("SELECT origin FROM jup_episodes") as c:
            origins = [r["origin"] for r in await c.fetchall()]
    assert api.asked == [owner] and n == 1 and origins == ["chain"]
    assert await jupiter_history.calls_today(now=now) == 3  # only the fallback spends Chainstack calls


async def test_chain_only_when_the_setting_says_so(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tests.test_jupiter_history import _real_setup

    monkeypatch.setattr(settings, "jupiter_history_source", "chain")
    rpc, owner = _real_setup()
    now = float(fixture("jupiter_close_tx.json")["tx"]["blockTime"]) + 60
    api = FakeApi(fixture("jupiter_api_trades.json")["trades"])
    await jupiter_history.backfill_owner(rpc, owner, jupiter_history.Budget(now=now), now=now, api=api)  # type: ignore[arg-type]
    assert api.asked == []


async def test_a_pass_reads_up_to_owners_per_pass(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jupiter_history_owners_per_pass", 3)
    async with store.session() as conn:
        for i in range(5):
            await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES (?, ?, ?)",
                               (f"O{i}", T, 5000 + i))  # fmt: skip
    api = FakeApi([])
    done = await jupiter_history.step(None, deadline=float("inf"), now=T, api=api)  # type: ignore[arg-type]
    assert done == 3 and api.asked == ["O4", "O3", "O2"]  # biggest first


def test_chain_episode_type_is_shared() -> None:
    assert ChainEpisode.__name__ == "ChainEpisode"


# --- settings ---------------------------------------------------------------------------------------------------
def test_min_episodes_above_full_proof_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUPITER_MIN_EPISODES", "16")
    with pytest.raises(ValidationError, match="JUPITER_MIN_EPISODES"):
        Settings()
    monkeypatch.setenv("JUPITER_MIN_EPISODES", "15")
    assert Settings().jupiter_min_episodes == 15  # equal is fine


def test_unknown_history_source_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUPITER_HISTORY_SOURCE", "helius")
    with pytest.raises(ValidationError, match="JUPITER_HISTORY_SOURCE"):
        Settings()


def test_main_hands_the_api_to_the_history_job() -> None:
    from pathlib import Path

    main = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "jupiter_history.step(self.solana, deadline=time.time() + 50, api=self.jup_api)" in main


async def test_migration_3_queues_the_chain_read_owners_for_the_api(tmp_path: Any) -> None:
    """The owners read from the chain before D-29 (liquidation and open fees missed) are read again, once."""
    from app.db import migrations

    path = str(tmp_path / "v2.db")
    async with store.session(path) as conn:
        for script in migrations.MIGRATIONS[:2]:
            await conn.executescript(script)
        await conn.execute("INSERT INTO meta (key, value, updated_at) VALUES ('schema_version', '2', 0)")
        await conn.execute(
            "INSERT INTO jup_owners (owner, first_seen, backfilled_at, max_open_usd) VALUES ('C', 1, 5, 9e4)"
        )
    assert await migrations.migrate(path) == 3
    async with store.session(path) as conn:
        async with conn.execute("SELECT backfilled_at FROM jup_owners WHERE owner = 'C'") as cur:
            row = await cur.fetchone()
    assert row is not None and row["backfilled_at"] is None
