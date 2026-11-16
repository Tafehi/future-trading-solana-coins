"""The signed KuCoin client: headers, signature formula, and error mapping. Offline (MockTransport)."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.config import settings
from app.data.http import DataError
from app.data.kucoin_private import AuthError, KucoinPrivate, headers
from app.trading.kucoin_executor import sum_fills


def _ref_sign(secret: str, msg: str) -> str:
    """KuCoin's documented formula, written out independently: base64(HMAC-SHA256(secret, msg))."""
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def test_headers_follow_the_documented_prehash() -> None:
    h = headers(
        key="KEY", secret="SECRET", passphrase="PASS", version="3", method="post",
        endpoint="/api/v1/orders", body='{"a":1}', ts_ms=1_700_000_000_000,
    )  # fmt: skip
    assert h["KC-API-SIGN"] == _ref_sign("SECRET", '1700000000000POST/api/v1/orders{"a":1}')
    assert h["KC-API-PASSPHRASE"] == _ref_sign("SECRET", "PASS")  # key version 2/3: the passphrase is signed too
    assert h["KC-API-TIMESTAMP"] == "1700000000000" and h["KC-API-KEY-VERSION"] == "3" and h["KC-API-KEY"] == "KEY"


def test_signature_pinned_value() -> None:
    """A fixed value, so a change to the formula cannot slip through unnoticed."""
    h = headers(key="k", secret="s", passphrase="p", version="3", method="GET",
                endpoint="/api/v1/positions?currency=USDT", body="", ts_ms=1)  # fmt: skip
    assert h["KC-API-SIGN"] == "zzIKFBlb/JWuwy2TAIg99TE6SEq5hWMp9vJ5tVx7DH0="


async def test_query_and_body_are_signed_exactly_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kucoin_api_secret", "SECRET")
    monkeypatch.setattr(settings, "kucoin_api_passphrase", "PASS")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"code": "200000", "data": {"orderId": "o1"}})

    client = KucoinPrivate(transport=httpx.MockTransport(handler), clock=lambda: 1.0)
    oid = await client.market_order(client_oid="c", symbol="ETHUSDTM", side="buy", size=3, leverage=3)
    await client.close()
    assert oid == "o1"
    req = seen[0]
    body = req.content.decode()
    assert json.loads(body)["leverage"] == 3 and json.loads(body)["marginMode"] == "ISOLATED"
    assert req.headers["KC-API-SIGN"] == _ref_sign("SECRET", "1000POST/api/v1/orders" + body)


async def test_auth_failure_raises_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "400003", "msg": "KC-API-KEY not exists"})

    client = KucoinPrivate(transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError):
        await client.account_overview()
    await client.close()


async def test_other_error_raises_data_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "300000", "msg": "order size too small"})

    client = KucoinPrivate(transport=httpx.MockTransport(handler))
    with pytest.raises(DataError) as err:
        await client.positions()
    assert not isinstance(err.value, AuthError)
    await client.close()


def test_sum_fills_average_and_fee() -> None:
    rows = [{"size": 2, "value": "40.0", "fee": "0.024"}, {"size": "3", "value": 60.3, "fee": 0.036}]
    lots, avg, fee = sum_fills(rows, 0.01)
    assert lots == 5 and avg == pytest.approx(100.3 / 0.05) and fee == pytest.approx(0.06)
