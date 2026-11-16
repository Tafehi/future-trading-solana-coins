"""End-to-end on a real SQLite file with fake exchanges: signal -> paper position -> exits -> ledger -> digest."""

import time
from typing import Any

import pytest

from app.config import settings
from app.core import wallet_grader as wg
from app.data.market import MarketCache
from app.db import meta, store
from app.notifications import digest, messages
from app.tests.helpers import FakeKucoin, FakeNotifier, book, contract
from app.trading import futures_paper as fp
from app.trading import ledger, risk
from app.trading.copier import Copier, stop_price
from app.trading.paper_executor import PaperExecutor

W = "0x" + "ab" * 20
NOW = 1_800_000_000.0


async def add_wallet(address: str = W, *, tier: str = wg.GOLD, weight: int = 10, score: float = 8.0) -> None:
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO wallets (address, source, tier, tracked, copy_weight, score, account_value) "
            "VALUES (?, 'jupiter', ?, 1, ?, ?, 1000000)",
            (address, tier, weight, score),
        )


async def add_signal(*, ts: float = NOW, coin: str = "ETH", side: str = "long", entry: float = 2000.0,
                     wallet: str = W, size_usd: float = 50_000.0) -> int:  # fmt: skip
    async with store.session() as conn:
        cur = await conn.execute(
            "INSERT INTO signals (ts, wallet, source, coin, kind, side, tier, wallet_size, wallet_entry_px, "
            "wallet_leverage) VALUES (?, ?, 'jupiter', ?, 'OPEN', ?, 'GOLD', ?, ?, 3)",
            (ts, wallet, coin, side, size_usd, entry),
        )
        return int(cur.lastrowid or 0)


def setup(mid: float = 2000.0) -> tuple[FakeKucoin, MarketCache, Copier]:
    kf = FakeKucoin(books={"ETHUSDTM": book(mid)}, marks={"ETHUSDTM": mid})
    market = MarketCache()
    market.load([contract(mark=mid)])
    ex = PaperExecutor(kf, delay_s=0)  # type: ignore[arg-type]
    return kf, market, Copier(kf, market, ex)  # type: ignore[arg-type]


async def signal_row(sid: int) -> dict[str, Any]:
    async with store.session() as conn:
        async with conn.execute("SELECT * FROM signals WHERE id = ?", (sid,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def test_a_gold_open_becomes_a_sized_paper_position(fresh_db: str) -> None:
    await add_wallet()
    sid = await add_signal()
    _, _, copier = setup()
    opened = await copier.handle_pending(now=NOW + 5)
    assert len(opened) == 1
    async with store.session() as conn:
        pos = (await ledger.open_positions(conn, settings.account))[0]
        drift = await ledger.reconcile(conn, settings.account)
    # $10,000 x 6% x weight 10 (full) = $600 margin -> $1,800 notional at 3x; 1 lot = 0.01 ETH x $2000 = $20.
    assert pos.contracts == 90 and pos.leverage == 3 and pos.margin == pytest.approx(90 * 0.01 * 2000.1 / 3)
    assert pos.entry_px == pytest.approx(2000.1)
    assert abs(drift) < 1e-9
    assert (await signal_row(sid))["copied"] == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"ts": NOW - 100}, "STALE"),
        ({"wallet": "0x" + "cd" * 20}, "NOT_TRACKED"),
        ({"coin": "NOPE"}, "NOT_LISTED"),
        ({"entry": 2100.0}, "ENTRY_DRIFT"),
    ],
)
async def test_refusals(fresh_db: str, change: dict[str, Any], code: str) -> None:
    await add_wallet()
    sid = await add_signal(**change)
    _, _, copier = setup()
    assert await copier.handle_pending(now=NOW + 1) == []
    assert (await signal_row(sid))["refused"] == code


async def test_a_second_signal_on_a_held_contract_is_refused_unless_much_better(fresh_db: str) -> None:
    await add_wallet(score=8.0)
    other = "0x" + "ef" * 20
    await add_wallet(other, score=8.5)
    _, _, copier = setup()
    await add_signal()
    await copier.handle_pending(now=NOW + 1)
    sid = await add_signal(wallet=other)
    await copier.handle_pending(now=NOW + 2)
    assert (await signal_row(sid))["refused"] == "SYMBOL_HELD"


