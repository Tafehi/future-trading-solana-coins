"""Every read of the OTHER program's database lives here, always read-only (D-30, 2026-11-18).

The scout writes `scout.db` (wallets, jup_*), the trader writes its own file (signals, positions, ledger, shadow).
- The trader reads the copy list from scout.db once a minute: the tracked + watched wallets with what the copier
  needs, the scout's last pick time (`jup:picked_at`, so a broken scout shows as a STALE list, not a fresh one) and its
  last feed poll, all in ONE read transaction; plus the known Position accounts of the listed owners (for the
  safety-net poll: the trader never runs getProgramAccounts, which Chainstack caps at 3 per 20 s on Jupiter Perps).
- The scout reads the trader's shadow results to pick wallets (proven first, losing ones never tracked), and once,
  on a fresh scout.db, the single bot's OWNER LIST from the trader's file (`owners`): the change feed only shows owners
  whose accounts change, and Jupiter's trade API needs the address, so a list once lost cannot be fetched again.

A missing file raises (mode=ro never creates one); the callers keep their last answer and say so.
"""

import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.data.jupiter_perps import SOURCE
from app.db import store

PICKED_AT_KEY = "jup:picked_at"
LAST_POLL_KEY = "jup:last_poll"


@dataclass(frozen=True)
class Entry:
    """One copy-list wallet, as the copier needs it."""

    address: str
    tier: str
    tracked: bool  # False = watched (shadow copies only)
    copy_weight: int
    account_value: float
    score: float  # the precedence score (a much better wallet replaces a copy)
    rank_score: float  # the grade score the scout ranked it by (`count_score`)


@dataclass(frozen=True)
class CopyListRead:
    entries: list[Entry]
    picked_at: float | None  # when the scout last picked (None = never)
    scout_fed_at: float | None  # the scout's last successful Jupiter feed poll


@dataclass(frozen=True)
class ShadowStats:
    n: int
    mean: float | None
    wins: int


ENTRY_COLUMNS = "address, tier, tracked, watched, copy_weight, account_value, score, count_score"


def entry_of(r: Any) -> Entry:
    """A `wallets` row (ENTRY_COLUMNS) -> the copier's view of it."""
    return Entry(
        str(r["address"]), str(r["tier"]), bool(r["tracked"]), int(r["copy_weight"] or 1),
        float(r["account_value"] or 0.0), float(r["score"] or 0.0), float(r["count_score"] or 0.0),
    )  # fmt: skip


async def copy_list(scout_db: str) -> CopyListRead:
    async with store.session(scout_db, read_only=True) as conn:
        await conn.execute("BEGIN")  # one snapshot: the list and its pick time belong together
        async with conn.execute(
            f"SELECT {ENTRY_COLUMNS} FROM wallets WHERE source = ? AND (tracked = 1 OR watched = 1)",  # noqa: S608
            (SOURCE,),
        ) as cur:
            rows = await cur.fetchall()
        async with conn.execute(
            "SELECT key, value FROM meta WHERE key IN (?, ?)", (PICKED_AT_KEY, LAST_POLL_KEY)
        ) as cur:
            got = {str(r["key"]): float(r["value"]) for r in await cur.fetchall()}
    return CopyListRead([entry_of(r) for r in rows], got.get(PICKED_AT_KEY), got.get(LAST_POLL_KEY))


async def owners(trader_db: str) -> list[tuple[str, float, float]]:
    """(owner, first_seen, max_open_usd) of every Jupiter owner in the trader's file (the single bot's, frozen at D-30,
    BUG B-28)."""
    async with store.session(trader_db, read_only=True) as conn:
        async with conn.execute("SELECT owner, first_seen, max_open_usd FROM jup_owners") as cur:
            return [(str(r["owner"]), float(r["first_seen"]), float(r["max_open_usd"] or 0.0))
                    for r in await cur.fetchall()]  # fmt: skip


async def owner_keys(scout_db: str, owners: list[str]) -> dict[str, set[str]]:
    """owner -> every Position account the scout knows for it (open positions + accounts of its history)."""
    if not owners:
        return {}
    marks = ", ".join("?" for _ in owners)
    out: dict[str, set[str]] = {o: set() for o in owners}
    async with store.session(scout_db, read_only=True) as conn:
        async with conn.execute(
            f"SELECT owner, pubkey AS k FROM jup_positions WHERE owner IN ({marks}) "  # noqa: S608 (placeholders only)
            f"UNION SELECT owner, position_key AS k FROM jup_episodes WHERE position_key IS NOT NULL "
            f"AND owner IN ({marks})",
            (*owners, *owners),
        ) as cur:
            for r in await cur.fetchall():
                out[str(r["owner"])].add(str(r["k"]))
    return out


async def shadow_stats(
    db_path: str | None = None, *, read_only: bool = False, now: float | None = None
) -> dict[str, ShadowStats]:
    """Per wallet: closed shadow copies in SHADOW_WINDOW_DAYS, their mean return and wins. The trader reads its own
    file; the scout passes the trader's file with read_only=True."""
    at = time.time() if now is None else now
    since = at - settings.shadow_window_days * 86400
    async with store.session(db_path, read_only=read_only) as conn:
        async with conn.execute(
            "SELECT wallet, COUNT(*) AS n, AVG(ret_pct) AS mean, SUM(CASE WHEN ret_pct > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM shadow_positions WHERE status = 'CLOSED' AND closed_at >= ? GROUP BY wallet",
            (since,),
        ) as cur:
            return {
                str(r["wallet"]): ShadowStats(int(r["n"]), float(r["mean"]) if r["mean"] is not None else None,
                                              int(r["wins"] or 0))
                for r in await cur.fetchall()
            }  # fmt: skip
