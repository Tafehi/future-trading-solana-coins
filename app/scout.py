"""The SCOUT (D-30): finds, records, grades and picks Jupiter Perps wallets. `python -m app.scout`.

It never trades and never sends Telegram. It writes only SCOUT_DB_PATH (data/scout.db). The trader reads the copy list
from that file (read-only, once a minute); the scout reads the trader's shadow record (read-only) to pick.

Loops (each beats `meta beat:<name>` in scout.db; /health on SCOUT_HEALTH_PORT fails when one goes quiet):
- jupiter   every JUPITER_POLL_SECONDS: the Jupiter Perps change feed -> owners, positions, episodes
            (`jobs/jupiter.py`); the hourly re-read of stored positions; recorder `jupiter/positions`
- jhistory  every 60 s: owners' past from Jupiter's trade API (the chain as the fallback), inside the call budget
- grade     every 60 s: once JUPITER_GRADE_MINUTES have passed, grade and pick. Its own loop, so a failing feed poll
            cannot stop the picks; an unreadable shadow record keeps the old pick and retries in 60 s
- cold      every COLD_LOOP_SECONDS: KuCoin market (the episodes' exit marks), daily grades snapshot, episodes
            export, recorder flush

Boot: settings, this program's file, migrate, the one-time owner seed from the trader's file (`jobs/jupiter.
seed_owners`), first market refresh, then the loops.
"""

import asyncio
import time

from app import runtime
from app.api import health
from app.config import settings
from app.data import jupiter_perps as jp
from app.data.jupiter_api import JupiterApi
from app.data.kucoin import KucoinFutures
from app.data.market import JUPITER_COINS, MarketCache
from app.data.solana_rpc import SolanaRpc
from app.db import jobclock, migrations, store
from app.jobs import jupiter, jupiter_history
from app.record import episodes_export
from app.record.recorder import Recorder
from app.utils.logging import get_logger

logger = get_logger(__name__)

MARKET_STALE_WARN_S = 300.0


class Scout:
    def __init__(self) -> None:
        self.kf = KucoinFutures()
        self.market = MarketCache()
        self.recorder = Recorder()
        self.solana = SolanaRpc()
        self.jup_api = JupiterApi(rps=settings.jupiter_api_rps)  # owners' past from Jupiter's trade API (D-29)
        self.last_market_ok = time.time()

    async def boot(self, stop: asyncio.Event) -> None:
        await migrations.migrate()
        await jupiter.seed_owners(settings.trader_db_path)  # once: the owners the single bot had recorded
        while not stop.is_set() and not await self.market.refresh(self.kf):
            logger.warning("boot: first market refresh failed, retrying in 15 s")
            await runtime.pause(stop, 15)

    def jupiter_marks(self) -> dict[str, float]:
        marks: dict[str, float] = {}
        for coin in JUPITER_COINS:
            m = self.market.mapping.get(coin)
            c = self.market.contract(m.symbol) if m else None
            if c and c.mark_price > 0:
                marks[coin] = c.mark_price
        return marks

    # ── loop bodies ───────────────────────────────────────────────────────────────────────
    async def jupiter_pass(self) -> None:
        if not settings.jupiter_enabled or not self.solana.enabled:
            return
        now = time.time()
        last = await jupiter.last_poll()
        block = jp.block_of(now)
        updated = await self.solana.updated_positions(block)
        if last is not None and jp.block_of(last) != block:
            # The block turned since the last poll: updates between that poll and the turn are in the old block.
            updated += await self.solana.updated_positions(jp.block_of(last))
        marks = self.jupiter_marks()
        res = await jupiter.process_updates(updated, marks, now=now)
        if settings.record_enabled:
            self.recorder.add_jupiter([p for p in updated if p.is_open], now=now)
        if res.opens or res.closes:
            logger.info("jupiter: %d updated, %d opened, %d closed, %d episodes%s", res.seen, res.opens, res.closes,
                        res.episodes, " (baseline)" if res.baseline else "")  # fmt: skip
        gap = last is not None and jupiter.is_baseline(last, now)
        if gap or await jobclock.claim("jupiter_verify", interval_s=3600):
            current = await self.solana.positions(await jupiter.known_pubkeys())
            closed = await jupiter.verify_known(current, marks, now=now)
            if closed:
                logger.info("jupiter: verify closed %d stored positions", closed)

    async def jhistory_pass(self) -> None:
        if not (settings.jupiter_enabled and settings.jupiter_history_enabled and self.solana.enabled):
            return
        await jupiter_history.step(self.solana, deadline=time.time() + 50, api=self.jup_api)

    async def grade_pass(self) -> None:
        if not settings.jupiter_enabled:
            return
        if not await jobclock.claim("jupiter_grade", interval_s=settings.jupiter_grade_minutes * 60):
            return
        try:
            await jupiter.grade_owners(shadow_db=settings.trader_db_path)
        except jupiter.ShadowUnreadable as exc:
            logger.warning("grade: %s; trying again in %ds", exc, jupiter.SHADOW_RETRY_S)
            await jobclock.schedule("jupiter_grade", jupiter.SHADOW_RETRY_S)

    async def cold_pass(self) -> None:
        now = time.time()
        # KuCoin market data is NOT recorded here: the KuCoin bot (future-trading-kucoins) records it already.
        if await self.market.refresh(self.kf):
            self.last_market_ok = now
        elif now - self.last_market_ok > MARKET_STALE_WARN_S:
            # The scout sends no Telegram; the episodes' exit marks go stale, so say it in the log and in /health.
            logger.warning("market data stale: no KuCoin contract refresh for %.0fs", now - self.last_market_ok)
        if settings.record_enabled:
            if await jobclock.claim("grades_snapshot", interval_s=86400):
                await self.record_grades(now)
            if self.recorder.due(now):
                await episodes_export.export_new(self.recorder)  # Jupiter wallet history (2026-11-16)
                self.recorder.flush(now=now)

    async def record_grades(self, now: float) -> None:
        async with store.session() as conn:
            async with conn.execute(
                "SELECT address, source, tier, style, trades, profit_factor, max_drawdown, score, copy_weight, "
                "tracked, graded_at, reason FROM wallets WHERE graded_at IS NOT NULL"
            ) as cur:
                rows = [dict(r) | {"snapshot_ms": int(now * 1000)} for r in await cur.fetchall()]
        self.recorder.add_rows("solana-bot/grades", rows)

    async def run(self, stop: asyncio.Event) -> None:
        await self.boot(stop)
        tasks = [
            asyncio.create_task(health.serve(settings.scout_health_port, health.scout_snapshot)),
            asyncio.create_task(runtime.loop("jupiter", settings.jupiter_poll_seconds, self.jupiter_pass, stop)),
            asyncio.create_task(runtime.loop("jhistory", 60, self.jhistory_pass, stop)),
            asyncio.create_task(runtime.loop("grade", 60, self.grade_pass, stop)),
            asyncio.create_task(runtime.loop("cold", settings.cold_loop_seconds, self.cold_pass, stop)),
        ]
        await stop.wait()
        logger.info("scout stopping: flushing the recorder")
        self.recorder.flush()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for c in (self.kf, self.solana, self.jup_api):
            await c.close()


def main() -> None:
    runtime.use_db(settings.scout_db_path)
    logger.info("solana-future-coin scout starting: db %s", settings.scout_db_path)
    runtime.run_forever(Scout().run)


if __name__ == "__main__":
    main()
