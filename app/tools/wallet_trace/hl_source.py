"""Read-only Hyperliquid `info` client for the wallet tracer (PLAN §4.1). Public API, no key. Not the bot's old client
(that left with the split, D-19 / B-17); this one only reads, and only for the tracer.

Checked 2026-11-17 against the docs (hyperliquid.gitbook.io: "Info endpoint", "Perpetuals", "Spot", "Rate limits and
user limits", "HyperCore <> HyperEVM transfers") and one real answer per query:

- Time-range answers return at most 500 elements; page with the last returned time as the next `startTime`
  (`userNonFundingLedgerUpdates`, `userFunding`). `userFillsByTime` returns at most 2000 per answer, oldest first, and
  only the 10,000 most recent fills exist at all: more than that and the history is cut (`fills_capped`).
- TWAP orders fill in slices that are NOT in `userFillsByTime`: a real wallet had 516 normal fills and 1,347 TWAP
  slices, and its balance only closed with both. `userTwapSliceFillsByTime` (items `{"fill": {...}, "twapId"}`, takes
  `startTime`; not on the info page, but listed on the rate-limit page and answering) is paged like the fills.
- `delegatorHistory` (no paging documented) holds every staking deposit/withdrawal. The non-funding ledger MISSED three
  `cStakingTransfer`s of a real wallet (10,060 HYPE among them) that `delegatorHistory` has; with it the balance sum is
  exact (DEBUG D-8). So staking comes from `delegatorHistory`, not the ledger.
- Weight per IP: 1200 a minute. `clearinghouseState` / `spotClearinghouseState` weigh 2, `userRole` 60, every other
  info call 20, and the list calls (`userFillsByTime`, `userFunding`, TWAP slices, `delegatorHistory`) add 1 per 20
  items. Paced by that
  weight (about one call a second) and never faster than `MAX_CALLS_PER_SECOND`; 429 / 5xx retry with backoff.
  A live depth-2 run (190 calls) still drew one 429: a list's item weight is only known after the answer. So the budget
  keeps headroom (`HL_WEIGHT_PER_MINUTE` below 1200) and a 429 that outlasts the backoff is waited out
  (`HL_429_WAIT_S`).
- System addresses: `0x2222…2222` = HYPE <-> HyperEVM; `0x20` followed by zeros and the token index (big-endian) in the
  last bytes = that token <-> HyperEVM. Money sent there left HyperCore: the trail stops.
- `vaultDetails` (`vaultAddress`) gives `name` and `leader`; HLP answers "Hyperliquidity Provider (HLP)". Only the name
  and leader are kept (the full answer is ~60 KB of follower lists).
- `userRole`: "missing" | "user" | "agent" (data.user = its master) | "vault" | "subAccount" (data.master).
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.data.http import DataError, RateLimiter, WeightBudget, _request
from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.cache import Cache

URL = "https://api.hyperliquid.xyz/info"
CHAIN = "hl"
HYPE_SYSTEM = "0x" + "22" * 20
HLP_NAME = "Hyperliquidity Provider"  # `vaultDetails` name of HLP, the protocol's own vault (read 2026-11-17)


def is_system(addr: str) -> bool:
    """A HyperCore <-> HyperEVM system address (docs, 2026-11-17)."""
    a = addr.lower()
    if a == HYPE_SYSTEM:
        return True
    return len(a) == 42 and a.startswith("0x20") and set(a[4:26]) <= {"0"}


class OutOfCalls(Exception):
    """The run's `--max-calls` is used up."""


@dataclass
class CallBudget:
    max_calls: int
    used: int = 0

    def spend(self) -> None:
        if self.used >= self.max_calls:
            raise OutOfCalls(f"call budget of {self.max_calls} used up")
        self.used += 1


@dataclass
class HlRaw:
    """Every raw answer for one wallet, as the API gave it (paged lists joined)."""

    address: str
    ledger: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    twap_fills: list[dict[str, Any]] = field(default_factory=list)  # userTwapSliceFillsByTime items (fill + twapId)
    funding: list[dict[str, Any]] = field(default_factory=list)
    staking: list[dict[str, Any]] = field(default_factory=list)  # delegatorHistory
    perp: dict[str, Any] = field(default_factory=dict)  # clearinghouseState
    spot: dict[str, Any] = field(default_factory=dict)  # spotClearinghouseState
    deleg: dict[str, Any] = field(default_factory=dict)  # delegatorSummary
    vaults: list[dict[str, Any]] = field(default_factory=list)  # userVaultEquities
    subs: list[dict[str, Any]] | None = None
    role: dict[str, Any] = field(default_factory=dict)
    portfolio: list[Any] = field(default_factory=list)
    vault_details: dict[str, dict[str, Any]] = field(default_factory=dict)  # vault -> {"name", "leader"} (vaultDetails)


# Query name -> HlRaw field. The order is the fetch order.
SINGLE = {
    "clearinghouseState": "perp",
    "spotClearinghouseState": "spot",
    "delegatorSummary": "deleg",
    "delegatorHistory": "staking",
    "userVaultEquities": "vaults",
    "subAccounts": "subs",
    "userRole": "role",
    "portfolio": "portfolio",
}
PAGED = {"userNonFundingLedgerUpdates": ("ledger", S.HL_PAGE_TIME_RANGE),
         "userFunding": ("funding", S.HL_PAGE_TIME_RANGE),
         "userFillsByTime": ("fills", S.HL_PAGE_FILLS),
         "userTwapSliceFillsByTime": ("twap_fills", S.HL_PAGE_FILLS)}  # fmt: skip


