"""The two programs (D-30): what each may load, the shared runtime, the settings that keep them apart, the scout's pick
(its time, and failing closed), and the trader's wiring from the scout's file to a paper position."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app import runtime
from app.config import Settings, settings
from app.core import wallet_grader as wg
from app.data import jupiter_perps as jp
from app.data.http import DataError
from app.data.solana_rpc import SolanaRpc
from app.db import crossread, jobclock, meta, migrations, store
from app.jobs import jupiter
from app.tests.helpers import FakeKucoin, book, contract, position_account
from app.trading import ledger
from app.trading.copier import Copier
from app.trading.paper_executor import PaperExecutor

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0


def _loaded(program: str, tmp_path: Path) -> set[str]:
    code = f"import json, sys, {program}; print(json.dumps(sorted(sys.modules)))"
    env = {"SETTINGS_FILE": "unset", "MODE": "paper", "PATH": "/usr/bin:/bin", "LOG_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert out.returncode == 0, out.stderr[-500:]
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_the_scout_loads_no_trading_code(tmp_path: Path) -> None:
    """The scout never trades and never sends Telegram: it cannot even load the code that would."""
    mods = _loaded("app.scout", tmp_path)
    bad = sorted(m for m in mods if m.startswith(("app.trading", "app.notifications", "app.data.kucoin_private",
                                                 "app.trader")))  # fmt: skip
    assert bad == []


def test_the_trader_loads_no_scout_code(tmp_path: Path) -> None:
    """The trader never grades or reads history: grading can never delay a copy."""
    mods = _loaded("app.trader", tmp_path)
    bad = sorted(m for m in mods if m.startswith(("app.jobs", "app.data.jupiter_api", "app.data.jupiter_events",
                                                 "app.scout")))  # fmt: skip
    assert bad == []


# ── the runtime ───────────────────────────────────────────────────────────────────────────


async def test_a_wake_starts_the_next_pass_at_once_and_every_pass_beats(fresh_db: str) -> None:
    stop, wake = asyncio.Event(), asyncio.Event()
    passes: list[float] = []

    async def body() -> None:
        passes.append(time.monotonic())
        if len(passes) == 1:
            raise RuntimeError("one bad pass")  # must not end the loop, and still beats

    task = asyncio.create_task(runtime.loop("events", 3600, body, stop, wake=wake))
    await asyncio.sleep(0.05)
    assert len(passes) == 1
    async with store.session() as conn:
        assert await meta.last_beat(conn, "events") is not None
    wake.set()
    await asyncio.sleep(0.05)
    assert len(passes) == 2  # an hour early: the wake ran it
    stop.set()
    await asyncio.wait_for(task, 1)


def test_use_db_points_every_session_at_the_programs_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "db_path", "unchanged")
    runtime.use_db(str(tmp_path / "scout.db"))
    assert settings.db_path == str(tmp_path / "scout.db")


# ── settings ──────────────────────────────────────────────────────────────────────────────


def test_one_file_for_both_programs_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("TRADER_DB_PATH", f"{tmp_path}/./x.db")  # a string: pathlib would drop the "." itself
    with pytest.raises(ValidationError, match="different files"):
        Settings()


def test_the_copy_list_age_must_cover_two_picks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JUPITER_GRADE_MINUTES", "5")
    monkeypatch.setenv("COPYLIST_MAX_AGE_S", "600")
    assert Settings().copylist_max_age_s == 600.0  # exactly two picks is enough
    monkeypatch.setenv("COPYLIST_MAX_AGE_S", "599.9")
    with pytest.raises(ValidationError, match="two picks"):
        Settings()


def test_the_websocket_url_is_derived_from_the_rpc_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "solana_ws_url", "")
    monkeypatch.setattr(settings, "solana_rpc_url", "https://node.invalid/placeholder")
    assert settings.ws_url == "wss://node.invalid/placeholder"
    monkeypatch.setattr(settings, "solana_ws_url", "wss://other.invalid/x")
    assert settings.ws_url == "wss://other.invalid/x"


# ── the scout's pick ──────────────────────────────────────────────────────────────────────


async def _graded(address: str, *, tier: str = wg.GOLD, at: float = NOW) -> None:
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO wallets (address, source, tier, graded_at, count_score) VALUES (?, 'jupiter', ?, ?, 5)",
            (address, tier, at),
        )


async def _flags() -> dict[str, tuple[int, int]]:
    async with store.session() as conn:
        async with conn.execute("SELECT address, tracked, watched FROM wallets ORDER BY address") as cur:
            return {str(r["address"]): (int(r["tracked"]), int(r["watched"])) for r in await cur.fetchall()}


async def _picked_at() -> float | None:
    async with store.session() as conn:
        got = await meta.get(conn, crossread.PICKED_AT_KEY)
    return float(got[0]) if got else None


async def test_the_pick_writes_its_time_with_the_flags(fresh_db: str) -> None:
    await _graded("G1")
    assert await jupiter.pick_owners(now=NOW) == 1
    assert await _flags() == {"G1": (1, 0)} and await _picked_at() == NOW


async def test_an_unreadable_shadow_record_keeps_the_old_pick(fresh_db: str, tmp_path: Path) -> None:
    await _graded("G1")
    await jupiter.pick_owners(now=NOW)
    await _graded("G2", at=NOW + 300)
    with pytest.raises(jupiter.ShadowUnreadable):
        await jupiter.pick_owners(now=NOW + 300, shadow_db=str(tmp_path / "missing-trader.db"))
    assert await _flags() == {"G1": (1, 0), "G2": (0, 0)}  # nothing re-picked without the shadow verdicts
    assert await _picked_at() == NOW  # so the trader sees the list age, not a fresh-looking one
    assert not (tmp_path / "missing-trader.db").exists()


async def test_the_grade_loop_retries_an_unreadable_shadow_record_in_a_minute(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.scout import Scout

    monkeypatch.setattr(settings, "trader_db_path", str(tmp_path / "missing-trader.db"))
    s = Scout()
    try:
        before = time.time()
        await s.grade_pass()
        assert before + jupiter.SHADOW_RETRY_S <= await jobclock.next_run_at("jupiter_grade") <= time.time() + 60
    finally:
        for c in (s.kf, s.solana, s.jup_api):
            await c.close()


async def test_a_failing_feed_poll_does_not_stop_the_pick(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grading is its own loop (it was inside the feed pass, so a failing poll skipped every pick)."""
    from app.scout import Scout

    trader_db = str(tmp_path / "trader.db")
    await migrations.migrate(trader_db)  # the trader's file exists: its shadow record can be read
    monkeypatch.setattr(settings, "trader_db_path", trader_db)
    s = Scout()

    async def broken() -> None:
        raise DataError("getProgramAccounts: HTTP 503")

    stop = asyncio.Event()
    try:
        tasks = [asyncio.create_task(runtime.loop("jupiter", 60, broken, stop)),
                 asyncio.create_task(runtime.loop("grade", 60, s.grade_pass, stop))]  # fmt: skip
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert await _picked_at() is not None
    finally:
        for c in (s.kf, s.solana, s.jup_api):
            await c.close()


