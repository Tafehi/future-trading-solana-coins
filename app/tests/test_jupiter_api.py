"""Jupiter's trade API (D-29): paging, lifetimes from trades, real-data agreement with the chain decoder, and the
liquidation fee the chain decoder used to miss (DEBUG D-12). No network: MockTransport and a real anonymised answer."""

import json
from typing import Any

import httpx
import pytest

from app.data import jupiter_api as ja
from app.data import jupiter_events as je
from app.data.http import DataError
from app.tests.conftest import fixture

OWNER = "fixture-owner"
PROG = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"


def tr(action: str, size: float, t: int, *, key: str = "P1", px: float = 100.0, pnl: float | None = None,
       fee: float = 1.0, order: str = "Market", owner: str = OWNER,
       name: str = "SOL-PERP") -> dict[str, Any]:  # fmt: skip
    return {"owner": owner, "positionPubkey": key, "positionName": name, "side": "long", "action": action,
            "orderType": order, "size": str(size), "price": str(px), "pnl": None if pnl is None else str(pnl),
            "fee": str(fee), "createdTime": t, "txHash": f"h{t}{action}"}  # fmt: skip


# --- lifetimes from trades --------------------------------------------------------------------------------------
def test_open_add_partial_and_full_close_make_one_lifetime() -> None:
    trades = [tr("Increase", 1000, 10, px=100), tr("Increase", 1000, 20, px=110),
              tr("Decrease", 500, 30, px=120, pnl=50), tr("Decrease", 1500, 40, px=130, pnl=300)]  # fmt: skip
    (e,) = ja.episodes_from_trades(OWNER, trades)
    assert (e.opened_at, e.closed_at, e.size_usd) == (10.0, 40.0, 2000.0)
    assert e.entry_px == pytest.approx(105.0) and e.exit_px == 130.0  # size-weighted average of the opens
    assert e.pnl == pytest.approx(350 - 4 * 1.0)  # every fee of the lifetime, open fees included


def test_a_still_open_position_is_no_lifetime() -> None:
    assert ja.episodes_from_trades(OWNER, [tr("Increase", 1000, 10), tr("Decrease", 400, 20, pnl=5)]) == []


def test_flat_at_exactly_the_tolerance_closes() -> None:
    """Closed when what is left is at most max($1, 0.1% of the peak); a cent more stays open."""
    left = max(ja.FLAT_USD, ja.FLAT_SHARE * 10_000)  # $10 on a $10,000 position
    at = [tr("Increase", 10_000, 10), tr("Decrease", 10_000 - left, 20, pnl=1)]
    above = [tr("Increase", 10_000, 10), tr("Decrease", 10_000 - left - 0.01, 20, pnl=1)]
    assert len(ja.episodes_from_trades(OWNER, at)) == 1
    assert ja.episodes_from_trades(OWNER, above) == []


def test_a_liquidation_closes_whatever_is_left() -> None:
    trades = [tr("Increase", 1000, 10), tr("Decrease", 300, 20, pnl=-290, fee=5, order="Liquidation")]
    (e,) = ja.episodes_from_trades(OWNER, trades)
    assert e.pnl == pytest.approx(-290 - 6)


def test_window_starts_at_exactly_since() -> None:
    trades = [tr("Increase", 100, 10), tr("Decrease", 100, 50, pnl=1)]
    assert len(ja.episodes_from_trades(OWNER, trades, since=50)) == 1
    assert ja.episodes_from_trades(OWNER, trades, since=51) == []


def test_other_owners_other_markets_and_orphan_closes_are_ignored() -> None:
    trades = [tr("Decrease", 100, 5, pnl=9),  # its open is older than the answer: no full lifetime
              tr("Increase", 100, 10, owner="someone-else"), tr("Decrease", 100, 20, pnl=1, owner="someone-else"),
              tr("Increase", 100, 10, name="DOGE-PERP"), tr("Decrease", 100, 20, pnl=1, name="DOGE-PERP")]  # fmt: skip
    assert ja.episodes_from_trades(OWNER, trades) == []


def test_open_and_close_in_the_same_second() -> None:
    trades = [tr("Decrease", 100, 10, pnl=2), tr("Increase", 100, 10)]  # listed close first, as the API may
    assert len(ja.episodes_from_trades(OWNER, trades)) == 1


