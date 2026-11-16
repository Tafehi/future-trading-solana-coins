"""Paper futures arithmetic, with boundary values tested AT the boundary."""

import pytest

from app.data.kucoin import OrderBook
from app.tests.helpers import book
from app.trading import futures_paper as fp


def test_contracts_round_down_to_whole_lots() -> None:
    # $1000 at $2000 with 0.01 ETH per contract = 50 contracts exactly; a hair less is 49.
    assert fp.contracts_for(1000.0, 2000.0, 0.01, 1) == 50
    assert fp.contracts_for(999.99, 2000.0, 0.01, 1) == 49
    assert fp.contracts_for(1000.0, 2000.0, 0.01, 10) == 50
    assert fp.contracts_for(999.0, 2000.0, 0.01, 10) == 40


def test_walk_pays_the_spread_and_refuses_a_thin_book() -> None:
    b = book(2000.0, spread=1.0, depth=10)
    fill = fp.walk(b, buy=True, size=15, max_slippage_bps=100)
    assert fill.avg_px > b.mid
    with pytest.raises(fp.FillRefused) as exc:
        fp.walk(b, buy=True, size=10_000, max_slippage_bps=100)
    assert exc.value.code == "THIN_BOOK"


def test_walk_refuses_too_much_slippage() -> None:
    thin = OrderBook("X", [(99.0, 100)], [(101.0, 1), (110.0, 100)])
    with pytest.raises(fp.FillRefused) as exc:
        fp.walk(thin, buy=True, size=50, max_slippage_bps=50)
    assert exc.value.code == "SLIPPAGE"


def test_liquidation_price_long_and_short() -> None:
    assert fp.liquidation_price(fp.LONG, 100.0, 10, 0.005) == pytest.approx(90.5)
    assert fp.liquidation_price(fp.SHORT, 100.0, 10, 0.005) == pytest.approx(109.5)


def test_a_mark_exactly_at_the_liquidation_price_liquidates() -> None:
    assert fp.is_liquidated(fp.LONG, 90.5, 90.5)
    assert not fp.is_liquidated(fp.LONG, 90.51, 90.5)
    assert fp.is_liquidated(fp.SHORT, 109.5, 109.5)
    assert not fp.is_liquidated(fp.SHORT, 109.49, 109.5)


def test_funding_sign_longs_pay_shorts_receive_on_a_positive_rate() -> None:
    assert fp.funding_received(fp.LONG, 1.0, 1000.0, 0.0001) == pytest.approx(-0.1)
    assert fp.funding_received(fp.SHORT, 1.0, 1000.0, 0.0001) == pytest.approx(0.1)
    assert fp.funding_received(fp.LONG, 1.0, 1000.0, -0.0001) == pytest.approx(0.1)


def _exit(**kw: float | str | None) -> str:
    base: dict[str, float | str | None] = dict(
        side=fp.LONG, entry=100.0, best_px=100.0, mark=100.0, liq_px=90.5, opened_at=0.0, now=10.0,
        stop_pct=10.0, trail_activate_pct=8.0, trail_giveback_pct=4.0, max_hold_hours=168.0,
    )  # fmt: skip
    base.update(kw)
    return fp.exit_reason(**base)  # type: ignore[arg-type]


def test_stop_fires_exactly_at_the_stop_distance() -> None:
    assert _exit(mark=90.0, liq_px=None) == fp.STOP_LOSS
    assert _exit(mark=90.01, liq_px=None) == ""
    assert _exit(side=fp.SHORT, mark=110.0, liq_px=None) == fp.STOP_LOSS


def test_liquidation_wins_over_the_stop() -> None:
    # 10x long: liquidation at 90.5 comes before the 10% stop at 90.
    assert _exit(mark=90.5) == fp.LIQUIDATED


def test_trail_needs_activation_then_the_giveback() -> None:
    assert _exit(best_px=107.9, mark=103.0) == ""  # never armed
    assert _exit(best_px=110.0, mark=105.6) == fp.TRAIL  # 4% back from 110
    assert _exit(best_px=110.0, mark=105.7) == ""
    assert _exit(side=fp.SHORT, best_px=90.0, mark=93.6, liq_px=None) == fp.TRAIL


def test_max_hold_at_exactly_the_limit() -> None:
    assert _exit(now=168 * 3600.0) == fp.MAX_HOLD
    assert _exit(now=168 * 3600.0 - 1) == ""


def test_exit_fill_never_refuses_and_is_pessimistic_past_the_book() -> None:
    thin = OrderBook("X", [(100.0, 1.0)], [(101.0, 1.0)])
    px = fp.exit_fill(thin, buy=False, size=2.0, fallback_px=100.0)
    assert px == pytest.approx((100.0 + 99.0) / 2)
    assert fp.exit_fill(OrderBook("X", [], []), buy=False, size=1.0, fallback_px=77.0) == 77.0
