"""Print every position of the account, newest first: `python -m app.tools.trade_log [--db PATH] [--limit N]`.

The ids match the Telegram `#id`. Read-only.
"""

import argparse
import asyncio
from datetime import UTC, datetime

from app.config import settings
from app.db import store


def _t(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M") if ts else "-"


async def run(db: str | None, account: str, limit: int) -> None:
    # The trader's file (positions and the account live there since D-30), opened read-only: never created.
    async with store.session(db or settings.trader_db_path, read_only=True) as conn:
        async with conn.execute(
            "SELECT * FROM positions WHERE account = ? ORDER BY id DESC LIMIT ?", (account, limit)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    print(f"{'#':>5} {'opened':>11} {'closed':>11} {'side':5} {'symbol':14} {'entry':>12} {'exit':>12} "
          f"{'net':>10} reason  wallet")  # fmt: skip
    for r in rows:
        net = "open" if r["status"] == "OPEN" else f"{float(r['net_pnl'] or 0):+.2f}"
        print(
            f"{r['id']:>5} {_t(r['opened_at']):>11} {_t(r['closed_at']):>11} {r['side']:5} {r['symbol']:14} "
            f"{float(r['entry_px']):>12.6g} {float(r['exit_px'] or 0):>12.6g} {net:>10} {r['reason'] or '-':7} "
            f"{r['source']}:{str(r['wallet'])[:10]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    parser.add_argument("--account", default=settings.account)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(run(args.db, args.account, args.limit))


if __name__ == "__main__":
    main()
