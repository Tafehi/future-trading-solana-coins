"""Jupiter wallet history from the chain (user, 2026-11-13, item 1): grade an owner on day one, not after 14 days.

For each candidate owner (biggest open position first, at least JUPITER_HISTORY_MIN_SIZE_USD, never backfilled):
1. `getProgramAccounts` with the owner at byte 8 -> all its Position accounts (one per market/side/collateral).
2. `getSignaturesForAddress` per account, newest first, back to JUPITER_HISTORY_DAYS (at most
   JUPITER_HISTORY_MAX_TX_PER_POSITION). Failed transactions are skipped without fetching them.
3. `getTransaction` per signature -> close events (`data/jupiter_events.py`) -> complete episodes with the REAL PnL.
4. The owner's feed-estimated episodes up to now are replaced by the chain ones; `history_from` = the start of what was
   read, so `grade_owners` counts it as history.

Every RPC call counts against JUPITER_HISTORY_CALLS_PER_DAY (meta key `jhist:calls:<UTC day>`) and is paced at
JUPITER_HISTORY_RPS. When the day's budget runs out mid-owner, nothing is written for that owner; it is done again
when budget returns (simple and safe; costs a few repeated calls).
"""

import time
from datetime import UTC, datetime

from app.config import settings
from app.data import jupiter_events as je
from app.data.http import DataError, RateLimiter
from app.data.solana_rpc import SolanaRpc
from app.db import meta, store
from app.utils.logging import get_logger

logger = get_logger(__name__)


MAX_FAILURES = 3


async def _note_failure(owner: str) -> int:
    key = f"jhist:fail:{owner}"
    async with store.session() as conn:
        got = await meta.get(conn, key)
        n = (int(float(got[0])) if got else 0) + 1
        await meta.put(conn, key, n)
    return n


class OutOfBudget(Exception):
    pass


def _day_key(now: float) -> str:
    return f"jhist:calls:{datetime.fromtimestamp(now, UTC):%Y-%m-%d}"


async def calls_today(*, now: float | None = None) -> int:
    at = time.time() if now is None else now
    async with store.session() as conn:
        got = await meta.get(conn, _day_key(at))
    return int(float(got[0])) if got else 0


class Budget:
    """The daily call cap, persisted, plus the pace."""

    def __init__(self, *, now: float | None = None) -> None:
        self.limiter = RateLimiter(settings.jupiter_history_rps)
        self.now = now

    async def spend(self) -> None:
        at = time.time() if self.now is None else self.now
        key = _day_key(at)
        async with store.session() as conn:
            got = await meta.get(conn, key)
            used = int(float(got[0])) if got else 0
            if used >= settings.jupiter_history_calls_per_day:
                raise OutOfBudget(f"{used} history calls today")
            await meta.put(conn, key, used + 1, now=at)
        await self.limiter.acquire()


async def next_owners(limit: int) -> list[str]:
    async with store.session() as conn:
        async with conn.execute(
            "SELECT owner FROM jup_owners WHERE backfilled_at IS NULL AND max_open_usd >= ? "
            "ORDER BY max_open_usd DESC LIMIT ?",
            (settings.jupiter_history_min_size_usd, limit),
        ) as cur:
            return [str(r["owner"]) for r in await cur.fetchall()]


async def backfill_owner(rpc: SolanaRpc, owner: str, budget: Budget, *, now: float | None = None) -> int:
    """Read one owner's history and store its episodes. Returns how many episodes. Raises OutOfBudget."""
    at = time.time() if now is None else now
    since = at - settings.jupiter_history_days * 86400
    await budget.spend()
    keys = await rpc.owner_position_keys(owner)
    events: list[je.CloseEvent] = []
    oldest = at
    full = True
    for key in keys:
        before: str | None = None
        taken = 0
        complete = False  # reached the window start, or the account has no older transactions
        while not complete and taken < settings.jupiter_history_max_tx_per_position:
            await budget.spend()
            page = await rpc.signatures(key, before=before, limit=1000)
            for sig in page:
                t = float(sig.get("blockTime") or 0)  # type: ignore[arg-type]
                if t and t < since:
                    complete = True
                    break
                if taken >= settings.jupiter_history_max_tx_per_position:
                    break
                taken += 1
                oldest = min(oldest, t or oldest)
                if sig.get("err") is not None:
                    continue  # failed on chain: no event, no call
                await budget.spend()
                events += je.events_from_transaction(await rpc.transaction(str(sig["signature"])),
                                                     settings.jupiter_program_id)  # fmt: skip
            if len(page) < 1000:
                complete = True
            elif page:
                before = str(page[-1]["signature"])
        full = full and complete
    eps = [e for e in je.episodes(events) if e.owner == owner and e.closed_at >= since]
    history_from = since if full else oldest
    async with store.session() as conn:
        # The chain record replaces what the feed estimated up to now; later closes keep coming from the feed.
        await conn.execute(
            "DELETE FROM jup_episodes WHERE owner = ? AND origin = 'feed' AND closed_at <= ?", (owner, at)
        )
        await conn.executemany(
            "INSERT OR IGNORE INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, "
            "pnl, origin, position_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'chain', ?)",
            [(e.owner, e.coin, e.side, e.size_usd, e.entry_px, e.exit_px, e.opened_at, e.closed_at, e.pnl,
              e.position_key) for e in eps],
        )  # fmt: skip
        await conn.execute(
            "UPDATE jup_owners SET backfilled_at = ?, history_from = ? WHERE owner = ?", (at, history_from, owner)
        )
    logger.info("jupiter history: %s… %d accounts, %d close events, %d episodes since %s", owner[:8], len(keys),
                len(events), len(eps), datetime.fromtimestamp(history_from, UTC).date())  # fmt: skip
    return len(eps)


async def step(rpc: SolanaRpc, *, deadline: float, now: float | None = None) -> int:
    """Backfill owners until `deadline` or the day's budget. Returns how many owners were done."""
    done = 0
    budget = Budget(now=now)
    for owner in await next_owners(20):
        if time.time() >= deadline:
            break
        try:
            await backfill_owner(rpc, owner, budget, now=now)
            done += 1
        except OutOfBudget:
            break
        except DataError as exc:
            # Usually a rate limit or a timeout: stop this pass, retry next minute. An owner that fails
            # MAX_FAILURES times is skipped, so one broken owner cannot block the queue.
            failures = await _note_failure(owner)
            logger.warning("jupiter history: %s… failed (%d/%d): %s", owner[:8], failures, MAX_FAILURES, exc)
            if failures >= MAX_FAILURES:
                async with store.session() as conn:
                    await conn.execute(
                        "UPDATE jup_owners SET backfilled_at = ? WHERE owner = ?",
                        (time.time() if now is None else now, owner),
                    )
            break
    return done
