"""Parsers against recorded real responses, and the coin mapping."""

import pytest

from app.data import symbol_map
from app.data.http import DataError
from app.data.hyperliquid import parse_fills, parse_positions
from app.data.kucoin import parse_book, parse_contracts
from app.tests.conftest import fixture
from app.tests.helpers import contract


def test_contracts_parse_with_maintenance_margin_and_absolute_funding_time() -> None:
    cs = {c.symbol: c for c in parse_contracts(fixture("kucoin_contracts.json"))}
    eth = cs["ETHUSDTM"]
    assert eth.base == "ETH" and eth.multiplier == 0.01 and eth.mmr == 0.005
    assert eth.taker_fee == 0.0006
    assert eth.next_funding_ms > 1_700_000_000_000  # absolute epoch ms, not "ms until"
    assert cs["XBTUSDTM"].base == "XBT"


def test_a_kucoin_error_body_is_an_error_not_an_empty_market() -> None:
    with pytest.raises(DataError):
        parse_contracts({"code": "400100", "msg": "bad"})


def test_book_levels_are_sorted_best_first() -> None:
    b = parse_book(fixture("kucoin_depth20.json"), "XBTUSDTM")
    assert b.bids[0][0] > b.bids[-1][0]
    assert b.asks[0][0] < b.asks[-1][0]
    assert b.best_ask > b.best_bid and b.spread_bps < 5


def test_positions_signed_size_and_leverage() -> None:
    pos = parse_positions(fixture("hl_clearinghouse.json"))
    btc = next(p for p in pos if p.coin == "BTC")
    assert btc.size < 0 and btc.leverage == 3 and btc.entry_px > 0


def test_positions_refuse_a_body_without_asset_positions() -> None:
    with pytest.raises(DataError):
        parse_positions({"error": "rate limited"})


def test_fills_parse() -> None:
    fills = parse_fills(fixture("hl_fills.json"))
    assert len(fills) == 300 and all(f.side in ("A", "B") for f in fills)


def test_symbol_map_rules() -> None:
    cs = [
        contract("XBTUSDTM", base="XBT"),
        contract("PEPEUSDTM", base="PEPE"),
        contract("1000BONKUSDTM", base="1000BONK"),
        contract("ETHUSDTM", base="ETH"),
    ]
    m = symbol_map.build(["BTC", "kPEPE", "kBONK", "ETH", "NOPE"], cs)
    assert m["BTC"].symbol == "XBTUSDTM" and m["BTC"].price_scale == 1.0
    assert m["kPEPE"].symbol == "PEPEUSDTM" and m["kPEPE"].price_scale == 0.001
    assert m["kBONK"].symbol == "1000BONKUSDTM" and m["kBONK"].price_scale == 1.0
    assert "NOPE" not in m


def test_jupiter_coins_map_to_kucoin_contracts() -> None:
    """The only coins this bot copies (SOL, ETH, BTC) all map, BTC to XBTUSDTM (D-19: no Hyperliquid coin list)."""
    from app.data.market import JUPITER_COINS, MarketCache

    market = MarketCache()
    market.load([contract("XBTUSDTM", base="XBT"), contract("ETHUSDTM", base="ETH"), contract("SOLUSDTM", base="SOL")])
    assert set(market.mapping) == set(JUPITER_COINS) and market.mapping["BTC"].symbol == "XBTUSDTM"
