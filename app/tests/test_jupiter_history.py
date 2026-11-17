"""Jupiter Perps close events and episodes, decoded from REAL mainnet transactions (DEBUG D-5). Used by the wallet
tracer for Solana futures."""

import pytest

from app.data import jupiter_events as je
from app.tests.conftest import fixture

PROG = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"


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
