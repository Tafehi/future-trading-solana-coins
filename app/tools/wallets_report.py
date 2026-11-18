"""The wallet pool at a glance: `python -m app.tools.wallets_report [--db PATH]`. Read-only.

Per source and tier: graded, tracked, watched (the only source is Jupiter since the split, D-19). The shadow-copy
record of the tracked and best watched wallets. How many Jupiter owners have chain history, and today's Jupiter
history RPC calls against the budget.
"""

import argparse
import asyncio
import time

from app.config import settings
from app.db import store
from app.jobs import jupiter_history
from app.trading import shadow


async def run(db: str | None) -> None:
    if db:
        settings.db_path = db
    now = time.time()
    async with store.session() as conn:
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
        async with conn.execute("SELECT COUNT(*) AS n FROM shadow_positions WHERE status = 'OPEN'") as cur:
            open_shadows = await cur.fetchone()
    stats = await shadow.stats(now=now)
    print(f"Wallet pool (graded in the last {settings.track_max_grade_age_hours:g} h)")
    for r in pool:
        print(f"  {r['source']:12} {r['tier']:7} graded {r['n']:5}  tracked {r['t'] or 0:3}  watched {r['w'] or 0:3}")
    n_open = open_shadows["n"] if open_shadows else 0
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
    calls = await jupiter_history.calls_today(now=now)
    print(f"Jupiter: {n_own} owners recorded, {n_back} with chain history; "
          f"history calls today {calls} / {settings.jupiter_history_calls_per_day}")  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    asyncio.run(run(parser.parse_args().db))


if __name__ == "__main__":
    main()
