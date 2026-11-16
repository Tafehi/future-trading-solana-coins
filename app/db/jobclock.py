"""A persisted clock for jobs whose cadence is longer than the process lives.

Wallet discovery runs once a day and costs most of an hour of Hyperliquid weight, and the box restarts
on every deploy. An in-memory timer would reset to "never run" and re-run the whole discovery on each
deploy, so the clock lives in the database: `meta` keys `jobclock:<name>` holding the epoch second the job is next due.

Rules:
- It stores `next_run_at`, not `last_run_at`, so a short retry is just `schedule(name, retry_s)`.
- `claim()` writes the next due time BEFORE the job runs. The interval is a spend limit: marking
  afterwards would make an always-failing job run every cycle. Nothing retries by default.
- A job that has never run is due.
- Exactly `interval_s` elapsed is due (`>=`, not `>`).
- `interval_s <= 0` raises. "Always due" is the expensive reading of zero.
"""

from __future__ import annotations

import time

from app.db import store
from app.utils.logging import get_logger

logger = get_logger(__name__)

KEY_PREFIX = "jobclock:"

DISCOVERY = "wallet_discovery"


def _key(name: str) -> str:
    return f"{KEY_PREFIX}{name}"


async def next_run_at(name: str, *, db_path: str | None = None) -> float:
    """The epoch second this job becomes due, or `0.0` when it has never run.

    `0.0` rather than `None`: every caller treats "never run" the same as "due long ago".
    """
    async with store.session(db_path) as conn:
        async with conn.execute("SELECT value FROM meta WHERE key = ?", (_key(name),)) as cur:
            row = await cur.fetchone()
    if row is None:
        return 0.0
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        # A corrupt value reads as "due", not as an error. The alternative is a cold path that refuses
        # to run any job because one row is unparseable.
        logger.warning("jobclock: %s holds an unparseable value, treating it as due", name)
        return 0.0


async def schedule(name: str, delay_s: float, *, now: float | None = None, db_path: str | None = None) -> float:
    """Make `name` due `delay_s` from now. Returns the stored due time.

    The one way anything writes this clock. A caller retrying after a transient failure passes a short
    `delay_s`; `claim` passes the full interval.
    """
    at = time.time() if now is None else now
    due_at = at + max(0.0, delay_s)
    async with store.session(db_path) as conn:
        await conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_key(name), repr(due_at), at),
        )
    return due_at


async def due(name: str, *, interval_s: float, now: float | None = None, db_path: str | None = None) -> bool:
    """Is `name` due? `interval_s` is only used to reject a nonsensical cadence — see the docstring.

    Raises:
        ValueError: when `interval_s <= 0`, because "always due" is the expensive reading of zero.
    """
    if interval_s <= 0:
        raise ValueError(f"{name}: interval_s must be positive; got {interval_s}. Zero would run it every cycle.")
    at = time.time() if now is None else now
    # `>=`: exactly at the boundary IS due.
    return at >= await next_run_at(name, db_path=db_path)


async def claim(name: str, *, interval_s: float, now: float | None = None, db_path: str | None = None) -> bool:
    """True at most once per `interval_s`, and the next due time is written **before** you run.

    A caller whose job then fails transiently calls `schedule(name, retry_s)` itself. Nothing is retried
    by default; see the module docstring.
    """
    if not await due(name, interval_s=interval_s, now=now, db_path=db_path):
        return False
    await schedule(name, interval_s, now=now, db_path=db_path)
    return True


async def snapshot(*, now: float | None = None, db_path: str | None = None) -> dict[str, float]:
    """Every job's seconds-until-due, for the health endpoint and `statusCheck.sh`.

    Negative means overdue by that many seconds, so a stalled cold path shows growing negatives.
    """
    at = time.time() if now is None else now
    async with store.session(db_path) as conn:
        async with conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"{KEY_PREFIX}%",)) as cur:
            rows = await cur.fetchall()
    out: dict[str, float] = {}
    for row in rows:
        name = str(row["key"])[len(KEY_PREFIX) :]
        try:
            out[name] = float(row["value"]) - at
        except (TypeError, ValueError):
            out[name] = 0.0
    return out


__all__: list[str] = [
    "DISCOVERY",
    "KEY_PREFIX",
    "claim",
    "due",
    "next_run_at",
    "schedule",
    "snapshot",
]
