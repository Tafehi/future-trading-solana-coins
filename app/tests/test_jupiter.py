"""Jupiter Perps: decoder (against REAL mainnet accounts), change-feed requests, and update processing."""

import base64
import hashlib
import struct

import pytest

from app.config import settings
from app.core import wallet_grader as wg
from app.core import wallet_stats as ws
from app.data import jupiter_perps as jp
from app.db import store
from app.jobs import jupiter
from app.tests.conftest import fixture

T = 1_800_000_000.0


def real_accounts() -> list[tuple[str, bytes]]:
    return [(a["pubkey"], base64.b64decode(a["data"])) for a in fixture("jupiter_positions.json")["accounts"]]


def test_real_accounts_are_216_bytes_with_the_anchor_discriminator() -> None:
    for _, raw in real_accounts():
        assert len(raw) == jp.ACCOUNT_SIZE == 216
        assert raw[:8] == jp.discriminator() == hashlib.sha256(b"account:Position").digest()[:8]


def test_decode_real_accounts() -> None:
    """Values read off mainnet on 2026-11-13 (DEBUG D-2): the layout offsets must keep producing them."""
    decoded = [
        jp.decode_slice(k, raw[jp.SLICE_OFFSET : jp.SLICE_OFFSET + jp.SLICE_LENGTH]) for k, raw in real_accounts()
    ]
    a, b, c = decoded
    assert a is not None and (a.coin, a.side, a.open_time) == ("SOL", "long", 1794524914)
    assert a.size_usd == pytest.approx(331.5417) and a.entry_px == pytest.approx(81.598366)
    assert a.owner == "EzMxY9AEPcygnrrAeVoTCkM9cj1CR8zDdxjzfdyhEDsF"
    assert b is not None and b.update_time == 1794550388 and b.is_open
    assert c is not None and c.side == "short" and not c.is_open  # a closed account keeps its row, size 0


def test_update_time_sits_where_the_filter_looks() -> None:
    _, raw = real_accounts()[1]
    (ut,) = struct.unpack_from("<q", raw, jp.UPDATE_TIME_OFFSET)
    assert ut == 1794550388
    assert raw[jp.UPDATE_TIME_OFFSET + 2 : jp.UPDATE_TIME_OFFSET + 8] == jp.block_of(ut).to_bytes(6, "little")


def test_change_feed_request_filters() -> None:
    body = jp.updated_request("PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu", jp.block_of(1794550388))
    opts = body["params"][1]  # type: ignore[index]
    assert opts["dataSlice"] == {"offset": 8, "length": 161}
    assert opts["filters"][0] == {"dataSize": 216}
    assert base64.b64decode(opts["filters"][1]["memcmp"]["bytes"]) == jp.discriminator()
    high = opts["filters"][2]["memcmp"]
    assert high["offset"] == 146 and base64.b64decode(high["bytes"]) == (1794550388 >> 16).to_bytes(6, "little")


def test_parse_rpc_answers() -> None:
    key, raw = real_accounts()[0]
    data = [base64.b64encode(raw[8:169]).decode(), "base64"]
    assert [p.pubkey for p in jp.parse_program_accounts({"result": [{"pubkey": key, "account": {"data": data}}]})] == [
        key
    ]
    multi = jp.parse_multiple({"result": {"value": [{"data": data}, None]}}, [key, "gone"])
    assert multi[key] is not None and multi["gone"] is None
    with pytest.raises(ValueError):
        jp.parse_program_accounts({"error": {"code": -32010}})


def test_b58_matches_known_keys() -> None:
    _, raw = real_accounts()[0]
    assert jp.b58encode(raw[72:104]) == "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz"  # the SOL custody


def pos(key: str, *, owner: str = "OWN", size: float = 5000.0, px: float = 100.0, opened: float = T, updated: float = T,
        side: str = "long") -> jp.JupPosition:  # fmt: skip
    return jp.JupPosition(key, owner, "SOL", side, size, px, int(opened), int(updated))


async def test_baseline_then_open_then_close_with_an_episode(fresh_db: str) -> None:
    first = await jupiter.process_updates([pos("OLD", opened=T - 9000)], {"SOL": 100.0}, now=T)
    assert first.baseline and first.opens == 0
    second = await jupiter.process_updates([pos("NEW", opened=T + 30, updated=T + 30)], {"SOL": 100.0}, now=T + 60)
    assert not second.baseline and second.opens == 1
    third = await jupiter.process_updates(
        [pos("NEW", size=0, opened=T + 30, updated=T + 100)], {"SOL": 110.0}, now=T + 120
    )
    assert third.closes == 1 and third.episodes == 1
    async with store.session() as conn:
        async with conn.execute("SELECT pnl FROM jup_episodes") as cur:
            assert [r["pnl"] for r in await cur.fetchall()] == [pytest.approx(500.0)]  # $5,000 long, +10%
        async with conn.execute("SELECT pubkey FROM jup_positions") as cur:
            assert [r["pubkey"] for r in await cur.fetchall()] == ["OLD"]


