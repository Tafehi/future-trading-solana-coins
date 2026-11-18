"""Jupiter Perps trade history from Jupiter's own public API (user, 2026-11-18, D-29).

Replaces the Chainstack chain read (`jobs/jupiter_history.py`, ~340 RPC calls per owner, 20,000 a day) as the first
source of an owner's past: ONE call returns up to 1,000 trades, free, so every recorded owner can be graded within
minutes of first being seen. The chain read stays as the fallback when this API fails.

Checked 2026-11-18:
- Endpoint: GET https://perps-api.jup.ag/v2/trades?walletAddress=<owner>&start=<offset>&end=<offset>. It is the API the
  official Jupiter CLI uses for `jup perps history --address` (github.com/jup-ag/cli, src/clients/PerpsClient.ts and
  docs/perps.md). No key needed (`x-api-key` optional). Answer: {"count": <all trades>, "dataList": [<trade>, ...]},
  newest first. One call returned 1,000 trades; an owner with 1,059 needs a second page (start=1000).
- Depth: years. Owners from blocks 75 and 120 days old had trades back to 341, 665 and 1,095 days.
- Load: 20 calls in a row at 2, 5 and 10 a second all answered 200 (no 429). Paced at JUPITER_API_RPS (2) anyway.
- A trade: action Increase|Decrease, orderType Market|Trigger|Liquidation, positionName "SOL-PERP", side, size (USD of
  this change), price, pnl (Decrease only), fee (= baseFee + borrowFee + liquidationFee + priceImpactFee),
  createdTime (unix s), positionPubkey, owner, txHash.
- Against the chain: all 20 closes of a real owner matched the bot's own close-event decoder (same transactions) to the
  cent in pnl, size and price, for InstantDecrease, the keeper DecreasePosition (a Trigger close) and liquidations.
  Fees matched too, EXCEPT liquidations: the API adds the liquidation fee that the chain decoder left out (DEBUG D-12).

`episodes_from_trades` rebuilds position lifetimes the same way the chain path does (`jupiter_events.episodes`): per
position account, oldest first, an Increase on a flat account opens a lifetime, Decreases shrink it, and the lifetime
closes when the size is back to zero (or on a liquidation). PnL = the closes' pnl minus EVERY fee of the lifetime,
open fees included (the chain path could not see open fees, so it was slightly optimistic).
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

from app.data.http import DataError, RateLimiter, _request
from app.data.jupiter_events import ChainEpisode
from app.utils.logging import get_logger

logger = get_logger(__name__)

URL = "https://perps-api.jup.ag/v2/trades"
PAGE = 1000  # trades per call (one call returned 1,000 on 2026-11-18)
MAX_PAGES = 20  # 20,000 trades: more than any owner grading needs; the rest is noted as cut
COINS = {"SOL", "ETH", "BTC"}  # Jupiter Perps markets (same as data/jupiter_perps.CUSTODY_COIN)
FLAT_USD = 1.0  # a lifetime is closed when what is left is at most this ...
FLAT_SHARE = 0.001  # ... or this share of its biggest size (rounding in the API's USD strings)


@dataclass(frozen=True)
class History:
    trades: list[dict[str, Any]]
    complete: bool  # every trade the API holds was read (False = MAX_PAGES cut it)


class JupiterApi:
    def __init__(self, *, rps: float = 2.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(timeout=30.0, transport=transport,
                                         headers={"x-client-platform": "solana-future-coin"})  # fmt: skip
        self._rate = RateLimiter(rps)
        self.calls = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _page(self, owner: str, start: int) -> dict[str, Any]:
        await self._rate.acquire()
        self.calls += 1
        params = {"walletAddress": owner, "start": start, "end": start + PAGE}
        resp = await _request(self._client, "GET", URL, params=params)
        if resp.status_code != 200:
            raise DataError(f"jupiter trades: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise DataError("jupiter trades: unreadable JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("dataList"), list):
            raise DataError(f"jupiter trades: unexpected answer {str(body)[:120]}")
        return body

    async def history(self, owner: str) -> History:
        """Every trade of `owner` the API holds (paged by offset), up to MAX_PAGES."""
        trades: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        count = 0
        for page in range(MAX_PAGES):
            body = await self._page(owner, page * PAGE)
            rows = [r for r in body["dataList"] if isinstance(r, dict)]
            count = int(body.get("count") or 0)
            for r in rows:
                # Newest first: a trade made between two pages pushes the list down, and the border row comes back.
                key = (r.get("txHash"), r.get("positionPubkey"), r.get("action"), r.get("size"), r.get("createdTime"))
                if key not in seen:
                    seen.add(key)
                    trades.append(r)
            if len(rows) < PAGE or page * PAGE + len(rows) >= count:
                return History(trades, True)
        return History(trades, False)


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def coin_of(trade: dict[str, Any]) -> str:
    return str(trade.get("positionName") or "").split("-")[0].upper()


def episodes_from_trades(owner: str, trades: list[dict[str, Any]], *, since: float = 0.0) -> list[ChainEpisode]:
    """API trades -> complete position lifetimes closed at or after `since`. Pure."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if t.get("owner") == owner and coin_of(t) in COINS and t.get("positionPubkey"):
            groups[str(t["positionPubkey"])].append(t)
    out: list[ChainEpisode] = []
    for key, rows in groups.items():
        # Oldest first; in the same second an Increase comes before a Decrease (open, then close).
        rows.sort(key=lambda t: (int(t.get("createdTime") or 0), 0 if t.get("action") == "Increase" else 1))
        size = peak = pnl = fees = entry_value = added = 0.0
        opened = 0.0
        is_open = False
        for t in rows:
            sz, px, fee = _f(t.get("size")), _f(t.get("price")), _f(t.get("fee"))
            when = float(t.get("createdTime") or 0)
            if t.get("action") == "Increase":
                if not is_open:
                    size = peak = pnl = fees = entry_value = added = 0.0
                    opened, is_open = when, True
                size += sz
                peak = max(peak, size)
                fees += fee
                entry_value += sz * px
                added += sz
                continue
            if not is_open:
                continue  # a close whose open is older than what the API returned: no full lifetime
            size -= sz
            pnl += _f(t.get("pnl"))
            fees += fee
            if size <= max(FLAT_USD, FLAT_SHARE * peak) or t.get("orderType") == "Liquidation":
                is_open = False
                if when >= since:
                    entry = entry_value / added if added else px  # size-weighted average of the opens
                    out.append(ChainEpisode(owner=owner, position_key=key, coin=coin_of(t), side=str(t.get("side")),
                                            size_usd=peak, entry_px=entry, exit_px=px, opened_at=opened,
                                            closed_at=when, pnl=pnl - fees))  # fmt: skip
    return sorted(out, key=lambda e: e.closed_at)
