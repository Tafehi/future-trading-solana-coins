"""Telegram client (ported from ai-kucoin-future). Message text lives in `messages.py`.

- A failed send logs the response body; Telegram's `description` explains a 400.
- `_Bucket` paces sends; a `429` sleeps `retry_after` and retries once.
- Every interpolated value goes through `esc()` (parse_mode HTML).
- Every message is stamped [PAPER] or [LIVE] by `mode()`.
- `send` never raises: the notifier must not break the trading loop.
"""

import asyncio
import html
import time
from typing import Protocol

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

API_BASE = "https://api.telegram.org"

# Well under Telegram's per-chat limit. This bot sends a handful of messages a day, so hitting this
# means a bug (e.g. a reconnect loop), and pacing shows it as a delay instead of dropped alerts.
MAX_PER_MINUTE = 20

# Telegram truncates nothing — it rejects. 4096 is the hard limit on `text`.
MAX_TEXT = 4096

SEND_TIMEOUT_S = 10.0


class _Bucket:
    """A token bucket over a 60-second window. Blocks rather than dropping.

    The messages are a trade log, and a late log is better than one with holes.
    """

    def __init__(self, per_minute: int = MAX_PER_MINUTE) -> None:
        self.per_minute = max(1, per_minute)
        self._sent: list[float] = []

    async def take(self) -> None:
        while True:
            now = time.monotonic()
            self._sent = [t for t in self._sent if now - t < 60.0]
            if len(self._sent) < self.per_minute:
                self._sent.append(now)
                return
            wait = 60.0 - (now - self._sent[0]) + 0.01
            logger.warning(
                "telegram: rate limit self-imposed, waiting %.1fs (%d in the last minute)", wait, len(self._sent)
            )
            await asyncio.sleep(wait)


def esc(value: object) -> str:
    """HTML-escape anything before it is interpolated into a message.

    `quote=False`: values only go between tags, never in attributes, and `&quot;` would show visibly.
    """
    return html.escape(str(value), quote=False)


# A report longer than one message is split at block and line boundaries (`send_blocks`), never inside a line:
# a cut at 4,096 can split an HTML tag, and Telegram rejects the whole message. The margin leaves room for the
# "(continued)" header.
SPLIT_AT = 3800


def pack(blocks: list[str], *, limit: int = SPLIT_AT) -> list[str]:
    """Blocks into messages of at most `limit` characters, splitting only between blocks or lines."""
    pieces: list[tuple[str, str]] = []
    for block in blocks:
        lines = [block] if len(block) <= limit else [line[:limit] for line in block.split("\n")]
        pieces += [("\n\n" if i == 0 else "\n", line) for i, line in enumerate(lines)]
    messages: list[str] = []
    current = ""
    for sep, text in pieces:
        if current and len(current) + len(sep) + len(text) > limit:
            messages.append(current)
            current = ""
        current = f"{current}{sep}{text}" if current else text
    if current:
        messages.append(current)
    return messages


def mode() -> str:
    """`PAPER` or `LIVE`, read at send time."""
    return "LIVE" if settings.is_live else "PAPER"


def money(amount: float) -> str:
    """`+$12.40` / `-$3.05`. Always signed."""
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def pct(fraction: float) -> str:
    sign = "+" if fraction >= 0 else "-"
    return f"{sign}{abs(fraction) * 100:.1f}%"


def duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


class Notifier(Protocol):
    """What the digest and alarms need; `TelegramNotifier` and the tests' fake both fit."""

    async def send(self, message: str, *, parse_mode: str = "HTML") -> bool: ...

    async def send_blocks(self, blocks: list[str]) -> bool: ...


class TelegramNotifier:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        self._bucket = _Bucket()
        # The client is built even when disabled if a transport was injected, so tests can exercise the
        # send path without a real token in the environment.
        self._client: httpx.AsyncClient | None = None
        if self.enabled or transport is not None:
            self._client = httpx.AsyncClient(timeout=SEND_TIMEOUT_S, transport=transport)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, message: str, *, parse_mode: str = "HTML") -> bool:
        """Post one message. `False` on any failure, and the reason is always logged.

        Never raises: the notifier must not break the trading loop. `/health` and `HEARTBEAT_URL` cover a
        notifier that has gone quiet.
        """
        if not self.enabled or self._client is None:
            logger.debug("telegram: disabled, dropping message")
            return False

        await self._bucket.take()
        payload = {
            "chat_id": self.chat_id,
            "text": message[:MAX_TEXT],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        url = f"{API_BASE}/bot{self.token}/sendMessage"

        for attempt in (1, 2):
            try:
                resp = await self._client.post(url, json=payload)
            except Exception as exc:  # noqa: BLE001 — a notifier must not raise into the trading loop
                logger.error("telegram: send failed: %s: %s", type(exc).__name__, exc)
                return False

            if 200 <= resp.status_code < 300:
                return True

            # The body's `description` is the only thing that explains a 400, so log it.
            body = resp.text[:500]
            if resp.status_code == 429 and attempt == 1:
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("parameters", {}).get("retry_after", 1.0))
                except Exception:  # noqa: BLE001 — a malformed 429 body still means "wait"
                    pass
                retry_after = min(60.0, max(1.0, retry_after))
                logger.warning("telegram: 429, retrying once after %.0fs: %s", retry_after, body)
                await asyncio.sleep(retry_after)
                continue

            logger.error("telegram: HTTP %d: %s", resp.status_code, body)
            return False

        return False

    async def send_blocks(self, blocks: list[str]) -> bool:
        """Send blocks packed into as few messages as fit (`pack`). True only if every part was taken."""
        parts = pack(blocks)
        ok = True
        for i, part in enumerate(parts):
            header = f"<i>(continued {i + 1}/{len(parts)})</i>\n" if i else ""
            ok = await self.send(header + part) and ok
        return ok
