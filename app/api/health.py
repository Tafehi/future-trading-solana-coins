"""Loopback `/health` for the Docker healthcheck and statusCheck.sh (ported from ai-kucoin-future).

200 when every loop has beaten recently; 503 otherwise, naming the stale loop. A halted or paused account is shown in
the body but does not fail health (the process is fine; statusCheck.sh reports the halt as a FAIL).

Standard library only (asyncio streams): one GET route does not justify a web framework in the image.
"""

import asyncio
import json
import time
from typing import Any

from app.config import settings
from app.db import meta, store
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Loop name -> seconds without a beat before /health fails.
LIMITS_S = {"poll": 300, "events": 120, "risk": 120, "cold": 600, "discovery": 1800}


async def snapshot(now: float | None = None, limits: dict[str, int] | None = None) -> tuple[int, dict[str, Any]]:
    at = time.time() if now is None else now
    body: dict[str, Any] = {"loops": {}, "mode": settings.mode, "account": settings.account}
    stale: list[str] = []
    async with store.session() as conn:
        for name, limit in (limits or LIMITS_S).items():
            ts = await meta.last_beat(conn, name)
            age = None if ts is None else round(at - ts, 1)
            body["loops"][name] = age
            if age is None or age > limit:
                stale.append(name)
        async with conn.execute(
            "SELECT halted, halt_reason, paused, pause_reason FROM account_state WHERE account = ?", (settings.account,)
        ) as cur:
            row = await cur.fetchone()
        body["account_state"] = dict(row) if row else {}
    body["status"] = "ok" if not stale else f"stale: {', '.join(stale)}"
    return (200 if not stale else 503), body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, limits: dict[str, int]) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        path = line.decode("latin-1").split(" ")[1] if line.count(b" ") >= 2 else ""
        if path.split("?")[0] == "/health":
            code, body = await snapshot(limits=limits)
        else:
            code, body = 404, {"error": "only /health"}
        payload = json.dumps(body).encode()
        reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[code]
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
    except Exception as exc:  # noqa: BLE001 — a bad request must not kill the server task
        logger.warning("health: request failed: %s", type(exc).__name__)
    finally:
        writer.close()


async def serve(limits: dict[str, int] | None = None) -> None:
    chosen = limits or LIMITS_S

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, chosen)

    # 0.0.0.0 inside the container; docker-compose publishes it on 127.0.0.1 of the host only.
    server = await asyncio.start_server(handle, host="0.0.0.0", port=settings.health_port)  # noqa: S104
    async with server:
        await server.serve_forever()