# ── the scout's one-time owner seed: a fresh scout.db must not forget who the single bot saw ─────────


async def _trader_owners(path: str, rows: list[tuple[str, float, float]]) -> None:
    await migrations.migrate(path)
    async with store.session(path) as conn:
        await conn.executemany(
            "INSERT INTO jup_owners (owner, first_seen, max_open_usd, backfilled_at) VALUES (?, ?, ?, 5)", rows
        )


async def _owners() -> dict[str, tuple[float, float, object]]:
    async with store.session() as conn:
        async with conn.execute("SELECT owner, first_seen, max_open_usd, backfilled_at FROM jup_owners") as cur:
            return {str(r["owner"]): (float(r["first_seen"]), float(r["max_open_usd"]), r["backfilled_at"])
                    for r in await cur.fetchall()}  # fmt: skip


async def test_a_fresh_scout_seeds_the_owner_list_once(fresh_db: str, tmp_path: Path) -> None:
    from app.jobs import jupiter_history

    trader = str(tmp_path / "trader.db")
    await _trader_owners(trader, [("A", 100.0, 5000.0), ("B", 200.0, 9000.0), ("C", 10.0, 10.0)])
    async with store.session() as conn:  # the scout's feed already saw A (earlier, smaller) and C (later, bigger)
        await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES ('A', 50, 2000)")
        await conn.execute("INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES ('C', 300, 9999)")
    assert await jupiter.seed_owners(trader, now=NOW) == 1  # only B is new
    # The earliest first-seen and the largest size win; backfilled_at stays empty, so every past is read again.
    assert await _owners() == {"A": (50.0, 5000.0, None), "B": (200.0, 9000.0, None), "C": (10.0, 9999.0, None)}
    assert await jupiter_history.next_owners(10) == ["C", "B", "A"]  # the API reads them biggest first
    assert await jupiter.seed_owners(trader, now=NOW + 60) == 0  # once


