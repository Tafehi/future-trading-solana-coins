"""Position diffs, the baseline rule, and the copy trigger."""

from app.core import signals as s
from app.data.hyperliquid import WalletPosition

W = "0x" + "1" * 40


def p(coin: str, size: float, px: float = 100.0) -> WalletPosition:
    return WalletPosition(coin, size, px, 5.0)


def test_the_first_snapshot_is_a_baseline_and_copies_nothing() -> None:
    assert s.diff(W, None, [p("ETH", 1)]) == []


def test_open_close_add_reduce() -> None:
    prev = {"ETH": p("ETH", 1), "BTC": p("BTC", -2)}
    kinds = {(e.coin, e.kind) for e in s.diff(W, prev, [p("ETH", 2), p("SOL", 3)])}
    assert kinds == {("ETH", s.ADD), ("BTC", s.CLOSE), ("SOL", s.OPEN)}
    assert [e.kind for e in s.diff(W, {"ETH": p("ETH", 2)}, [p("ETH", 1)])] == [s.REDUCE]


def test_a_flip_is_close_then_open() -> None:
    evs = s.diff(W, {"ETH": p("ETH", 1)}, [p("ETH", -1)])
    assert [(e.kind, e.side) for e in evs] == [(s.CLOSE, s.LONG), (s.OPEN, s.SHORT)]


def open_sig(wallet: str = W, coin: str = "ETH", side: str = s.LONG) -> s.Signal:
    return s.Signal(wallet, coin, s.OPEN, side, 1.0, 100.0, 5.0)