def _weight(query: str, items: int) -> int:
    w = S.HL_WEIGHT.get(query, S.HL_WEIGHT_DEFAULT)
    if query in S.HL_ITEM_WEIGHTED:
        w += items // S.HL_WEIGHT_PER_ITEMS
    return w


class HlSource:
    def __init__(
        self,
        cache: Cache,
        budget: CallBudget,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rps: float = S.MAX_CALLS_PER_SECOND,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cache = cache
        self.budget = budget
        self._client = httpx.AsyncClient(timeout=30.0, transport=transport)
        self._rate = RateLimiter(rps)
        self._weight = WeightBudget(S.HL_WEIGHT_PER_MINUTE)
        self._sleep = sleep

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, body: dict[str, Any]) -> Any:
        query = str(body["type"])
        self.budget.spend()
        await self._weight.spend(_weight(query, 0))
        for attempt in range(S.HL_429_RETRIES + 1):
            await self._rate.acquire()
            try:
                resp = await _request(self._client, "POST", URL, json=body)
                break
            except DataError as exc:
                if "429" not in str(exc) or attempt == S.HL_429_RETRIES:
                    raise
                await self._sleep(S.HL_429_WAIT_S)  # the IP is blocked for a while: wait it out, then try again
        if resp.status_code != 200:
            raise DataError(f"hyperliquid {query}: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            answer = resp.json()
        except ValueError as exc:
            raise DataError(f"hyperliquid {query}: unreadable JSON") from exc
        if isinstance(answer, list):
            self._weight.add(_weight(query, len(answer)) - _weight(query, 0))
        return answer

    async def query(self, query: str, address: str, **extra: Any) -> Any:
        """One cached query (whole answer). `address` "_global" for queries without a user."""
        hit = self.cache.get(CHAIN, address, query)
        if hit is not None:
            return hit
        body: dict[str, Any] = {"type": query, **extra}
        if address != "_global":
            body["user"] = address
        answer = await self._post(body)
        self.cache.put(CHAIN, address, query, answer)
        return answer

    async def paged(self, query: str, address: str, page: int) -> list[dict[str, Any]]:
        """Every element of a time-range query: page by the last time until a short page. Cached as one answer."""
        hit = self.cache.get(CHAIN, address, query)
        if hit is not None:
            return list(hit)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        start = 0
        while True:
            chunk = await self._post({"type": query, "user": address, "startTime": start})
            if not isinstance(chunk, list):
                raise DataError(f"hyperliquid {query}: expected a list, got {str(chunk)[:100]}")
            for item in chunk:
                key = repr(sorted(item.items())) if isinstance(item, dict) else repr(item)
                if key not in seen:  # the page border repeats elements of the last millisecond
                    seen.add(key)
                    out.append(item)
            if len(chunk) < page:
                break
            tail = chunk[-1]
            last = int(tail["time"] if "time" in tail else tail["fill"]["time"])
            start = last if last > start else start + 1  # a whole page in one millisecond must still move on
        self.cache.put(CHAIN, address, query, out)
        return out

    async def spot_meta(self) -> dict[str, Any]:
        return dict(await self.query("spotMeta", "_global"))

    async def mids(self) -> dict[str, str]:
        """Current mid price per coin (spot pairs as `@N` / `PURR/USDC`); values the gaps of the balance check."""
        return dict(await self.query("allMids", "_global"))

    async def fetch(self, address: str) -> HlRaw:
        a = address.lower()
        raw = HlRaw(address=a)
        for query, (name, page) in PAGED.items():
            setattr(raw, name, await self.paged(query, a, page))
        for query, name in SINGLE.items():
            answer = await self.query(query, a)
            setattr(raw, name, answer if answer is not None or name == "subs" else getattr(raw, name))
        for vault in vaults_of(raw):
            raw.vault_details[vault] = await self.vault(vault)
        return raw

    async def vault(self, vault: str) -> dict[str, Any]:
        """Name and leader of a vault (cached under the vault's own address)."""
        hit = self.cache.get(CHAIN, vault, "vaultDetails")
        if hit is not None:
            return dict(hit)
        answer = await self._post({"type": "vaultDetails", "vaultAddress": vault}) or {}
        small = {"name": str(answer.get("name") or ""), "leader": str(answer.get("leader") or "").lower()}
        self.cache.put(CHAIN, vault, "vaultDetails", small)
        return small


def vaults_of(raw: HlRaw) -> list[str]:
    """Every vault the wallet touched: from the ledger and from `userVaultEquities`."""
    out = [str((e.get("delta") or {}).get("vault") or "").lower() for e in raw.ledger]
    out += [str(v.get("vaultAddress") or "").lower() for v in raw.vaults if isinstance(v, dict)]
    return sorted({v for v in out if v})


def raw_from_answers(address: str, answers: dict[str, Any]) -> HlRaw:
    """Build an HlRaw from a {query: answer} dict (fixtures and the cache use the API's own query names)."""
    raw = HlRaw(address=address.lower())
    for query, (name, _) in PAGED.items():
        setattr(raw, name, list(answers.get(query) or []))
    for query, name in SINGLE.items():
        if query in answers and (answers[query] is not None or name == "subs"):
            setattr(raw, name, answers[query])
    raw.vault_details = dict(answers.get("vaultDetails") or {})
    return raw
