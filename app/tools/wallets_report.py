"""The wallet pool at a glance: `python -m app.tools.wallets_report [--scout-db PATH] [--trader-db PATH]`. Read-only.

Since D-30 the pool lives in the SCOUT's file (wallets, Jupiter owners, the history budget) and the shadow copies in
the TRADER's file. Both are opened read-only, so the report never creates or changes either; a file that cannot be
read is reported, not created. statusCheck.sh runs it inside the scout container (both files are on the same volume).

Per source and tier: graded, tracked, watched (the only source is Jupiter since the split, D-19). The shadow-copy
record of the tracked and best watched wallets. How many Jupiter owners have their past read (Jupiter's trade API,
or the chain as the fallback, D-29), and today's chain history calls against the budget.
"""

import argparse
import asyncio
import sqlite3
import time

from app.config import settings
from app.db import crossread, store
from app.db.crossread import ShadowStats
from app.jobs import jupiter_history


async def run(scout_db: str, trader_db: str) -> None:
    now = time.time()
    async with store.session(scout_db, read_only=True) as conn:
        async with conn.execute(
            "SELECT source, tier, COUNT(*) AS n, SUM(tracked) AS t, SUM(watched) AS w FROM wallets "
            "WHERE graded_at >= ? GROUP BY source, tier ORDER BY source, tier",
            (now - settings.track_max_grade_age_hours * 3600,),
        ) as cur:
            pool = [dict(r) for r in await cur.fetchall()]
        async with conn.execute(
            "SELECT address, source, tier, tracked, copy_weight FROM wallets WHERE tracked = 1 OR watched = 1"
        ) as cur:
            members = [dict(r) for r in await cur.fetchall()]
        async with conn.execute("SELECT COUNT(*) AS n, SUM(backfilled_at IS NOT NULL) AS b FROM jup_owners") as cur:
            owners = await cur.fetchone()
    calls = await jupiter_history.calls_today(now=now, db_path=scout_db, read_only=True)
    stats: dict[str, ShadowStats] = {}
    n_open: int | str = "?"
    try:
        stats = await crossread.shadow_stats(trader_db, read_only=True, now=now)
        async with store.session(trader_db, read_only=True) as conn:
            async with conn.execute("SELECT COUNT(*) AS n FROM shadow_positions WHERE status = 'OPEN'") as cur:
                row = await cur.fetchone()
        n_open = int(row["n"]) if row else 0
    except sqlite3.Error as exc:
        print(f"  (the trader's file is unreadable: {type(exc).__name__}; shadow copies not shown)")
    print(f"Wallet pool (graded in the last {settings.track_max_grade_age_hours:g} h)")
    for r in pool:
        print(f"  {r['source']:12} {r['tier']:7} graded {r['n']:5}  tracked {r['t'] or 0:3}  watched {r['w'] or 0:3}")
    print(f"Shadow copies: {n_open} open; record over {settings.shadow_window_days:g} days:")
    ranked = sorted(members, key=lambda m: (-(m["tracked"] or 0), -(stats[m["address"]].mean or -99)
                                            if m["address"] in stats else 99))  # fmt: skip
    for m in ranked[:25]:
        st = stats.get(m["address"])
        rec = (
            f"{st.n:3} copies, mean {st.mean:+.2f}%, {st.wins} won" if st and st.mean is not None else "no closed copy"
        )
        role = "TRACKED" if m["tracked"] else "watched"
        print(f"  {role:7} {m['source']:11} {m['tier']:6} w{m['copy_weight'] or 0:<2} {m['address'][:12]}…  {rec}")
    n_own, n_back = (owners["n"], owners["b"] or 0) if owners else (0, 0)
    print(f"Jupiter: {n_own} owners recorded, {n_back} with their past read ({settings.jupiter_history_source}); "
          f"chain fallback calls today {calls} / {settings.jupiter_history_calls_per_day}")  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scout-db", default=settings.scout_db_path)
    parser.add_argument("--trader-db", default=settings.trader_db_path)
    args = parser.parse_args()
    asyncio.run(run(args.scout_db, args.trader_db))


if __name__ == "__main__":
    main()
