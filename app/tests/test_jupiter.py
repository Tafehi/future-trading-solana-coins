"""Jupiter Perps Position decoder, checked against REAL mainnet accounts (DEBUG D-2). Used by the wallet tracer."""

import base64
import hashlib
import struct

import pytest

from app.data import jupiter_perps as jp
from app.tests.conftest import fixture

PROG = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"


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
