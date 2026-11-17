"""Minimal Solana JSON-RPC client (used by the wallet tracer, `tools/wallet_trace/sol_source.py`).

The URL is passed in by the caller and is never logged or put in an error message: it can carry a key (Chainstack puts
it in the path). Errors keep only "HTTP <status>" or the exception type (DEBUG D-10).
"""

from typing import Any

import httpx

from app.data.http import DataError, _request, _Retryable

TIMEOUT_S = 60.0


class SolanaRpc:
    def __init__(self, *, url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url
        self._client = httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, body: dict[str, object], what: str) -> object:
        """POST with the shared retry (429 / 5xx / transport errors, exponential backoff, `data/http._request`)."""
        if not self.url:
            raise DataError("no Solana RPC URL")
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

    async def call(self, method: str, params: list[object]) -> Any:
        """One JSON-RPC call -> its `result`. Raises DataError on an RPC error body."""
        body = await self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, method)
        if not isinstance(body, dict) or "result" not in body:
            raise DataError(f"{method}: {str(body)[:200]}")
        return body["result"]
