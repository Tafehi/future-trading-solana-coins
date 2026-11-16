"""Signed KuCoin Futures REST client (live mode only).

Signing, checked against KuCoin's docs (docs-new/authentication) and its official SDK
(kucoin-universal-sdk `default_signer.py`) on 2026-11-13:
- prehash  = timestamp_ms + METHOD + endpoint (path plus `?query`) + body (the exact JSON string sent)
- KC-API-SIGN       = base64(HMAC-SHA256(secret, prehash))
- KC-API-PASSPHRASE = base64(HMAC-SHA256(secret, passphrase))      (key version 2 and 3)
- KC-API-KEY-VERSION = KUCOIN_KEY_VERSION (default "3", what the SDK sends)

Endpoints (paths from the official SDK's generated API files, 2026-11-13):
- POST   /api/v1/orders                      place an order (market, or a stop when `stop` is set)
- GET    /api/v1/orders/{orderId}            one order
- DELETE /api/v1/orders/{orderId}            cancel
- GET    /api/v1/fills?orderId=              fills of one order (price, size, value, fee)
- GET    /api/v1/positions?currency=USDT     open positions
- GET    /api/v1/account-overview?currency=USDT
- GET    /api/v2/position/getMarginMode?symbol=   POST /api/v2/position/changeMarginMode

Response field names used here (`currentQty`, `avgEntryPrice`, `accountEquity`, ...) are from KuCoin's docs; the
first live trade must confirm them (BUG.md B-5).
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.data.http import DataError, RateLimiter
from app.utils.logging import get_logger

logger = get_logger(__name__)

OK = "200000"
# KuCoin's authentication error codes (missing headers, bad key/sign/passphrase/timestamp, IP or permission).
AUTH_CODES = frozenset({"400001", "400002", "400003", "400004", "400005", "400006", "400007", "411100"})


def sign(secret: str, message: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()


def headers(
    *, key: str, secret: str, passphrase: str, version: str, method: str, endpoint: str, body: str, ts_ms: int
) -> dict[str, str]:
    """The five KC-API headers for one request. Pure, so the signature can be tested against a known value."""
    ts = str(ts_ms)
    return {
        "KC-API-KEY": key,
        "KC-API-SIGN": sign(secret, ts + method.upper() + endpoint + body),
        "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": sign(secret, passphrase),
        "KC-API-KEY-VERSION": version,
        "Content-Type": "application/json",
    }


class AuthError(DataError):
    """KuCoin refused the credentials (bad key, passphrase, permission or IP)."""


class KucoinPrivate:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, clock: Any = time.time) -> None:
        self._client = httpx.AsyncClient(timeout=settings.http_timeout_s, transport=transport)
        self._limiter = RateLimiter(5.0)
        self._clock = clock
        self.base = settings.kucoin_futures_url

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> Any:
        await self._limiter.acquire()
        endpoint = path + (f"?{urlencode(params)}" if params else "")
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        hdrs = headers(
            key=settings.kucoin_api_key,
            secret=settings.kucoin_api_secret,
            passphrase=settings.kucoin_api_passphrase,
            version=settings.kucoin_key_version,
            method=method,
            endpoint=endpoint,
            body=raw,
            ts_ms=int(self._clock() * 1000),
        )
        try:
            resp = await self._client.request(method, self.base + endpoint, content=raw or None, headers=hdrs)
        except httpx.HTTPError as exc:
            raise DataError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise DataError(f"{method} {path}: HTTP {resp.status_code}, unreadable body") from exc
        code = str(data.get("code")) if isinstance(data, dict) else ""
        if resp.status_code in (401, 403) or code in AUTH_CODES:
            raise AuthError(f"{method} {path}: KuCoin auth refused: {str(data)[:200]}")
        if code != OK:
            raise DataError(f"{method} {path}: KuCoin error {str(data)[:300]}")
        return data.get("data")

    # ── Read ─────────────────────────────────────────────────────────────────────────────
    async def account_overview(self) -> dict[str, Any]:
        return dict(await self.request("GET", "/api/v1/account-overview", params={"currency": "USDT"}) or {})

    async def positions(self) -> list[dict[str, Any]]:
        rows = await self.request("GET", "/api/v1/positions", params={"currency": "USDT"}) or []
        return [r for r in rows if float(r.get("currentQty") or 0) != 0]

    async def order(self, order_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", f"/api/v1/orders/{order_id}") or {})

    async def fills(self, order_id: str) -> list[dict[str, Any]]:
        data = await self.request("GET", "/api/v1/fills", params={"orderId": order_id}) or {}
        return list(data.get("items") or []) if isinstance(data, dict) else list(data)

    async def margin_mode(self, symbol: str) -> str:
        data = await self.request("GET", "/api/v2/position/getMarginMode", params={"symbol": symbol}) or {}
        return str(data.get("marginMode") or "")

    # ── Write ────────────────────────────────────────────────────────────────────────────
    async def set_isolated(self, symbol: str) -> None:
        await self.request(
            "POST", "/api/v2/position/changeMarginMode", body={"symbol": symbol, "marginMode": "ISOLATED"}
        )

    async def market_order(
        self, *, client_oid: str, symbol: str, side: str, size: int, leverage: int, reduce_only: bool = False
    ) -> str:
        body: dict[str, Any] = {
            "clientOid": client_oid,
            "symbol": symbol,
            "side": side,
            "type": "market",
            "size": size,
            "marginMode": "ISOLATED",
        }
        if reduce_only:
            body["reduceOnly"] = True
        else:
            body["leverage"] = leverage
        data = await self.request("POST", "/api/v1/orders", body=body) or {}
        return str(data.get("orderId") or "")

    async def stop_order(self, *, client_oid: str, symbol: str, side: str, stop: str, stop_px: float) -> str:
        """A reduce-only stop-market that closes the whole position when the MARK price crosses `stop_px`."""
        body = {
            "clientOid": client_oid,
            "symbol": symbol,
            "side": side,
            "type": "market",
            "stop": stop,
            "stopPriceType": "MP",
            "stopPrice": f"{stop_px:.10g}",
            "closeOrder": True,
            "marginMode": "ISOLATED",
        }
        data = await self.request("POST", "/api/v1/orders", body=body) or {}
        return str(data.get("orderId") or "")

    async def cancel(self, order_id: str) -> None:
        await self.request("DELETE", f"/api/v1/orders/{order_id}")
