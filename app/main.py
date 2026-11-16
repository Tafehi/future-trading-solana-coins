"""The bot: one process, one container. `python -m app.main`.

The Solana bot: Jupiter Perps traders copied onto KuCoin (split from future-trading-coins on 2026-11-14, D-19).

Loops (each beats `meta beat:<name>`; `/health` fails when one goes quiet):
- jupiter    every JUPITER_POLL_SECONDS: Jupiter Perps change feed -> episodes, signals; grading every
             JUPITER_GRADE_MINUTES
- jhistory   every 60 s: Jupiter owners' history from the chain, inside the daily call budget (item 1)
- events     every second: signals -> positions (`trading/copier.py`)
- risk       every RISK_LOOP_SECONDS: kill switch, mark, exits (`trading/risk.py`), shadow-copy exits (item 4)
- cold       every COLD_LOOP_SECONDS: markets, funding, equity + ledger check, Telegram digest, recorder, heartbeat
- reconcile  live only, every RECONCILE_SECONDS: our positions against KuCoin's

Boot order: settings (a bad MODE=live config already exited), migrate, seed the account, live pre-flight, first
market refresh, then the loops.
"""

import asyncio
import signal
import sys
import time
from collections.abc import Awaitable, Callable

import httpx

from app.api import health
from app.config import settings
from app.data import jupiter_perps as jp
from app.data.http import DataError
from app.data.kucoin import KucoinFutures
from app.data.kucoin_private import AuthError, KucoinPrivate
from app.data.market import JUPITER_COINS, MarketCache
from app.data.solana_rpc import SolanaRpc
from app.db import jobclock, meta, migrations, store
from app.jobs import jupiter, jupiter_history
from app.notifications import digest, messages
from app.notifications.telegram import TelegramNotifier
from app.record import episodes_export
from app.record.recorder import Recorder
from app.trading import ledger, risk, shadow
from app.trading.copier import Copier
from app.trading.executor import Executor
from app.trading.kucoin_executor import KucoinExecutor
from app.trading.paper_executor import PaperExecutor
from app.utils.logging import get_logger

logger = get_logger(__name__)

MARKET_STALE_ALARM_S = 300.0


class PreflightFailed(RuntimeError):
    pass


async def live_preflight(client: KucoinPrivate) -> None:
    """MODE=live only. Refuse to start unless the key works, the balance is there and KuCoin holds nothing unknown."""
    try:
        overview = await client.account_overview()
        held = await client.positions()
    except AuthError as exc:
        raise PreflightFailed(f"KuCoin refused the key: {exc}") from exc
    except DataError as exc:
        raise PreflightFailed(f"KuCoin account check failed: {exc}") from exc
    equity = float(overview.get("accountEquity") or 0.0)
    if equity < settings.live_start_balance:
        raise PreflightFailed(
            f"futures account equity ${equity:,.2f} < LIVE_START_BALANCE ${settings.live_start_balance:,.2f}"
        )
    async with store.session() as conn:
        ours = {p.symbol for p in await ledger.open_positions(conn, settings.account)}
    unknown = [str(p["symbol"]) for p in held if str(p["symbol"]) not in ours]
    if unknown:
        raise PreflightFailed(f"KuCoin holds positions the bot does not know: {', '.join(unknown)}. Close them first.")
    logger.info("live pre-flight passed: equity $%.2f, %d known positions", equity, len(held))


