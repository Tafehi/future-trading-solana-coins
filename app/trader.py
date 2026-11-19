"""The TRADER (D-30): copies the scout's picks onto KuCoin. `python -m app.trader`.

It never grades. It writes only TRADER_DB_PATH (data/solana-future-coin.db: the paper account, positions, ledger,
signals, shadow copies, feed_positions). It reads the copy list from the scout's file (read-only, once a minute).

Feed: a Solana websocket (`data/solana_ws.py`, one programSubscribe per copy-list owner, about 1-2 s after the fill)
and a safety-net poll every FEED_POLL_SECONDS (getMultipleAccounts on the listed owners' known accounts; never
getProgramAccounts). Both go on one queue; one worker applies them in order (`trading/watch.py`), and a written signal
wakes the copier at once.

Loops (each beats `meta beat:<name>` in the trader's file; /health on TRADER_HEALTH_PORT fails when one goes quiet):
- events     every second, and at once on a new signal: signals -> positions (`trading/copier.py`)
- risk       every RISK_LOOP_SECONDS: kill switch, mark, exits (`trading/risk.py`), shadow-copy exits
- cold       every COLD_LOOP_SECONDS: markets, funding, equity + ledger check, Telegram report, alarms, recorder,
             heartbeat
- copylist   every COPYLIST_REFRESH_S: the scout's list -> memory, the websocket's owners, the poll's known accounts
- feedpoll   every FEED_POLL_SECONDS: the safety-net poll -> the worker
- reconcile  live only, every RECONCILE_SECONDS: our positions against KuCoin's

Boot order: settings (a bad MODE=live config already exited), this program's file, migrate, seed the account, live
pre-flight, first market refresh, the feed's last state, the first copy list, then the loops.
"""

import asyncio
import sys
import time
from typing import Any

import httpx

from app import runtime
from app.api import health
from app.config import settings
from app.data.http import DataError
from app.data.jupiter_perps import JupPosition
from app.data.kucoin import KucoinFutures
from app.data.kucoin_private import AuthError, KucoinPrivate
from app.data.market import MarketCache
from app.data.solana_rpc import SolanaRpc
from app.data.solana_ws import SolanaFeed
from app.db import crossread, jobclock, migrations, store
from app.notifications import digest, messages
from app.notifications.telegram import TelegramNotifier
from app.record.recorder import Recorder
from app.trading import ledger, risk, shadow
from app.trading.copier import Copier
from app.trading.copylist import CopyList
from app.trading.executor import Executor
from app.trading.kucoin_executor import KucoinExecutor
from app.trading.paper_executor import PaperExecutor
from app.trading.watch import POLL, WS, Obs, PositionTracker
from app.utils.logging import get_logger

logger = get_logger(__name__)

MARKET_STALE_ALARM_S = 300.0
FEED_DOWN_ALARM_S = 300.0  # the websocket down this long -> alarm (the poll still runs)
SCOUT_FEED_STALE_ALARM_S = 900.0  # the scout's last successful Jupiter feed poll this old -> alarm
QUEUE_MAX = 10_000  # observations waiting for the worker; above it a websocket update is dropped (the poll repeats it)


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


