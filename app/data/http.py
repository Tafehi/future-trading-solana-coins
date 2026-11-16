"""Shared HTTP plumbing for the public data clients.

- `RateLimiter` paces plain requests per second (KuCoin's public pool).
- `WeightBudget` paces Hyperliquid's weighted limit: 1200 weight a minute per IP, where a position poll
  costs 2 and a fill history costs 20 plus 1 per 20 items. Waiting is better than a 429 that blocks the
  IP, because the signal loop shares the same budget.
- `get_json` / `post_json` retry transient failures (timeouts, 429, 5xx) with backoff and raise
  `DataError` when they give up, so callers handle one exception type.
"""

import asyncio
import time
from collections import deque
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.utils.logging import get_logger

logger = get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class DataError(RuntimeError):
    """A data source failed after retries, or answered with something we cannot read."""


class _Retryable(Exception):
    pass


class RateLimiter:
    """At most `rps` requests a second, spaced evenly."""

    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / rps
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval


class WeightBudget:
    """A sliding 60-second window of spent weight. `spend` waits until `weight` fits.

    `reserve_for_hot` keeps part of the budget for the signal poller: a cold-path caller (grading) may
    only fill the window up to `limit - reserve_for_hot`.
    """

    def __init__(self, per_minute: int, *, reserve_for_hot: int = 0) -> None:
        self.limit = per_minute
        self.reserve = min(reserve_for_hot, per_minute - 1)
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def used(self, now: float | None = None) -> int:
        at = time.monotonic() if now is None else now
        while self._spent and at - self._spent[0][0] >= 60.0:
            self._spent.popleft()
        return sum(w for _, w in self._spent)

    async def spend(self, weight: int, *, hot: bool = False) -> None:
        cap = self.limit if hot else self.limit - self.reserve
        weight = min(weight, cap)
        async with self._lock:
            while True:
                now = time.monotonic()
                if self.used(now) + weight <= cap:
                    self._spent.append((now, weight))
                    return
                oldest = self._spent[0][0] if self._spent else now
                await asyncio.sleep(max(0.05, 60.0 - (now - oldest)))

    def add(self, weight: int) -> None:
        """Record extra weight learned after the call (per-item charges). Never waits."""
        if weight > 0:
            self._spent.append((time.monotonic(), weight))


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (_Retryable, httpx.TransportError))


async def _request(
    client: httpx.AsyncClient, method: str, url: str, *, attempts: int = 4, **kwargs: Any
) -> httpx.Response:
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=0.5, max=8.0),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code in RETRY_STATUSES:
                    raise _Retryable(f"HTTP {resp.status_code}")
                return resp
    except (_Retryable, httpx.HTTPError) as exc:
        raise DataError(f"{method} {url}: {type(exc).__name__}: {exc}") from exc
    raise DataError(f"{method} {url}: no attempt made")  # pragma: no cover — tenacity always attempts


async def get_json(client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None) -> Any:
    resp = await _request(client, "GET", url, params=params)
    if resp.status_code != 200:
        raise DataError(f"GET {url}: HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise DataError(f"GET {url}: unreadable JSON: {exc}") from exc


async def post_json(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> Any:
    resp = await _request(client, "POST", url, json=body)
    if resp.status_code != 200:
        raise DataError(f"POST {url} {body.get('type', '')}: HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise DataError(f"POST {url}: unreadable JSON: {exc}") from exc
