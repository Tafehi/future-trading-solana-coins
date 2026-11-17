"""Solana source for the wallet tracer (PLAN §4.2): signatures + transactions over JSON-RPC, decoded into transfers,
swaps and Jupiter Perps close events. Free public RPC by default (`settings.SOL_DEFAULT_RPC`); the bot's
`SOLANA_RPC_URL` is only used when the user passes it with `--rpc-url` on purpose. No URL is ever logged or put in an
error message: it can carry a key (the transport goes through `SolanaRpc`, which strips it, `data/solana_rpc.py`).

Checked 2026-11-17 against one real answer each:
- `getTransaction` with `encoding: jsonParsed` gives `accountKeys` as objects (`pubkey`, `signer`, `source`), parsed
  `system` / `spl-token` instructions (`parsed.type`, `parsed.info`), and `preTokenBalances` / `postTokenBalances` with
  `owner`, `mint` and `uiTokenAmount.uiAmountString`. Unknown programs stay raw (`programId`, `data`).
- **Transaction version 1 is live on mainnet** (SIMD-0385, since epoch 1035, per Jupiter's docs): asking with
  `maxSupportedTransactionVersion: 0` returns error -32015 for such a transaction. The tracer asks for 1.
- The public RPC answered `getProgramAccounts` with a memcmp filter, and started returning 429 ("Too many requests for
  a specific RPC call") after about 10 `getTransaction` calls at ~2 a second; at 1 a second, 20 in a row passed. So
  Solana is paced at `SOL_CALLS_PER_SECOND`, and 429 is retried with backoff.
- SOL price: Hyperliquid's public `allMids` (perp mid of `SOL`, no key; 73.47 when Jupiter's price API said 73.42
  on 2026-11-17). Used only to put a USD value on SOL moves; `--sol-usd` overrides it.
- Program ids, each confirmed `executable` on mainnet with `getAccountInfo`: pump.fun bonding curve `6EF8…F6P` and
  PumpSwap `pAMM…XEA` (pump-fun/pump-public-docs), Raydium AMM v4 / CPMM / CLMM / LaunchLab (docs.raydium.io,
  "Program addresses"), Jupiter aggregator v6 `JUP6…aV4`, Jupiter Perps (already used by the bot, DEBUG D-2).
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.data import jupiter_events as je
from app.data import jupiter_perps as jp
from app.data.http import DataError, RateLimiter, _request
from app.data.solana_rpc import SolanaRpc
from app.tools.wallet_trace import settings as S
from app.tools.wallet_trace.cache import Cache
from app.tools.wallet_trace.hl_source import CallBudget

CHAIN = "sol"
HL_INFO = "https://api.hyperliquid.xyz/info"
JUPITER_PERPS = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"
VENUES = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
}
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL: "SOL", USDC: "USDC", USDT: "USDT"}  # mints confirmed with getAccountInfo 2026-11-17
SYSTEM_ADDRESSES = set(VENUES) | {JUPITER_PERPS, "11111111111111111111111111111111", WSOL}


def is_system(addr: str) -> bool:
    return addr in SYSTEM_ADDRESSES


@dataclass
class SolRaw:
    address: str
    signatures: list[dict[str, Any]] = field(default_factory=list)  # newest first
    history_end: bool = False  # True = reached the wallet's first transaction
    txs: list[dict[str, Any]] = field(default_factory=list)  # jsonParsed getTransaction results
    balance_lamports: int | None = None
    perp_events: list[je.CloseEvent] = field(default_factory=list)
    perp_positions: int = 0
    sol_usd: float = 0.0


class SolSource:
    def __init__(
        self,
        cache: Cache,
        budget: CallBudget,
        *,
        url: str = S.SOL_DEFAULT_RPC,
        transport: httpx.AsyncBaseTransport | None = None,
        rps: float = S.SOL_CALLS_PER_SECOND,
        max_tx: int = S.SOL_MAX_TX_PER_WALLET,
    ) -> None:
        self.cache = cache
        self.budget = budget
        self.rpc = SolanaRpc(url=url, transport=transport)  # url is never None here: never the bot's secret by default
        self._rate = RateLimiter(rps)
        self.max_tx = max_tx
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)

    async def close(self) -> None:
        await self.rpc.close()
        await self._http.aclose()

    async def _call(self, address: str, query: str, method: str, params: list[object]) -> Any:
        hit = self.cache.get(CHAIN, address, query)
        if hit is not None:
            return hit
        self.budget.spend()
        await self._rate.acquire()
        answer = await self.rpc.call(method, params)
        self.cache.put(CHAIN, address, query, answer)
        return answer

    async def sol_usd(self) -> float:
        """SOL in USD from Hyperliquid's public mids (cached per run). Raises DataError when it cannot."""
        hit = self.cache.get(CHAIN, "_global", "sol_usd")
        if hit is not None:
            return float(hit)
        self.budget.spend()
        resp = await _request(self._http, "POST", HL_INFO, json={"type": "allMids"})
        try:
            price = float(resp.json()["SOL"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DataError(f"SOL price: unreadable answer (HTTP {resp.status_code})") from exc
        self.cache.put(CHAIN, "_global", "sol_usd", price)
        return price

    async def signatures(self, address: str) -> tuple[list[dict[str, Any]], bool]:
        """Newest-first signatures, up to `SOL_MAX_SIG_PAGES` pages. Returns (signatures, reached the first one)."""
        out: list[dict[str, Any]] = []
        before: str | None = None
        for page in range(S.SOL_MAX_SIG_PAGES):
            opts: dict[str, object] = {"limit": S.SOL_SIG_PAGE, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            chunk = await self._call(address, f"signatures_{page}", "getSignaturesForAddress", [address, opts])
            rows = [r for r in chunk or [] if isinstance(r, dict)]
            out += rows
            if len(rows) < S.SOL_SIG_PAGE:
                return out, True
            before = str(rows[-1]["signature"])
        return out, False

    async def transaction(self, signature: str, *, encoding: str = "jsonParsed") -> Any:
        opts = {"encoding": encoding, "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}
        return await self._call("_tx", f"{signature}_{encoding}", "getTransaction", [signature, opts])

    async def jupiter(self, owner: str) -> tuple[int, list[je.CloseEvent]]:
        """Jupiter Perps: every Position account of the owner -> its transactions -> close events (as the bot's history
        job, `jobs/jupiter_history.backfill_owner`, but cached and on the tracer's own budget)."""
        keys = await self._call(owner, "jup_positions", "getProgramAccounts", [
            JUPITER_PERPS,
            {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0},
             "filters": [{"dataSize": jp.ACCOUNT_SIZE}, {"memcmp": {"offset": jp.SLICE_OFFSET, "bytes": owner}}]},
        ])  # fmt: skip
        events: list[je.CloseEvent] = []
        pubkeys = [str(r["pubkey"]) for r in keys or [] if isinstance(r, dict)]
        for key in pubkeys:
            sigs = await self._call(key, "signatures_0", "getSignaturesForAddress",
                                    [key, {"limit": S.SOL_JUP_TX_PER_POSITION, "commitment": "confirmed"}])  # fmt: skip
            for s in sigs or []:
                if not isinstance(s, dict) or s.get("err") is not None:
                    continue
                tx = await self.transaction(str(s["signature"]), encoding="json")
                events += [e for e in je.events_from_transaction(tx, JUPITER_PERPS) if e.owner == owner]
        return len(pubkeys), events

    async def fetch(self, address: str, *, sol_usd: float) -> SolRaw:
        raw = SolRaw(address=address, sol_usd=sol_usd)
        raw.signatures, raw.history_end = await self.signatures(address)
        ok = [s for s in raw.signatures if s.get("err") is None]
        chosen = ok[: self.max_tx]
        if raw.history_end:
            chosen += [s for s in ok[-S.SOL_OLDEST_TX :] if s not in chosen]  # the funder is in the oldest ones
        for s in chosen:
            tx = await self.transaction(str(s["signature"]))
            if isinstance(tx, dict):
                raw.txs.append(tx)
        bal = await self._call(address, "balance", "getBalance", [address, {"commitment": "confirmed"}])
        raw.balance_lamports = int(bal["value"]) if isinstance(bal, dict) and "value" in bal else None
        try:
            raw.perp_positions, raw.perp_events = await self.jupiter(address)
        except DataError as exc:
            raise DataError(f"jupiter positions: {exc}") from None
        return raw
