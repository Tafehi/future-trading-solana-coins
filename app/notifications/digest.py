"""The 15-minute trade report and the alarms, each sent at most once.

- `due_windows`: every complete window after the last one sent (`meta` key `digest:last_end`), at most
  MAX_BACKLOG of them, so a restart neither repeats a window nor floods the chat. The very first run starts at the
  current window (no backfill of history).
- `send_alarm`: the same title at most once per ALARM_REPEAT_S, so a stuck problem does not spam.
"""

import time

from app.config import settings
from app.db import meta, store
from app.notifications import messages
from app.notifications.telegram import Notifier
from app.utils.logging import get_logger

logger = get_logger(__name__)

LAST_END_KEY = "digest:last_end"
MAX_BACKLOG = 8
ALARM_REPEAT_S = 3600.0


async def due_windows(now: float | None = None) -> list[tuple[float, float]]:
    end = messages.window_end(now)
    step = settings.digest_minutes * 60
    async with store.session() as conn:
        got = await meta.get(conn, LAST_END_KEY)
        if got is None:
            await meta.put(conn, LAST_END_KEY, end)
            return []
    last = float(got[0])
    windows: list[tuple[float, float]] = []
    t = max(last, end - MAX_BACKLOG * step)
    while t + step <= end + 1e-6:
        windows.append((t, t + step))
        t += step
    return windows


async def run_digest(notifier: Notifier, *, now: float | None = None) -> int:
    """Send every due window that had a trade. Each window is marked done before sending. Returns messages sent."""
    sent = 0
    for start, end in await due_windows(now):
        async with store.session() as conn:
            await meta.put(conn, LAST_END_KEY, end)
        text = await messages.digest(start, end)
        if text is None:
            continue
        if await notifier.send_blocks(text.split("\n\n")):
            sent += 1
    return sent


async def send_alarm(notifier: Notifier, title: str, detail: str, *, now: float | None = None) -> bool:
    at = time.time() if now is None else now
    key = f"alarm:{title}"
    async with store.session() as conn:
        got = await meta.get(conn, key)
        if got is not None and at - float(got[0]) < ALARM_REPEAT_S:
            return False
        await meta.put(conn, key, at, now=at)
    logger.error("ALARM %s: %s", title, detail)
    return await notifier.send(messages.alarm(title, detail))