class Trader:
    def __init__(self) -> None:
        self.kf = KucoinFutures()
        self.market = MarketCache()
        self.notifier = TelegramNotifier()
        self.recorder = Recorder()
        self.solana = SolanaRpc()
        self.private: KucoinPrivate | None = KucoinPrivate() if settings.is_live else None
        self.executor: Executor = KucoinExecutor(self.private) if self.private else PaperExecutor(self.kf)
        self.copylist = CopyList()
        self.copier = Copier(self.kf, self.market, self.executor, self.copylist)
        self.tracker = PositionTracker()
        self.queue: asyncio.Queue[Obs] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.feed = SolanaFeed(settings.ws_url, self.on_ws_update)
        self.scout_keys: dict[str, set[str]] = {}  # owner -> its Position accounts the scout knows
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.worker: asyncio.Task[None] | None = None
        self.dropped = 0
        self.started_at = time.time()
        self.last_market_ok = time.time()
        self.last_bot_rows = time.time()
        self.last_shadow_rows = time.time()

    async def alarm(self, title: str, detail: str) -> None:
        await digest.send_alarm(self.notifier, title, detail)

    async def on_loop_error(self, name: str, exc: Exception) -> None:
        if isinstance(exc, AuthError):
            await self.alarm("KuCoin auth failed", str(exc))

    async def boot(self, stop: asyncio.Event) -> None:
        await migrations.migrate()
        async with store.session() as conn:
            if await ledger.seed(conn, settings.account, settings.start_balance):
                logger.info("account %s created with $%.2f", settings.account, settings.start_balance)
        if self.private is not None:
            await live_preflight(self.private)
        while not stop.is_set() and not await self.market.refresh(self.kf):
            logger.warning("boot: first market refresh failed, retrying in 15 s")
            await runtime.pause(stop, 15)
        known = await self.tracker.load()
        try:
            await self.copylist_pass()
        except Exception as exc:  # noqa: BLE001 — no list yet: new opens are refused LIST_STALE until one is read
            logger.warning("boot: copy list unreadable (%s); new opens wait for the scout", type(exc).__name__)
        async with store.session() as conn:
            equity = await ledger.equity(conn, settings.account)
        logger.info("boot: %d feed accounts known, %d copy-list owners", known, len(self.copylist.entries))
        await self.notifier.send(messages.started(equity, self.copylist.tracked))

    # ── the feed ──────────────────────────────────────────────────────────────────────────
    def on_ws_update(self, pubkey: str, pos: JupPosition, slot: int) -> None:
        try:
            self.queue.put_nowait(Obs(pubkey, pos, slot, WS, time.time()))
        except asyncio.QueueFull:
            self.dropped += 1  # the poll reads the account again within FEED_POLL_SECONDS

    async def feed_worker(self) -> None:
        while True:
            obs = await self.queue.get()
            try:
                if await self.tracker.apply(obs, self.copylist):
                    self.wake.set()
            except Exception as exc:  # noqa: BLE001 — one failed write must not stop the feed; the poll repeats it
                logger.exception("feed: applying an observation failed: %s", type(exc).__name__)

    async def copylist_pass(self) -> None:
        read = await crossread.copy_list(settings.scout_db_path)
        added, dropped = self.copylist.replace(read, now=time.time())
        owners = self.copylist.owners_ranked()
        self.scout_keys = await crossread.owner_keys(settings.scout_db_path, owners)
        await self.feed.set_owners(owners)
        if added or dropped:
            logger.info("copy list: %d owners, %d tracked (+%d, -%d)", len(owners), self.copylist.tracked, len(added),
                        len(dropped))  # fmt: skip

    async def feedpoll_pass(self) -> None:
        owners = self.copylist.owners_ranked()
        if not self.solana.enabled or not owners:
            return
        keys = self.tracker.keys_of(owners)
        for o in owners:
            keys |= self.scout_keys.get(o, set())
        if not keys:
            return
        slot, got = await self.solana.positions_at(sorted(keys))
        now = time.time()
        for key, pos in got.items():
            if pos is not None:
                await self.queue.put(Obs(key, pos, slot, POLL, now))

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
        await self.feed_alarms(now)
        if settings.record_enabled and self.recorder.due(now):
            await self.record_bot_positions()
            await self.record_shadow_positions()
            self.recorder.flush(now=now)
        if settings.heartbeat_url and await jobclock.claim("heartbeat", interval_s=300):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(settings.heartbeat_url)
            except httpx.HTTPError as exc:
                logger.warning("heartbeat failed: %s", type(exc).__name__)

    async def feed_alarms(self, now: float) -> None:
        """The copy list, the websocket and the scout's own feed (each alarm at most once an hour, `digest`)."""
        if self.copylist.stale(now) and now - self.started_at >= settings.copylist_max_age_s:
            age = self.copylist.age(now)
            await self.alarm("Copy list stale", ("the scout never picked" if age is None else
                                                 f"the scout's last pick is {age:.0f}s old")
                             + ": new opens are refused (LIST_STALE), exits continue")  # fmt: skip
        down = self.feed.stats()["down_for_s"]
        if self.feed.enabled and down is not None and down > FEED_DOWN_ALARM_S:
            await self.alarm("Solana feed down", f"websocket down for {down:.0f}s; the "
                             f"{settings.feed_poll_seconds:.0f}-s poll still runs, copies come later")  # fmt: skip
        fed = self.copylist.scout_fed_at
        if settings.jupiter_enabled and fed is not None and now - fed > SCOUT_FEED_STALE_ALARM_S:
            await self.alarm("Scout feed stale", f"the scout's last Jupiter feed poll is {now - fed:.0f}s old")

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

    async def health_snapshot(self) -> tuple[int, dict[str, Any]]:
        now = time.time()
        feed = self.feed.stats() | {"queue": self.queue.qsize(), "dropped": self.dropped, **self.tracker.stats()}
        return await health.trader_snapshot(
            list_age=self.copylist.age(now),
            listed={"tracked": self.copylist.tracked, "watched": len(self.copylist.entries) - self.copylist.tracked},
            feed=feed,
            worker_alive=self.worker is not None and not self.worker.done(),
            now=now,
        )

    async def run(self, stop: asyncio.Event) -> None:
        self.stop = stop
        await self.boot(stop)
        self.worker = asyncio.create_task(self.feed_worker())
        tasks = [
            self.worker,
            asyncio.create_task(health.serve(settings.trader_health_port, self.health_snapshot)),
            asyncio.create_task(
                runtime.loop("events", 1.0, self.events_pass, stop, wake=self.wake, on_error=self.on_loop_error)
            ),  # fmt: skip
            asyncio.create_task(
                runtime.loop("risk", settings.risk_loop_seconds, self.risk_pass, stop, on_error=self.on_loop_error)
            ),  # fmt: skip
            asyncio.create_task(
                runtime.loop("cold", settings.cold_loop_seconds, self.cold_pass, stop, on_error=self.on_loop_error)
            ),  # fmt: skip
            asyncio.create_task(runtime.loop("copylist", settings.copylist_refresh_s, self.copylist_pass, stop)),
            asyncio.create_task(runtime.loop("feedpoll", settings.feed_poll_seconds, self.feedpoll_pass, stop)),
        ]
        if self.feed.enabled:
            tasks.append(asyncio.create_task(self.feed.run()))
        if self.private is not None:
            reconcile = runtime.loop("reconcile", settings.reconcile_seconds, self.reconcile_pass, stop,
                                     on_error=self.on_loop_error)  # fmt: skip
            tasks.append(asyncio.create_task(reconcile))
        await stop.wait()
        logger.info("trader stopping: flushing the recorder")
        self.recorder.flush()
        await self.feed.close()
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
    runtime.use_db(settings.trader_db_path)
    logger.info("solana-future-coin trader starting: MODE=%s account=%s", settings.mode, settings.account)
    if risk.kill_switch_on():
        logger.error("%s exists: the kill switch is on. Remove it to start.", settings.stop_file)
        # Idle instead of exiting: docker would restart an exited container in a loop.
        while risk.kill_switch_on():
            time.sleep(30)
    try:
        runtime.run_forever(Trader().run)
    except PreflightFailed as exc:
        logger.error("live pre-flight failed, refusing to trade: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
