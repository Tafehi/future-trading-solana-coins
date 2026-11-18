"""Minimal Solana JSON-RPC client for the Jupiter Perps change feed (`data/jupiter_perps.py`) and the wallet tracer.

The URL (Chainstack, with its key inside) comes from the `SOLANA_RPC_URL` secret and is never logged: errors carry
the exception type only.
"""

from typing import Any

import httpx

from app.config import settings
from app.data import jupiter_perps as jp
from app.data.http import DataError, _request, _Retryable
from app.utils.logging import get_logger

logger = get_logger(__name__)

SCAN_TIMEOUT_S = 60.0


class SolanaRpc:
    def __init__(self, *, url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url if url is not None else settings.solana_rpc_url
        self._client = httpx.AsyncClient(timeout=SCAN_TIMEOUT_S, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, body: dict[str, object], what: str) -> object:
        """POST with the shared retry (429 / 5xx / transport errors, exponential backoff, `data/http._request`)."""
        if not self.url:
            raise DataError("SOLANA_RPC_URL is not set")
        try:
            resp = await _request(self._client, "POST", self.url, json=body)
        except DataError as exc:
            # The message can carry the URL (with its key inside): keep only the kind of failure (DEBUG D-10). Our own
            # retry marker holds just "HTTP <status>"; any other cause is reduced to its exception type, because its
            # text may repeat the URL (the old `split(':')[-1]` kept "//host/<key>" in exactly that case).
            cause = exc.__cause__
            kind = str(cause) if isinstance(cause, _Retryable) else type(cause).__name__ if cause else "request failed"
            raise DataError(f"{what}: {kind[:80]}") from None
        if resp.status_code != 200:
            raise DataError(f"{what}: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise DataError(f"{what}: unreadable JSON") from exc

    async def updated_positions(self, block: int) -> list[jp.JupPosition]:
        body = await self._post(jp.updated_request(settings.jupiter_program_id, block), "getProgramAccounts")
        try:
            return jp.parse_program_accounts(body)
        except ValueError as exc:
            raise DataError(str(exc)) from exc

    async def call(self, method: str, params: list[object]) -> Any:
        """One JSON-RPC call -> its `result`. Raises DataError on an RPC error body."""
        body = await self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, method)
        if not isinstance(body, dict) or "result" not in body:
            raise DataError(f"{method}: {str(body)[:200]}")
        return body["result"]

    async def owner_position_keys(self, owner: str) -> list[str]:
        """Every Position account (open or closed) of one owner: the owner key sits at byte 8 (memcmp, base58)."""
        result = await self.call(
            "getProgramAccounts",
            [
                settings.jupiter_program_id,
                {
                    "encoding": "base64",
                    "dataSlice": {"offset": 0, "length": 0},
                    "filters": [
                        {"dataSize": jp.ACCOUNT_SIZE},
                        {"memcmp": {"offset": jp.SLICE_OFFSET, "bytes": owner}},
                    ],
                },
            ],
        )
        return [str(r["pubkey"]) for r in result or [] if isinstance(r, dict)]

    async def signatures(
        self, address: str, *, before: str | None = None, limit: int = 1000
    ) -> list[dict[str, object]]:
        opts: dict[str, object] = {"limit": limit, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        result = await self.call("getSignaturesForAddress", [address, opts])
        return [r for r in result or [] if isinstance(r, dict)]

    async def transaction(self, signature: str) -> Any:
        # Version 1 transactions are live on mainnet (SIMD-0385); asking with 0 gets error -32015 for them (2026-11-17).
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}],
        )

    async def positions(self, pubkeys: list[str]) -> dict[str, jp.JupPosition | None]:
        out: dict[str, jp.JupPosition | None] = {}
        for i in range(0, len(pubkeys), jp.MAX_MULTIPLE):
            chunk = pubkeys[i : i + jp.MAX_MULTIPLE]
            body = await self._post(jp.multiple_request(chunk), "getMultipleAccounts")
            try:
                out.update(jp.parse_multiple(body, chunk))
            except ValueError as exc:
                raise DataError(str(exc)) from exc
        return out