async def test_an_unreadable_trader_file_seeds_nothing_and_is_tried_again(fresh_db: str, tmp_path: Path) -> None:
    missing = tmp_path / "trader.db"
    assert await jupiter.seed_owners(str(missing), now=NOW) == 0
    assert not missing.exists()  # read-only: the scout never creates the trader's file
    await _trader_owners(str(missing), [("A", 100.0, 5000.0)])
    assert await jupiter.seed_owners(str(missing), now=NOW + 60) == 1  # the next start seeds


async def test_the_scout_seeds_at_boot(fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scout import Scout

    trader = str(tmp_path / "trader.db")
    await _trader_owners(trader, [("A", 100.0, 5000.0)])
    monkeypatch.setattr(settings, "trader_db_path", trader)
    s = Scout()

    async def refreshed(kf: object) -> bool:
        return True

    monkeypatch.setattr(s.market, "refresh", refreshed)
    try:
        await s.boot(asyncio.Event())
        assert "A" in await _owners()
    finally:
        for c in (s.kf, s.solana, s.jup_api):
            await c.close()


# ── the trader, from the scout's file to a paper position ─────────────────────────────────


async def _scout_file(path: str, owner: str, key: str, *, picked_at: float) -> None:
    await migrations.migrate(path)
    async with store.session(path) as conn:
        await conn.execute(
            "INSERT INTO wallets (address, source, tier, tracked, watched, copy_weight, score, account_value, "
            "count_score) VALUES (?, 'jupiter', 'GOLD', 1, 0, 10, 8, 1000000, 9)",
            (owner,),
        )
        await conn.execute(
            "INSERT INTO jup_positions (pubkey, owner, coin, side, size_usd, entry_px, open_time, seen_at) "
            "VALUES (?, ?, 'SOL', 'long', 0, 150, 1, 1)",
            (key, owner),
        )
        await meta.put(conn, crossread.PICKED_AT_KEY, picked_at)


async def test_the_trader_copies_an_open_its_poll_sees(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.trader import Trader

    owner, key = jp.b58encode(bytes([21]) * 32), "ACC-21"
    scout_db = str(tmp_path / "scout.db")
    now = time.time()
    await _scout_file(scout_db, owner, key, picked_at=now)
    monkeypatch.setattr(settings, "scout_db_path", scout_db)
    data = position_account(21, size_usd=20_000.0, price=150.0, opened=int(now) - 5)
    calls: list[str] = []

    def rpc(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        assert body["params"][0] == [key]  # only the listed owner's known account: never getProgramAccounts
        piece = data[jp.SLICE_OFFSET : jp.SLICE_OFFSET + jp.SLICE_LENGTH]
        value = [{"data": [base64.b64encode(piece).decode(), "base64"]}]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 777}, "value": value}}
        )

    t = Trader()
    kf = FakeKucoin(books={"SOLUSDTM": book(150.0, spread=0.01, symbol="SOLUSDTM")}, marks={"SOLUSDTM": 150.0})
    t.market.load([contract("SOLUSDTM", base="SOL", multiplier=0.1, mark=150.0)])
    t.solana = SolanaRpc(url="https://rpc.invalid/placeholder", transport=httpx.MockTransport(rpc))
    t.executor = PaperExecutor(kf, delay_s=0)  # type: ignore[arg-type]
    t.copier = Copier(kf, t.market, t.executor, t.copylist)  # type: ignore[arg-type]
    try:
        await t.copylist_pass()
        assert t.copylist.owners_ranked() == [owner] and t.scout_keys == {owner: {key}}
        await t.feedpoll_pass()
        assert calls == ["getMultipleAccounts"] and t.queue.qsize() == 1
        t.worker = asyncio.create_task(t.feed_worker())
        await asyncio.sleep(0.2)
        assert t.wake.is_set()  # a written signal wakes the copier at once
        opened = await t.copier.handle_pending()
        assert len(opened) == 1
        async with store.session() as conn:
            pos = (await ledger.open_positions(conn, settings.account))[0]
            async with conn.execute("SELECT via, position_key, open_time FROM signals") as cur:
                sig: dict[str, Any] = dict(await cur.fetchone() or {})
        assert (pos.wallet, pos.symbol) == (owner, "SOLUSDTM")
        assert sig == {"via": "poll", "position_key": key, "open_time": int(now) - 5}
    finally:
        if t.worker is not None:
            t.worker.cancel()
        await t.solana.close()
        await t.kf.close()
        await t.notifier.close()


def test_no_program_reads_the_old_single_bot_entry_point() -> None:
    assert not (REPO / "app" / "main.py").exists()
    assert os.path.exists(REPO / "app" / "scout.py") and os.path.exists(REPO / "app" / "trader.py")
