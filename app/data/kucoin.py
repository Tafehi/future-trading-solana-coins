"""KuCoin public futures market data: contracts, order books, mark price and funding (ported from ai-kucoin-future).

Public endpoints, no key. Every response carries `code == "200000"` on success; anything else raises
`DataError` so a KuCoin error body is never read as an empty market.

Sizes on a futures book are in contracts; one contract is `multiplier` units of the base coin.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.data.http import DataError, RateLimiter, get_json

OK = "200000"


@dataclass(frozen=True)
class Contract:
    symbol: str
    base: str
    multiplier: float
    lot_size: float
    tick_size: float
    max_leverage: float
    taker_fee: float
    maker_fee: float
    mmr: float  # maintenance margin rate
    funding_rate: float  # current interval's rate
    funding_interval_ms: int
    next_funding_ms: int  # absolute epoch ms
    mark_price: float
    turnover_24h: float
    status: str
    max_order_qty: float = 0.0  # in lots; 0 = unknown
    open_interest: float = 0.0  # in lots, as KuCoin reports it
    index_price: float = 0.0
    # Largest position value (USDT) allowed at the first isolated risk-limit level. Paper refuses an order above it,
    # as KuCoin would unless the risk level is raised (we never raise it). 0 = unknown, no check.
    max_risk_limit: float = 0.0


@dataclass(frozen=True)
class OrderBook:
    """Levels as (price, size). Bids best first (highest), asks best first (lowest)."""

    symbol: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    ts_ms: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.best_ask - self.best_bid) / mid * 10_000.0 if mid > 0 else float("inf")


def _data(body: Any, what: str) -> Any:
    if not isinstance(body, dict) or str(body.get("code")) != OK:
        raise DataError(f"{what}: KuCoin error body {str(body)[:200]!r}")
    return body.get("data")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def parse_contracts(body: Any) -> list[Contract]:
    out: list[Contract] = []
    for c in _data(body, "contracts/active") or []:
        try:
            symbol = str(c["symbol"])
            if not symbol.endswith("USDTM") or c.get("isInverse"):
                continue
            mmr = _f(c.get("maintainMargin")) or _f(c.get("maintainMarginRate")) or _f(c.get("initialMargin")) / 2
            out.append(
                Contract(
                    symbol=symbol,
                    base=str(c["baseCurrency"]),
                    multiplier=float(c["multiplier"]),
                    lot_size=float(c.get("lotSize") or 1),
                    tick_size=_f(c.get("tickSize")),
                    max_leverage=_f(c.get("maxLeverage"), 1.0),
                    taker_fee=_f(c.get("takerFeeRate"), 0.0006),
                    maker_fee=_f(c.get("makerFeeRate"), 0.0002),
                    mmr=mmr,
                    funding_rate=_f(c.get("fundingFeeRate")),
                    funding_interval_ms=int(
                        c.get("currentFundingRateGranularity") or c.get("fundingRateGranularity") or 28_800_000
                    ),
                    next_funding_ms=int(c.get("nextFundingRateDateTime") or 0),
                    mark_price=_f(c.get("markPrice")),
                    turnover_24h=_f(c.get("turnoverOf24h")),
                    status=str(c.get("status", "")),
                    max_order_qty=_f(c.get("maxOrderQty")),
                    open_interest=_f(c.get("openInterest")),
                    index_price=_f(c.get("indexPrice")),
                    max_risk_limit=_f(c.get("maxRiskLimit")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_book(body: Any, symbol: str) -> OrderBook:
    data = _data(body, f"book {symbol}") or {}
    try:
        bids = [(float(p), float(s)) for p, s, *_ in data.get("bids") or []]
        asks = [(float(p), float(s)) for p, s, *_ in data.get("asks") or []]
    except (TypeError, ValueError) as exc:
        raise DataError(f"book {symbol}: unreadable levels: {exc}") from exc
    bids.sort(key=lambda lv: -lv[0])
    asks.sort(key=lambda lv: lv[0])
    return OrderBook(symbol, bids, asks, int(data.get("ts") or data.get("time") or 0))


def parse_funding_history(body: Any) -> list[tuple[int, float]]:
    """(timepoint ms, rate), oldest first."""
    rows = _data(body, "funding history") or []
    out = [(int(r["timepoint"]), float(r["fundingRate"])) for r in rows if "timepoint" in r and "fundingRate" in r]
    return sorted(out)


def parse_klines(body: Any) -> list[tuple[int, float, float, float, float]]:
    """(open time ms, open, high, low, close), oldest first."""
    rows = _data(body, "klines") or []
    return sorted((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows)


class KucoinFutures:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, limiter: RateLimiter | None = None):
        self._client = httpx.AsyncClient(timeout=settings.http_timeout_s, transport=transport)
        self._limiter = limiter or RateLimiter(settings.kucoin_public_rps)
        self.base = settings.kucoin_futures_url

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._limiter.acquire()
        return await get_json(self._client, f"{self.base}{path}", params=params)

    async def contracts(self) -> list[Contract]:
        return parse_contracts(await self._get("/api/v1/contracts/active"))

    async def book(self, symbol: str) -> OrderBook:
        return parse_book(await self._get("/api/v1/level2/depth20", {"symbol": symbol}), symbol)

    async def mark_price(self, symbol: str) -> float:
        data = _data(await self._get(f"/api/v1/mark-price/{symbol}/current"), f"mark {symbol}") or {}
        price = _f(data.get("value"))
        if price <= 0:
            raise DataError(f"mark {symbol}: no price in {str(data)[:120]!r}")
        return price

    async def funding_history(self, symbol: str, *, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        body = await self._get("/api/v1/contract/funding-rates", {"symbol": symbol, "from": start_ms, "to": end_ms})
        return parse_funding_history(body)

    async def klines(
        self, symbol: str, *, granularity_min: int, start_ms: int, end_ms: int
    ) -> list[tuple[int, float, float, float, float]]:
        body = await self._get(
            "/api/v1/kline/query", {"symbol": symbol, "granularity": granularity_min, "from": start_ms, "to": end_ms}
        )
        return parse_klines(body)