async def test_an_old_position_seen_late_is_not_an_open(fresh_db: str) -> None:
    await jupiter.process_updates([], {}, now=T)
    res = await jupiter.process_updates([pos("X", opened=T - 3600, updated=T + 50)], {}, now=T + 60)
    assert res.opens == 0
    async with store.session() as conn:
        async with conn.execute("SELECT COUNT(*) AS n FROM jup_positions") as cur:
            assert (await cur.fetchone())["n"] == 1  # type: ignore[index]


async def test_the_same_update_is_not_applied_twice(fresh_db: str) -> None:
    await jupiter.process_updates([], {}, now=T)
    await jupiter.process_updates([pos("A", opened=T + 10, updated=T + 10)], {}, now=T + 60)
    again = await jupiter.process_updates([pos("A", opened=T + 10, updated=T + 10)], {}, now=T + 120)
    assert again.opens == 0


async def test_a_size_increase_on_a_known_position_is_not_a_new_open(fresh_db: str) -> None:
    """ADD is ignored: a later update of an account we already hold must never become a second OPEN."""
    await jupiter.process_updates([], {}, now=T)
    # Opened 2 s before a poll, increased before the next one: the open time alone still looks "fresh".
    await jupiter.process_updates([pos("A", opened=T + 58, updated=T + 58)], {}, now=T + 60)
    bigger = await jupiter.process_updates([pos("A", size=9000, opened=T + 58, updated=T + 100)], {}, now=T + 120)
    assert bigger.opens == 0
    async with store.session() as conn:
        async with conn.execute("SELECT size_usd FROM jup_positions WHERE pubkey = 'A'") as cur:
            assert (await cur.fetchone())["size_usd"] == 9000  # type: ignore[index]


async def test_a_gap_makes_a_new_baseline(fresh_db: str) -> None:
    await jupiter.process_updates([], {}, now=T)
    gap = jupiter.BASELINE_GAP_POLLS * settings.jupiter_poll_seconds + 1
    res = await jupiter.process_updates([pos("B", opened=T + gap - 5, updated=T + gap - 5)], {}, now=T + gap)
    assert res.baseline and res.opens == 0


async def test_tracked_owner_open_and_close_become_signals(fresh_db: str) -> None:
    async with store.session() as conn:
        await conn.execute("INSERT INTO wallets (address, source, tier, tracked) VALUES ('OWN', 'jupiter', 'GOLD', 1)")
    await jupiter.process_updates([], {}, now=T)
    await jupiter.process_updates([pos("A", opened=T + 30, updated=T + 30)], {"SOL": 100.0}, now=T + 60)
    await jupiter.process_updates([pos("A", size=0, updated=T + 90)], {"SOL": 100.0}, now=T + 120)
    async with store.session() as conn:
        async with conn.execute("SELECT kind, source, coin, side FROM signals ORDER BY id") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("OPEN", "jupiter", "SOL", "long"), ("CLOSE", "jupiter", "SOL", "long")]


async def test_dust_is_ignored(fresh_db: str) -> None:
    await jupiter.process_updates([], {}, now=T)
    small = pos("A", size=settings.jupiter_min_size_usd - 1, opened=T + 30, updated=T + 30)
    assert (await jupiter.process_updates([small], {}, now=T + 60)).opens == 0


async def test_verify_closes_what_the_feed_missed(fresh_db: str) -> None:
    await jupiter.process_updates([pos("A", opened=T - 100)], {}, now=T)
    assert await jupiter.known_pubkeys() == ["A"]
    closed = await jupiter.verify_known({"A": pos("A", size=0, updated=T + 5000)}, {"SOL": 90.0}, now=T + 9000)
    assert closed == 1 and await jupiter.known_pubkeys() == []


def ep(pnl: float, i: int) -> ws.Episode:
    return ws.Episode("SOL", "long", i * 86_400_000, i * 86_400_000 + 7_200_000, pnl, 10_000.0)


def test_grade_needs_the_recording_first() -> None:
    eps = [ep(100.0, i) for i in range(20)]
    g, _, _ = jupiter.grade_episodes(eps, max_size=10_000, recorded_days=settings.jupiter_min_record_days - 0.1)
    assert g.tier == wg.WHITE


def test_grade_gold_and_black() -> None:
    good = [ep(300.0 if i % 4 else -100.0, i) for i in range(20)]
    g, _, dd = jupiter.grade_episodes(good, max_size=10_000, recorded_days=20)
    assert g.tier == wg.GOLD and dd is not None and dd <= settings.gold_max_drawdown
    bad = [ep(-50.0 if i % 2 else 40.0, i) for i in range(20)]
    g2, _, _ = jupiter.grade_episodes(bad, max_size=10_000, recorded_days=20)
    assert g2.tier == wg.BLACK
