"""Print the go-live gates for the current account: `python -m app.tools.golive_check [--db PATH] [--account NAME]`.

Exit code 0 = every gate passes, 1 = at least one fails. Read-only.
"""

import argparse
import asyncio
import sys

from app.config import settings
from app.core import golive
from app.db import store


async def run(db: str | None, account: str) -> int:
    # The trader's file (positions and the account live there since D-30), opened read-only: never created.
    async with store.session(db or settings.trader_db_path, read_only=True) as conn:
        async with conn.execute(
            "SELECT net_pnl, closed_at, wallet, reason FROM positions WHERE account = ? AND status = 'CLOSED'",
            (account,),
        ) as cur:
            closed = [dict(r) for r in await cur.fetchall()]
        async with conn.execute("SELECT halted FROM account_state WHERE account = ?", (account,)) as cur:
            row = await cur.fetchone()
    halted = bool(row["halted"]) if row else False
    results = golive.gates(closed, halted=halted)
    print(f"Go-live check for account {account}")
    for g in results:
        print(f"  {'PASS' if g.passed else 'FAIL'}  {g.name}: {g.detail}")
    ok = all(g.passed for g in results)
    print("VERDICT:", "ALL GATES PASS — the user may switch MODE=live" if ok else "STAY ON PAPER")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    parser.add_argument("--account", default=settings.account)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.db, args.account)))


if __name__ == "__main__":
    main()