async def test_a_much_better_wallet_replaces_the_held_copy(fresh_db: str) -> None:
    await add_wallet(score=5.0)
    better = "0x" + "ef" * 20
    await add_wallet(better, score=6.0)  # exactly REPLACE_MIN_SCORE_GAP (1.0) higher: replaces
    _, _, copier = setup()
    await add_signal()
    await copier.handle_pending(now=NOW + 1)
    await add_signal(wallet=better)
    opened = await copier.handle_pending(now=NOW + 2)
    async with store.session() as conn:
        async with conn.execute("SELECT reason FROM positions WHERE status = 'CLOSED'") as cur:
            reasons = [r["reason"] for r in await cur.fetchall()]
        held = await ledger.open_positions(conn, settings.account)
    assert reasons == [fp.REPLACED] and len(opened) == 1 and held[0].wallet == better


async def test_the_stop_closes_at_six_percent_and_the_ledger_balances(fresh_db: str) -> None:
    await add_wallet()
    await add_signal()
    kf, market, copier = setup()
    await copier.handle_pending(now=NOW + 1)
    async with store.session() as conn:
        pos = (await ledger.open_positions(conn, settings.account))[0]
    stop = stop_price(pos.side, pos.entry_px)
    kf.marks["ETHUSDTM"] = stop
    kf.books["ETHUSDTM"] = book(stop)
    closed = await risk.mark_and_exit(kf, market, copier.executor, now=NOW + 60)  # type: ignore[arg-type]
    assert [c.reason for c in closed] == [fp.STOP_LOSS]
    assert closed[0].net < 0
    assert await risk.snapshot(now=NOW + 61) == ""
    async with store.session() as conn:
        assert abs(await ledger.reconcile(conn, settings.account)) < 1e-9
        assert await ledger.open_positions(conn, settings.account) == []


async def test_funding_is_applied_once_per_settlement(fresh_db: str) -> None:
    await add_wallet()
    await add_signal()
    kf, market, copier = setup()
    await copier.handle_pending(now=NOW + 1)
    t = int((NOW + 3600) * 1000)
    kf.funding["ETHUSDTM"] = [(t, 0.0001)]
    assert await risk.settle_funding(kf, now=NOW + 7200) == 1  # type: ignore[arg-type]
    assert await risk.settle_funding(kf, now=NOW + 7300) == 0  # type: ignore[arg-type]
    async with store.session() as conn:
        pos = (await ledger.open_positions(conn, settings.account))[0]
        assert pos.funding == pytest.approx(-pos.qty * pos.entry_px * 0.0001)  # a long pays a positive rate
        assert abs(await ledger.reconcile(conn, settings.account)) < 1e-9


async def test_a_cent_of_drift_halts_the_account(fresh_db: str) -> None:
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO ledger (account, ts, kind, amount, note) VALUES (?, ?, 'FEE', -0.011, 'tamper')",
            (settings.account, NOW),
        )
    assert "drift" in await risk.snapshot(now=NOW)
    async with store.session() as conn:
        assert (await ledger.state(conn, settings.account))["halted"] == 1


async def test_drift_at_exactly_the_tolerance_does_not_halt(fresh_db: str) -> None:
    async with store.session() as conn:
        await conn.execute(
            "INSERT INTO ledger (account, ts, kind, amount, note) VALUES (?, ?, 'FEE', -0.01, 'edge')",
            (settings.account, NOW),
        )
    assert await risk.snapshot(now=NOW) == ""


async def test_digest_sends_one_window_once_and_skips_empty_ones(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "digest_minutes", 15)
    step = 900.0
    w0 = NOW - (NOW % step)  # a window boundary
    async with store.session() as conn:
        await meta.put(conn, digest.LAST_END_KEY, w0)
    await add_wallet()
    await add_signal(ts=w0 + 10)
    _, _, copier = setup()
    await copier.handle_pending(now=w0 + 10)
    n = FakeNotifier()
    assert await digest.run_digest(n, now=w0 + 2 * step + 5) == 1  # window 1 had the open, window 2 was empty
    assert len(n.sent) == 1 and "OPEN #1" in n.sent[0] and "[PAPER]" in n.sent[0]
    assert "since start" in n.sent[0]
    assert await digest.run_digest(n, now=w0 + 2 * step + 50) == 0  # a restart in the same window: nothing again
    assert len(n.sent) == 1


