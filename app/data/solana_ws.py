"""Solana websocket feed for the trader (D-30): one `programSubscribe` per copy-list owner on the Jupiter Perps program.

Checked 2026-11-18 against solana.com/docs/rpc/websocket (programSubscribe, programUnsubscribe), Chainstack's docs
and real mainnet notifications (1.3 s p50, 1.9 s max after the fill):
- subscribe: {"jsonrpc": "2.0", "id": n, "method": "programSubscribe", "params": [program, {"commitment":
  "confirmed", "encoding": "base64", "filters": [{"dataSize": 216}, {"memcmp": {"offset": 8, "bytes": owner}}]}]}
  -> {"jsonrpc": "2.0", "result": <subscription id, an integer>, "id": n}
- notification: {"method": "programNotification", "params": {"subscription": id, "result": {"context": {"slot": s},
  "value": {"pubkey": key, "account": {"data": [<base64>, "base64"], ...}}}}}
- unsubscribe: {"method": "programUnsubscribe", "params": [id]} -> {"result": true}
- Filters are AND only, so there is one subscription per owner. Subscribing sends no snapshot, and a notification can
  repeat unchanged data: `trading/watch.decide` handles both.
- `dataSlice` is only honoured from Agave 4.4.0 on, so a notification carries the whole 216-byte account;
  `jupiter_perps.decode_account` checks the size and the discriminator.
- Chainstack (Global Node): WSS on the same host and key as HTTPS; 1 request unit per subscribe and 1 per
  notification. Its limits on subscriptions per connection and on idle time are NOT verified yet (BUG B-27).

`SolanaFeed.run` keeps one connection open: on every (re)connect it subscribes every wanted owner again, because
subscription ids belong to one connection. `set_owners` diffs while it runs (subscribe new, unsubscribe dropped; an
owner dropped while its subscribe is in flight is unsubscribed when the answer arrives). It pings every
PING_EVERY_S and reconnects when no pong comes within PONG_TIMEOUT_S. Reconnects back off 1, 2, 4 ... 60 s plus up
to 1 s of jitter; the backoff resets only after a connection that stayed up STABLE_S.

The URL holds the Chainstack key: it is never logged, and errors are logged by exception type (and HTTP status) only.
"""

import asyncio
import base64
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.config import settings
from app.data import jupiter_perps as jp
from app.utils.logging import get_logger

logger = get_logger(__name__)

PING_EVERY_S = 20.0
PONG_TIMEOUT_S = 10.0
MAX_BACKOFF_S = 60.0
STABLE_S = 60.0  # a connection that stayed up this long resets the backoff
SUB = "subscribe"
UNSUB = "unsubscribe"


class Socket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def ping(self) -> Awaitable[float]: ...

    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[Socket]]
OnUpdate = Callable[[str, jp.JupPosition, int], None]  # (account, position, slot)


async def default_connect(url: str) -> Socket:
    from websockets.asyncio.client import connect

    # Our own ping (below) instead of the library's, so a dead socket is found the same way in tests and on the box.
    sock: Socket = await connect(url, ping_interval=None, open_timeout=20, close_timeout=5)
    return sock


