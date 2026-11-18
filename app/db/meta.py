"""Small `meta` table helpers: heartbeats and counters the status report reads."""

import time

import aiosqlite


async def put(conn: aiosqlite.Connection, key: str, value: str | float | int, *, now: float | None = None) -> None:
    await conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), time.time() if now is None else now),
    )


async def get(conn: aiosqlite.Connection, key: str) -> tuple[str, float] | None:
    async with conn.execute("SELECT value, updated_at FROM meta WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return (str(row["value"]), float(row["updated_at"])) if row else None


async def beat(conn: aiosqlite.Connection, name: str, *, now: float | None = None) -> None:
    at = time.time() if now is None else now
    await put(conn, f"beat:{name}", at, now=at)


async def last_beat(conn: aiosqlite.Connection, name: str) -> float | None:
    got = await get(conn, f"beat:{name}")
    return float(got[0]) if got else None
