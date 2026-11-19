"""What both programs share (D-30; modelled on hyperliquid-trading-bot's `app/runtime.py`): the loop runner, the
database each program writes, and the stop signal.

The scout and the trader are separate processes in separate containers. They share nothing in memory. The only links
are read-only reads of the other program's file (`app/db/crossread.py`).
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Awaitable, Callable

from app.config import settings
from app.db import meta, store
from app.utils.logging import get_logger

logger = get_logger(__name__)

Body = Callable[[], Awaitable[None]]
OnError = Callable[[str, Exception], Awaitable[None]]


def use_db(path: str) -> None:
    """Every `store.session()` without a path opens this file from now on (the program's OWN file)."""
    settings.db_path = path


async def pause(stop: asyncio.Event, seconds: float, wake: asyncio.Event | None = None) -> None:
    """Wait `seconds`, or less when `stop` (or `wake`) is set."""
    waits = [asyncio.ensure_future(stop.wait())]
    if wake is not None:
        waits.append(asyncio.ensure_future(wake.wait()))
    try:
        await asyncio.wait(waits, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


async def loop(
    name: str,
    every: float,
    body: Body,
    stop: asyncio.Event,
    *,
    wake: asyncio.Event | None = None,
    on_error: OnError | None = None,
) -> None:
    """Run `body` every `every` seconds until `stop`, and beat `meta beat:<name>` after each pass (for /health).

    `wake`: setting it starts the next pass at once (the trader's copier, when a signal is written). It is cleared
    BEFORE a pass, so a wake set during a pass starts another one right after it."""
    while not stop.is_set():
        if wake is not None:
            wake.clear()
        started = time.monotonic()
        try:
            await body()
        except Exception as exc:  # noqa: BLE001 — one bad pass must not end the loop
            logger.exception("loop %s failed: %s", name, type(exc).__name__)
            if on_error is not None:
                with contextlib.suppress(Exception):
                    await on_error(name, exc)
        try:
            async with store.session() as conn:
                await meta.beat(conn, name)
        except Exception as exc:  # noqa: BLE001 — a locked database must not end the loop either
            logger.warning("loop %s: beat not written (%s)", name, type(exc).__name__)
        await pause(stop, every - (time.monotonic() - started), wake)


def run_forever(main: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    """Run `main(stop)`; SIGTERM / SIGINT set `stop`, so the program flushes and closes cleanly."""

    async def _run() -> None:
        stop = asyncio.Event()
        ev = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            ev.add_signal_handler(sig, stop.set)
        await main(stop)

    asyncio.run(_run())