class Bot:
    def __init__(self) -> None:
        self.kf = KucoinFutures()
        self.market = MarketCache()
        self.notifier = TelegramNotifier()
        self.recorder = Recorder()
        self.solana = SolanaRpc()
        self.private: KucoinPrivate | None = KucoinPrivate() if settings.is_live else None
        self.executor: Executor = KucoinExecutor(self.private) if self.private else PaperExecutor(self.kf)
        self.copier = Copier(self.kf, self.market, self.executor)
        self.stop = asyncio.Event()
        self.last_market_ok = time.time()
        self.last_bot_rows = time.time()
        self.last_shadow_rows = time.time()

    async def alarm(self, title: str, detail: str) -> None:
        await digest.send_alarm(self.notifier, title, detail)

    async def boot(self) -> None:
        await migrations.migrate()
        async with store.session() as conn:
            if await ledger.seed(conn, settings.account, settings.start_balance):
                logger.info("account %s created with $%.2f", settings.account, settings.start_balance)
        if self.private is not None:
            await live_preflight(self.private)
        while not await self.market.refresh(self.kf):
            logger.warning("boot: first market refresh failed, retrying in 15 s")
            await asyncio.sleep(15)
        async with store.session() as conn:
            equity = await ledger.equity(conn, settings.account)
            async with conn.execute("SELECT COUNT(*) AS n FROM wallets WHERE tracked = 1") as cur:
                row = await cur.fetchone()
        await self.notifier.send(messages.started(equity, int(row["n"]) if row else 0))

    async def _loop(self, name: str, every: float, body: Callable[[], Awaitable[None]]) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                await body()
            except AuthError as exc:
                await self.alarm("KuCoin auth failed", str(exc))
            except Exception as exc:  # noqa: BLE001 — one bad pass must not end the loop
                logger.exception("loop %s failed: %s", name, exc)
            async with store.session() as conn:
                await meta.beat(conn, name)
            wait = max(0.0, every - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
            except TimeoutError:
                pass

    # ── loop bodies ───────────────────────────────────────────────────────────────────────
    async def events_pass(self) -> None:
        await self.copier.handle_pending()

    async def risk_pass(self) -> None:
        if risk.kill_switch_on():
            closed = await risk.close_everything(self.kf, self.market, self.executor)
            await self.alarm("Kill switch", f"{settings.stop_file} found: {len(closed)} positions closed, stopping")
            self.stop.set()
            return
        await risk.mark_and_exit(self.kf, self.market, self.executor)
        if settings.shadow_enabled:
            await shadow.mark_and_exit(self.market)

    def jupiter_marks(self) -> dict[str, float]:
        marks: dict[str, float] = {}
        for coin in JUPITER_COINS:
            m = self.market.mapping.get(coin)
            c = self.market.contract(m.symbol) if m else None
            if c and c.mark_price > 0:
                marks[coin] = c.mark_price
        return marks

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
        if await jobclock.claim("jupiter_grade", interval_s=settings.jupiter_grade_minutes * 60):
            await jupiter.grade_owners()

    async def jhistory_pass(self) -> None:
        if not (settings.jupiter_enabled and settings.jupiter_history_enabled and self.solana.enabled):
            return
        await jupiter_history.step(self.solana, deadline=time.time() + 50)

    async def cold_pass(self) -> None:
        now = time.time()
        # KuCoin market data is NOT recorded here: the KuCoin bot (future-trading-kucoins) records it already.
        if await self.market.refresh(self.kf):
            self.last_market_ok = now
        elif now - self.last_market_ok > MARKET_STALE_ALARM_S:
            await self.alarm("Market data stale", f"no KuCoin contract refresh for {now - self.last_market_ok:.0f}s")
        if await jobclock.claim("funding", interval_s=300):
            await risk.settle_funding(self.kf)
        if await jobclock.claim("snapshot", interval_s=300):
            halted = await risk.snapshot()
            if halted:
                await self.alarm("Account halted", halted)
        await digest.run_digest(self.notifier)
        if settings.record_enabled:
            if await jobclock.claim("grades_snapshot", interval_s=86400):
                await self.record_grades(now)
            if self.recorder.due(now):
                await episodes_export.export_new(self.recorder)  # Jupiter wallet history (2026-11-16)
                await self.record_bot_positions()
                await self.record_shadow_positions()
                self.recorder.flush(now=now)
        if settings.heartbeat_url and await jobclock.claim("heartbeat", interval_s=300):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(settings.heartbeat_url)
            except httpx.HTTPError as exc:
                logger.warning("heartbeat failed: %s", type(exc).__name__)

    async def record_grades(self, now: float) -> None:
        async with store.session() as conn:
            async with conn.execute(
                "SELECT address, source, tier, style, trades, profit_factor, max_drawdown, score, copy_weight, "
                "tracked, graded_at, reason FROM wallets WHERE graded_at IS NOT NULL"
            ) as cur:
                rows = [dict(r) | {"snapshot_ms": int(now * 1000)} for r in await cur.fetchall()]
        self.recorder.add_rows("solana-bot/grades", rows)

    async def record_bot_positions(self) -> None:
        async with store.session() as conn:
            async with conn.execute(
                "SELECT * FROM positions WHERE status = 'CLOSED' AND closed_at >= ?", (self.last_bot_rows,)
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            self.last_bot_rows = max(float(r["closed_at"]) for r in rows) + 1e-6
            self.recorder.add_rows("solana-bot/positions", rows)

    async def record_shadow_positions(self) -> None:
        async with store.session() as conn:
            async with conn.execute(
                "SELECT * FROM shadow_positions WHERE status = 'CLOSED' AND closed_at >= ?", (self.last_shadow_rows,)
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            self.last_shadow_rows = max(float(r["closed_at"]) for r in rows) + 1e-6
            self.recorder.add_rows("solana-bot/shadow", rows)

    async def reconcile_pass(self) -> None:
        if self.private is None:
            return
        problems = await risk.live_reconcile(self.private)
        if problems:
            await self.alarm("Reconcile mismatch, entries paused", "; ".join(problems))

    async def run(self) -> None:
        await self.boot()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        tasks = [
            asyncio.create_task(health.serve()),
            asyncio.create_task(self._loop("events", 1.0, self.events_pass)),
            asyncio.create_task(self._loop("risk", settings.risk_loop_seconds, self.risk_pass)),
            asyncio.create_task(self._loop("cold", settings.cold_loop_seconds, self.cold_pass)),
            asyncio.create_task(self._loop("jupiter", settings.jupiter_poll_seconds, self.jupiter_pass)),
            asyncio.create_task(self._loop("jhistory", 60, self.jhistory_pass)),
        ]
        if self.private is not None:
            tasks.append(asyncio.create_task(self._loop("reconcile", settings.reconcile_seconds, self.reconcile_pass)))
        await self.stop.wait()
        logger.info("stopping: flushing the recorder")
        self.recorder.flush()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.close()

    async def close(self) -> None:
        for c in (self.kf, self.solana, self.notifier):
            await c.close()
        if self.private is not None:
            await self.private.close()


def main() -> None:
    logger.info("solana-future-coin bot starting: MODE=%s account=%s", settings.mode, settings.account)
    if risk.kill_switch_on():
        logger.error("%s exists: the kill switch is on. Remove it to start.", settings.stop_file)
        # Idle instead of exiting: docker would restart an exited container in a loop.
        while risk.kill_switch_on():
            time.sleep(30)
    try:
        asyncio.run(Bot().run())
    except PreflightFailed as exc:
        logger.error("live pre-flight failed, refusing to trade: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