async def test_first_digest_run_does_not_backfill(fresh_db: str) -> None:
    n = FakeNotifier()
    assert await digest.run_digest(n, now=NOW) == 0 and n.sent == []


async def test_alarm_repeats_at_most_hourly(fresh_db: str) -> None:
    n = FakeNotifier()
    assert await digest.send_alarm(n, "X", "d", now=NOW)
    assert not await digest.send_alarm(n, "X", "d", now=NOW + 3599)
    assert await digest.send_alarm(n, "X", "d", now=NOW + 3600)
    assert all(m.startswith("🚨") for m in n.sent)


async def test_closed_message_shows_the_dollar_result(fresh_db: str) -> None:
    await add_wallet()
    await add_signal()
    kf, market, copier = setup()
    await copier.handle_pending(now=NOW + 1)
    kf.marks["ETHUSDTM"] = 2000.0 * 1.0601
    kf.books["ETHUSDTM"] = book(2000.0 * 1.0601)
    await risk.mark_and_exit(kf, market, copier.executor, now=NOW + 120)  # type: ignore[arg-type]
    kf.marks["ETHUSDTM"] = 2000.0 * 1.0601 * 0.97
    kf.books["ETHUSDTM"] = book(2000.0 * 1.0601 * 0.97)
    closed = await risk.mark_and_exit(kf, market, copier.executor, now=NOW + 180)  # type: ignore[arg-type]
    assert [c.reason for c in closed] == [fp.TRAIL]
    text = await messages.digest(NOW, NOW + 900)
    assert text is not None and "CLOSE #1" in text and "TRAIL" in text and "on margin" in text


async def test_kill_switch_closes_everything(fresh_db: str) -> None:
    await add_wallet()
    await add_signal()
    kf, market, copier = setup()
    await copier.handle_pending(now=NOW + 1)
    open(settings.stop_file, "w").close()
    assert risk.kill_switch_on()
    closed = await risk.close_everything(kf, market, copier.executor)  # type: ignore[arg-type]
    assert [c.reason for c in closed] == [risk.KILL_SWITCH]


async def test_insufficient_margin_is_refused(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "position_fraction", 0.4)
    monkeypatch.setattr(settings, "max_open_positions", 10)
    _, market, copier = setup()
    for i in range(4):
        w = f"0x{i:040x}"
        await add_wallet(w)
        c = contract(symbol=f"C{i}USDTM", base=f"C{i}")
        market.contracts[c.symbol] = c
        market.mapping[f"C{i}"] = type(market.mapping.get("ETH"))(f"C{i}", c, 1.0)  # type: ignore[misc]
        copier.kf.books[c.symbol] = book(2000.0, symbol=c.symbol)  # type: ignore[attr-defined]
        await add_signal(coin=f"C{i}", wallet=w, ts=time.time())
    await copier.handle_pending()
    async with store.session() as conn:
        async with conn.execute("SELECT refused FROM signals ORDER BY id") as cur:
            refused = [r["refused"] for r in await cur.fetchall()]
    # 40% margin each ($4,000 + fees): two fit in $10,000, the third does not.
    assert refused[:2] == ["", ""] and "INSUFFICIENT_MARGIN" in refused[2:]


def test_every_message_names_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both bots post to one Telegram chat (split 2026-11-14): the stamp says which bot, escaped like any value."""
    assert messages.stamp() == "[PAPER] Jupiter-KuCoin"
    assert "[PAPER] Jupiter-KuCoin" in messages.alarm("Kill switch", "x")
    monkeypatch.setattr(settings, "bot_name", "<b>x</b>")
    assert messages.stamp() == "[PAPER] &lt;b&gt;x&lt;/b&gt;"
