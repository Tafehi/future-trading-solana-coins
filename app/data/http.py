"""Shared HTTP plumbing for the wallet tracer's public data clients.

- `RateLimiter` paces plain requests per second (the free Solana RPC, the Hyperliquid cap).
- `WeightBudget` paces Hyperliquid's weighted limit (1200 weight a minute per IP; most info calls weigh 20, the
  list calls 1 more per 20 items). Waiting is better than a 429 that blocks the IP.
- `_request` retries transient failures (timeouts, 429, 5xx) with backoff and raises `DataError` when it gives up,
  so callers handle one exception type.
"""

import asyncio
import time
from collections import deque
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

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
    """A sliding 60-second window of spent weight. `spend` waits until `weight` fits; `add` records weight learned
    after the answer (per-item charges)."""

    def __init__(self, per_minute: int) -> None:
        self.limit = per_minute
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def used(self, now: float | None = None) -> int:
        at = time.monotonic() if now is None else now
        while self._spent and at - self._spent[0][0] >= 60.0:
            self._spent.popleft()
        return sum(w for _, w in self._spent)

    async def spend(self, weight: int) -> None:
        weight = min(weight, self.limit)
        async with self._lock:
            while True:
                now = time.monotonic()
                if self.used(now) + weight <= self.limit:
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
