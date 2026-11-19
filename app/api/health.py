"""Loopback `/health` for the Docker healthcheck and statusCheck.sh, one per program (D-30): the scout on
SCOUT_HEALTH_PORT (8080), the trader on TRADER_HEALTH_PORT (8081).

200 when every loop has beaten recently AND the program's own checks pass; 503 otherwise, naming what failed:
- scout: its loops; the last SUCCESSFUL Jupiter feed poll (`jup:last_poll`: a loop beats even when its pass fails);
  the last pick (`jup:picked_at`: the trader refuses new opens once it is older than COPYLIST_MAX_AGE_S).
- trader: its loops; the copy list's age; the websocket (down longer than FEED_DOWN_LIMIT_S); the feed worker.
A halted or paused account is shown in the body but does not fail health (the process is fine; statusCheck.sh
reports the halt as a FAIL).

The loop names MUST be exactly the loops each program runs (a test reads scout.py and trader.py). Until 2026-11-18
this still listed `poll` and `discovery`, two loops that had left: they never beat, so /health was always 503 and
Docker showed the container "unhealthy" (DEBUG D-11).

Standard library only (asyncio streams): one GET route does not justify a web framework in the image. No import
from `app.trading`: the scout serves this too and never loads trading code.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings
from app.data.jupiter_perps import SOURCE
from app.db import crossread, meta, store
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Loop name -> seconds without a beat before /health fails.
SCOUT_LIMITS_S = {"jupiter": 300, "jhistory": 600, "grade": 600, "cold": 600}
TRADER_LIMITS_S = {"events": 120, "risk": 120, "cold": 600, "copylist": 300, "feedpoll": 300}
RECONCILE_LIMIT_S = 600  # the trader's reconcile loop runs in live mode only
FEED_POLL_LIMIT_S = 300  # the scout's last successful Jupiter feed poll (5 polls missed)
FEED_DOWN_LIMIT_S = 300  # the trader's websocket may be down this long (a reconnect backs off up to 60 s)

Snapshot = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


def trader_limits() -> dict[str, int]:
    limits = dict(TRADER_LIMITS_S)
    if settings.is_live:
        limits["reconcile"] = RECONCILE_LIMIT_S
    return limits


def _age(at: float, ts: float | None) -> float | None:
    """Seconds since `ts`, exact: limits compare this, the body shows it rounded."""
    return None if ts is None else at - ts


def _shown(age: float | None) -> float | None:
    return None if age is None else round(age, 1)


async def _beats(limits: dict[str, int], at: float) -> tuple[dict[str, float | None], list[str], dict[str, Any]]:
    """(loop ages, stale loop names, account state) from this program's own file."""
    ages: dict[str, float | None] = {}
    stale: list[str] = []
    async with store.session() as conn:
        for name, limit in limits.items():
            age = _age(at, await meta.last_beat(conn, name))
            ages[name] = _shown(age)
            if age is None or age > limit:
                stale.append(name)
        async with conn.execute(
            "SELECT halted, halt_reason, paused, pause_reason FROM account_state WHERE account = ?", (settings.account,)
        ) as cur:
            row = await cur.fetchone()
    return ages, stale, dict(row) if row else {}


def _finish(body: dict[str, Any], stale: list[str]) -> tuple[int, dict[str, Any]]:
    body["status"] = "ok" if not stale else f"stale: {', '.join(stale)}"
    return (200 if not stale else 503), body


async def scout_snapshot(now: float | None = None) -> tuple[int, dict[str, Any]]:
    at = time.time() if now is None else now
    ages, stale, _ = await _beats(SCOUT_LIMITS_S, at)
    async with store.session() as conn:
        poll = await meta.get(conn, crossread.LAST_POLL_KEY)
        pick = await meta.get(conn, crossread.PICKED_AT_KEY)
        async with conn.execute(
            "SELECT COALESCE(SUM(tracked), 0) AS t, COALESCE(SUM(watched), 0) AS w FROM wallets WHERE source = ?",
            (SOURCE,),
        ) as cur:
            row = await cur.fetchone()
    poll_age = _age(at, float(poll[0]) if poll else None)
    pick_age = _age(at, float(pick[0]) if pick else None)
    body: dict[str, Any] = {
        "program": "scout",
        "loops": ages,
        "feed_poll_age_s": _shown(poll_age),
        "pick_age_s": _shown(pick_age),
        "tracked": int(row["t"]) if row else 0,
        "watched": int(row["w"]) if row else 0,
    }
    if settings.jupiter_enabled and settings.solana_rpc_url and (poll_age is None or poll_age > FEED_POLL_LIMIT_S):
        stale.append("feed poll")
    if settings.jupiter_enabled and (pick_age is None or pick_age > settings.copylist_max_age_s):
        stale.append("pick")
    return _finish(body, stale)


async def trader_snapshot(
    *, list_age: float | None, listed: dict[str, int], feed: dict[str, Any] | None, worker_alive: bool,
    now: float | None = None,
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    """`list_age`: seconds since the scout's pick (None = never); `feed`: the websocket's stats (None = off)."""
    at = time.time() if now is None else now
    ages, stale, account_state = await _beats(trader_limits(), at)
    body: dict[str, Any] = {
        "program": "trader",
        "mode": settings.mode,
        "account": settings.account,
        "loops": ages,
        "copy_list": {"age_s": _shown(list_age), **listed},
        "feed": feed,
        "account_state": account_state,
    }
    if list_age is None or list_age > settings.copylist_max_age_s:
        stale.append("copy list")
    down = (feed or {}).get("down_for_s")
    if feed is not None and feed.get("enabled") and down is not None and down > FEED_DOWN_LIMIT_S:
        stale.append("websocket")
    if not worker_alive:
        stale.append("feed worker")
    return _finish(body, stale)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, snapshot: Snapshot) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        path = line.decode("latin-1").split(" ")[1] if line.count(b" ") >= 2 else ""
        if path.split("?")[0] == "/health":
            code, body = await snapshot()
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


async def serve(port: int, snapshot: Snapshot) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, snapshot)

    # 0.0.0.0 inside the container; docker-compose publishes it on 127.0.0.1 of the host only.
    server = await asyncio.start_server(handle, host="0.0.0.0", port=port)  # noqa: S104
    async with server:
        await server.serve_forever()