# --- paging -------------------------------------------------------------------------------------------------------
def _api(pages: dict[int, dict[str, Any]], seen: list[dict[str, Any]]) -> ja.JupiterApi:
    def h(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=pages[int(req.url.params["start"])])

    return ja.JupiterApi(rps=1000, transport=httpx.MockTransport(h))


async def test_paging_reads_every_page_and_drops_border_repeats() -> None:
    rows = [tr("Increase", 1, t) for t in range(1500)]
    pages = {
        0: {"count": 1500, "dataList": rows[:1000]},
        1000: {"count": 1500, "dataList": [rows[999]] + rows[1000:1499]},
    }  # fmt: skip  (row 999 repeats)
    seen: list[dict[str, Any]] = []
    api = _api(pages, seen)
    h = await api.history(OWNER)
    await api.close()
    assert [p["start"] for p in seen] == ["0", "1000"] and [p["end"] for p in seen] == ["1000", "2000"]
    assert len(h.trades) == 1499 and h.complete


async def test_a_short_first_page_is_one_call() -> None:
    seen: list[dict[str, Any]] = []
    api = _api({0: {"count": 3, "dataList": [tr("Increase", 1, t) for t in range(3)]}}, seen)
    h = await api.history(OWNER)
    await api.close()
    assert len(seen) == 1 and len(h.trades) == 3 and h.complete


async def test_more_than_max_pages_is_marked_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ja, "MAX_PAGES", 2)
    pages = {s: {"count": 5000, "dataList": [tr("Increase", 1, s + i) for i in range(1000)]} for s in (0, 1000)}
    api = _api(pages, [])
    h = await api.history(OWNER)
    await api.close()
    assert len(h.trades) == 2000 and not h.complete


async def test_an_error_answer_is_a_data_error() -> None:
    api = ja.JupiterApi(rps=1000, transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))
    with pytest.raises(DataError):
        await api.history(OWNER)
    await api.close()


# --- real data (2026-11-18): the API and the chain decoder agree, once the liquidation fee is counted ----------
def test_real_owner_lifetimes_from_the_api() -> None:
    doc = fixture("jupiter_api_trades.json")
    eps = ja.episodes_from_trades(OWNER, doc["trades"])
    assert len(eps) == 20  # the chain decoder found the same 20 lifetimes (verified 2026-11-18)
    assert sum(e.pnl for e in eps) == pytest.approx(-2828.53, abs=0.01)
    assert sum(1 for t in doc["trades"] if t["orderType"] == "Liquidation") == 4


def test_the_liquidation_fee_is_counted_like_the_api_does() -> None:
    """DEBUG D-12: on 4 real liquidations the decoder's fee = the API's fee (base + borrow + liquidation + impact)."""
    doc = fixture("jupiter_api_trades.json")
    api = {t["txHash"]: t for t in doc["trades"]}
    checked = 0
    for tx in doc["liquidation_txs"]:
        (ev,) = je.events_from_transaction(tx, PROG)
        row = api[tx["tx"]]
        assert ev.name == je.LIQUIDATION
        assert ev.fee_usd == pytest.approx(float(row["fee"]), abs=0.011)
        assert ev.pnl_usd == pytest.approx(float(row["pnl"]), abs=0.011)
        assert float(row["liquidationFee"]) > 0
        checked += 1
    assert checked == 4


def test_fee_is_the_sum_of_its_parts_in_the_real_answer() -> None:
    for t in fixture("jupiter_api_trades.json")["trades"]:
        parts = sum(float(t[k]) for k in ("baseFee", "borrowFee", "liquidationFee", "priceImpactFee"))
        assert parts == pytest.approx(float(t["fee"]), abs=0.011)


def test_fixture_holds_no_real_keys() -> None:
    text = json.dumps(fixture("jupiter_api_trades.json")["trades"])
    assert '"mint"' not in text and "fixture-owner" in text


async def test_exactly_one_full_page_is_one_call() -> None:
    """count == 1,000 and a full page: done, no second call (it would ask for an offset that has nothing)."""
    seen: list[dict[str, Any]] = []
    api = _api({0: {"count": 1000, "dataList": [tr("Increase", 1, t) for t in range(1000)]}}, seen)
    h = await api.history(OWNER)
    await api.close()
    assert len(seen) == 1 and len(h.trades) == 1000 and h.complete
