"""Order rules and the paper executor: paper refuses what KuCoin would refuse, and fills on a FRESH book."""

import pytest

from app.config import settings
from app.data.kucoin import parse_funding_history
from app.tests.conftest import fixture
from app.tests.helpers import FakeKucoin, book, contract
from app.trading import futures_paper as fp
from app.trading.executor import check_rules
from app.trading.paper_executor import PaperExecutor


def test_funding_history_parses_oldest_first() -> None:
    fh = parse_funding_history(fixture("kucoin_funding_history.json"))
    assert fh == sorted(fh) and len(fh) >= 3


def test_rules_accept_a_normal_order() -> None:
    check_rules(contract(), contracts=10, leverage=3, price=2000.0)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"contracts": 0.0}, "SIZE_ZERO"),
        ({"contracts": 0.5}, "SIZE_ZERO"),
        ({"contracts": 1.5}, "BAD_LOT"),
        ({"contracts": 1_000_001}, "MAX_ORDER_QTY"),
        ({"leverage": 101}, "MAX_LEVERAGE"),
    ],
)
def test_rules_refuse_what_kucoin_refuses(kwargs: dict[str, float], code: str) -> None:
    args = {"contracts": 10.0, "leverage": 3, "price": 2000.0} | kwargs
    with pytest.raises(fp.FillRefused) as err:
        check_rules(contract(), **args)  # type: ignore[arg-type]
    assert err.value.code == code


def test_risk_limit_boundary_value() -> None:
    """Exactly at the first risk-limit level passes; one lot more is refused."""
    c = contract(max_risk_limit=20_000.0)  # 1 lot = 0.01 ETH x 2000 = $20
    check_rules(c, contracts=1000, leverage=3, price=2000.0)  # $20,000 exactly
    with pytest.raises(fp.FillRefused) as err:
        check_rules(c, contracts=1001, leverage=3, price=2000.0)
    assert err.value.code == "RISK_LIMIT"


def test_max_leverage_boundary_value() -> None:
    check_rules(contract(max_leverage=3), contracts=1, leverage=3, price=2000.0)
    with pytest.raises(fp.FillRefused):
        check_rules(contract(max_leverage=3), contracts=1, leverage=4, price=2000.0)


async def test_paper_waits_then_reads_a_fresh_book() -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    kf = FakeKucoin(books={"ETHUSDTM": book(2000.0)})
    ex = PaperExecutor(kf, delay_s=1.5, sleep=sleep)  # type: ignore[arg-type]
    fill = await ex.open(contract(), side=fp.LONG, contracts=10, leverage=3)
    assert slept == [1.5]
    assert kf.calls == [("book", "ETHUSDTM")]
    assert fill.avg_px == pytest.approx(2000.1)  # the best ask: a buy pays the spread
    assert fill.fee == pytest.approx(10 * 0.01 * 2000.1 * 0.0006)


async def test_paper_refuses_a_thin_book() -> None:
    kf = FakeKucoin(books={"ETHUSDTM": book(2000.0, depth=1.0)})
    ex = PaperExecutor(kf, delay_s=0)  # type: ignore[arg-type]
    with pytest.raises(fp.FillRefused) as err:
        await ex.open(contract(), side=fp.LONG, contracts=100, leverage=3)
    assert err.value.code == "THIN_BOOK"


async def test_paper_refuses_slippage_above_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_slippage_bps", 0.1)
    kf = FakeKucoin(books={"ETHUSDTM": book(2000.0, spread=2.0)})
    ex = PaperExecutor(kf, delay_s=0)  # type: ignore[arg-type]
    with pytest.raises(fp.FillRefused) as err:
        await ex.open(contract(), side=fp.LONG, contracts=1, leverage=3)
    assert err.value.code == "SLIPPAGE"


def test_stop_fires_at_exactly_six_percent_and_liquidation_wins() -> None:
    base = dict(
        side=fp.LONG, entry=100.0, best_px=100.0, opened_at=0.0, now=10.0, stop_pct=6.0,
        trail_activate_pct=6.0, trail_giveback_pct=3.0, max_hold_hours=72.0,
    )  # fmt: skip
    assert fp.exit_reason(mark=94.0, liq_px=None, **base) == fp.STOP_LOSS  # type: ignore[arg-type]
    assert fp.exit_reason(mark=94.0001, liq_px=None, **base) == ""  # type: ignore[arg-type]
    # A mark at the liquidation price liquidates, even though it is also past the stop.
    assert fp.exit_reason(mark=67.0, liq_px=67.0, **base) == fp.LIQUIDATED  # type: ignore[arg-type]