def why(exc: BaseException) -> str:
    """What went wrong, without the message: a websockets / OS error message can repeat the URL (and its key)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__} (HTTP {status})" if isinstance(status, int) else type(exc).__name__


class SolanaFeed:
    def __init__(
        self,
        url: str,
        on_update: OnUpdate,
        *,
        program_id: str | None = None,
        max_subscriptions: int | None = None,
        connect: Connector = default_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
        ping_every_s: float = PING_EVERY_S,
        pong_timeout_s: float = PONG_TIMEOUT_S,
    ) -> None:
        self.url = url
        self.on_update = on_update
        self.program_id = program_id or settings.jupiter_program_id
        self.max_subscriptions = (
            settings.solana_ws_max_subscriptions if max_subscriptions is None else max_subscriptions
        )
        self._connect = connect
        self._sleep = sleep  # the reconnect backoff
        self._clock = clock
        self._rand = rand
        self.ping_every_s = ping_every_s
        self.pong_timeout_s = pong_timeout_s
        self._wanted: list[str] = []  # owners, best first, capped at max_subscriptions
        self._wanted_set: set[str] = set()
        self._subs: dict[str, int] = {}  # owner -> subscription id (on this connection)
        self._by_sub: dict[int, str] = {}  # subscription id -> owner
        self._pending: dict[str, int] = {}  # owner -> request id of its subscribe in flight
        self._requests: dict[int, tuple[str, str | int]] = {}  # request id -> (SUB, owner) | (UNSUB, subscription id)
        self._next_id = 1
        self._sock: Socket | None = None
        self._send_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self.stop = asyncio.Event()
        # For /health and statusCheck.
        self.connects = 0
        self.notifications = 0
        self.errors = 0  # socket errors (each one is a reconnect) and handler failures
        self.refused = 0  # error answers to our requests
        self.undecodable = 0  # notifications that are not a Position account of the subscribed owner
        self.unknown = 0  # notifications for a subscription we no longer hold
        self.pong_timeouts = 0
        self.last_message_at: float | None = None
        self.down_since: float | None = clock()  # not connected yet

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def connected(self) -> bool:
        return self._sock is not None

    @property
    def subscribed(self) -> set[str]:
        return set(self._subs)

    async def set_owners(self, owners: list[str]) -> None:
        """The owners to follow, best first. Above `max_subscriptions` the rest are left to the poll."""
        self._wanted = list(dict.fromkeys(owners))[: max(0, self.max_subscriptions)]
        self._wanted_set = set(self._wanted)
        if self._sock is None:
            return
        try:
            await self._sync()
        except Exception as exc:  # noqa: BLE001 — a dead socket: the reader reconnects and subscribes everything again
            logger.warning("ws: subscription update failed (%s); done again on reconnect", why(exc))

    async def close(self) -> None:
        self.stop.set()
        sock = self._sock
        if sock is not None:
            with contextlib.suppress(Exception):
                await sock.close()

    # ── requests ──────────────────────────────────────────────────────────────────────────
    async def _send(self, payload: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("ws: not connected")
        async with self._send_lock:
            await sock.send(json.dumps(payload))

    def _new_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    def subscribe_request(self, rid: int, owner: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "programSubscribe",
            "params": [
                self.program_id,
                {
                    "commitment": "confirmed",
                    "encoding": "base64",
                    "filters": [{"dataSize": jp.ACCOUNT_SIZE}, {"memcmp": {"offset": jp.SLICE_OFFSET, "bytes": owner}}],
                },
            ],
        }

    async def _subscribe(self, owner: str) -> None:
        rid = self._new_id()
        self._pending[owner] = rid
        self._requests[rid] = (SUB, owner)
        await self._send(self.subscribe_request(rid, owner))

    async def _unsubscribe(self, sub_id: int) -> None:
        rid = self._new_id()
        self._requests[rid] = (UNSUB, sub_id)
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "programUnsubscribe", "params": [sub_id]})

    async def _sync(self) -> None:
        async with self._sync_lock:
            for owner in sorted(set(self._subs) - self._wanted_set):
                sub_id = self._subs.pop(owner)
                self._by_sub.pop(sub_id, None)
                await self._unsubscribe(sub_id)
            for owner in list(self._wanted):
                if owner in self._wanted_set and owner not in self._subs and owner not in self._pending:
                    await self._subscribe(owner)

    # ── the connection ────────────────────────────────────────────────────────────────────
    def _reset(self, sock: Socket | None) -> None:
        """Subscription ids belong to one connection: forget them all when it ends (the next one asks again)."""
        self._sock = sock
        self._subs.clear()
        self._by_sub.clear()
        self._pending.clear()
        self._requests.clear()

    async def _pinger(self, sock: Socket) -> None:
        while True:
            await asyncio.sleep(self.ping_every_s)
            try:
                waiter = await sock.ping()
                await asyncio.wait_for(waiter, self.pong_timeout_s)
            except TimeoutError:
                self.pong_timeouts += 1
                logger.warning("ws: no pong within %.0fs; reconnecting", self.pong_timeout_s)
                with contextlib.suppress(Exception):
                    await sock.close()  # the reader's recv fails, and run() reconnects
                return
            except Exception:  # noqa: BLE001 — the socket is already dead; the reader notices
                return

    async def run(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            pinger: asyncio.Task[None] | None = None
            opened_at: float | None = None
            try:
                sock = await self._connect(self.url)
                opened_at = self._clock()
                self._sock = sock  # the subscription state is empty: cleared when the last connection ended
                self.down_since = None
                self.connects += 1
                await self._sync()
                logger.info("ws: connected, subscribing %d owners", len(self._pending))
                pinger = asyncio.create_task(self._pinger(sock))
                while not self.stop.is_set():
                    await self._on_raw(await sock.recv())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any socket error means reconnect
                if not self.stop.is_set():
                    self.errors += 1
                    logger.warning("ws: %s", why(exc))
            finally:
                if pinger is not None:
                    pinger.cancel()
                old = self._sock
                self._reset(None)
                if self.down_since is None:
                    self.down_since = self._clock()
                if old is not None:
                    with contextlib.suppress(Exception):
                        await old.close()
            if self.stop.is_set():
                break
            if opened_at is not None and self._clock() - opened_at >= STABLE_S:
                backoff = 1.0
            delay = backoff + self._rand()
            logger.info("ws: reconnecting in %.1fs", delay)
            await self._sleep(delay)
            backoff = min(MAX_BACKOFF_S, backoff * 2)

    # ── messages ──────────────────────────────────────────────────────────────────────────
    async def _on_raw(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            self.errors += 1
            return
        if not isinstance(msg, dict):
            return
        self.last_message_at = self._clock()
        if msg.get("method") == "programNotification":
            self._on_notification(msg.get("params"))
            return
        rid = msg.get("id")
        req = self._requests.pop(rid, None) if isinstance(rid, int) else None
        if "error" in msg:
            self.refused += 1
            err = msg.get("error")
            code = err.get("code") if isinstance(err, dict) else None
            text = str(err.get("message") if isinstance(err, dict) else err)[:120]
            logger.warning("ws: server refused %s: %s %s", req[0] if req else "a request", code, text)
            if req is not None and req[0] == SUB and self._pending.get(str(req[1])) == rid:
                del self._pending[str(req[1])]  # asked again on the next set_owners (every COPYLIST_REFRESH_S)
            return
        if req is not None and req[0] == SUB:
            await self._subscribed(str(req[1]), int(rid), msg.get("result"))  # type: ignore[arg-type]

    async def _subscribed(self, owner: str, rid: int, result: object) -> None:
        if self._pending.get(owner) == rid:
            del self._pending[owner]
        if not isinstance(result, int) or isinstance(result, bool):
            self.refused += 1
            return
        if owner in self._wanted_set and owner not in self._subs:
            self._subs[owner] = result
            self._by_sub[result] = owner
        else:
            await self._unsubscribe(result)  # dropped (or doubled) while its subscribe was in flight

    def _on_notification(self, params: object) -> None:
        self.notifications += 1
        try:
            sub_id = params["subscription"]  # type: ignore[index]
            result = params["result"]  # type: ignore[index]
            slot = int(result["context"]["slot"])
            value = result["value"]
            pubkey = str(value["pubkey"])
            data = base64.b64decode(value["account"]["data"][0])
        except (KeyError, TypeError, ValueError, IndexError):
            self.undecodable += 1
            return
        owner = self._by_sub.get(sub_id) if isinstance(sub_id, int) else None
        if owner is None:
            self.unknown += 1
            return
        pos = jp.decode_account(pubkey, data)
        if pos is None or pos.owner != owner:
            self.undecodable += 1
            return
        try:
            self.on_update(pubkey, pos, slot)
        except Exception as exc:  # noqa: BLE001 — one bad message must not drop the connection
            self.errors += 1
            logger.exception("ws: handler failed: %s", type(exc).__name__)

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "subscriptions": len(self._subs),
            "wanted": len(self._wanted),
            "pending": len(self._pending),
            "connects": self.connects,
            "notifications": self.notifications,
            "errors": self.errors,
            "refused": self.refused,
            "undecodable": self.undecodable,
            "unknown": self.unknown,
            "pong_timeouts": self.pong_timeouts,
            "down_for_s": None if self.connected or self.down_since is None else round(now - self.down_since, 1),
            "last_message_age_s": None if self.last_message_at is None else round(now - self.last_message_at, 1),
        }
