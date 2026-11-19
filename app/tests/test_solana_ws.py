"""The trader's Solana websocket (D-30, `data/solana_ws.py`) against a fake socket: the subscribe shape, ids, decoding,
the diff, reconnects, the backoff (boundary: exactly STABLE_S of uptime resets it), the pong check, and the URL (with
its key) never in a log line.

Accounts are a real mainnet Position account from the fixtures with fields rewritten, so no address is typed here."""

import asyncio
import base64
import contextlib
import json
import logging
from typing import Any

import pytest

from app.data import jupiter_perps as jp
from app.data import solana_ws
from app.data.jupiter_perps import JupPosition
from app.data.solana_ws import SolanaFeed
from app.tests.helpers import position_account

PROGRAM = "PROGRAM-ID"
URL = "wss://rpc.invalid/KEY-THAT-MUST-NOT-LEAK"
OWNER_A = jp.b58encode(bytes([11]) * 32)
OWNER_B = jp.b58encode(bytes([12]) * 32)
OWNER_C = jp.b58encode(bytes([13]) * 32)


def account(owner: str, *, opened: int = 1_800_000_000, updated: int = 1_800_000_000) -> bytes:
    return position_account({OWNER_A: 11, OWNER_B: 12, OWNER_C: 13}[owner], opened=opened, updated=updated)


def notification(sub: int, data: bytes, *, slot: int = 321, key: str = "ACC-1") -> dict[str, Any]:
    value = {"pubkey": key, "account": {"data": [base64.b64encode(data).decode(), "base64"], "owner": PROGRAM}}
    return {"jsonrpc": "2.0", "method": "programNotification",
            "params": {"subscription": sub, "result": {"context": {"slot": slot}, "value": value}}}  # fmt: skip


class FakeSocket:
    def __init__(self, *, pong: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.closed = False
        self.pong = pong
        self.pings = 0

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def ping(self) -> asyncio.Future[float]:
        self.pings += 1
        fut: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        if self.pong:
            fut.set_result(0.001)
        return fut

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(ConnectionError("closed"))

    def push(self, msg: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(msg))

    def methods(self) -> list[tuple[str, Any]]:
        return [(m["method"], m["params"][0] if m["method"] == "programUnsubscribe" else m["params"][1]["filters"][1]
                 ["memcmp"]["bytes"]) for m in self.sent]  # fmt: skip


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


class Harness:
    def __init__(self, *items: object, max_subs: int = 100, ping_every: float = 3600.0, pong_timeout: float = 10.0):
        self.items = list(items)
        self.sockets: list[FakeSocket] = []
        self.got: list[tuple[str, JupPosition, int]] = []
        self.sleeps: list[float] = []
        self.t = [1000.0]
        self.feed = SolanaFeed(URL, lambda k, p, s: self.got.append((k, p, s)), program_id=PROGRAM,
                               max_subscriptions=max_subs, connect=self.connect, sleep=self.sleep,
                               clock=lambda: self.t[0], rand=lambda: 0.25, ping_every_s=ping_every,
                               pong_timeout_s=pong_timeout)  # fmt: skip

    async def connect(self, url: str) -> FakeSocket:
        item = self.items.pop(0) if self.items else FakeSocket()
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, FakeSocket)
        self.sockets.append(item)
        return item

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    @property
    def sock(self) -> FakeSocket:
        return self.sockets[-1]

    async def __aenter__(self) -> "Harness":
        self.task = asyncio.create_task(self.feed.run())
        await settle()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.feed.close()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


def answer(rid: int, result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "result": result, "id": rid}


async def test_the_subscribe_request_has_the_verified_shape() -> None:
    h = Harness()
    await h.feed.set_owners([OWNER_A])
    async with h:
        assert h.sock.sent == [{
            "jsonrpc": "2.0", "id": 1, "method": "programSubscribe",
            "params": [PROGRAM, {"commitment": "confirmed", "encoding": "base64",
                                 "filters": [{"dataSize": 216}, {"memcmp": {"offset": 8, "bytes": OWNER_A}}]}],
        }]  # fmt: skip


async def test_a_notification_reaches_the_handler_decoded() -> None:
    h = Harness()
    await h.feed.set_owners([OWNER_A])
    async with h:
        h.sock.push(answer(1, 77))
        h.sock.push(notification(77, account(OWNER_A, opened=1_800_000_100, updated=1_800_000_100), slot=555))
        await settle()
        assert h.feed.subscribed == {OWNER_A}
        assert len(h.got) == 1
        key, pos, slot = h.got[0]
        assert (key, slot, pos.owner, pos.coin, pos.size_usd, pos.open_time) == (
            "ACC-1",
            555,
            OWNER_A,
            "SOL",
            5000.0,
            1_800_000_100,
        )
        assert h.feed.stats()["notifications"] == 1


async def test_owners_above_the_cap_are_left_to_the_poll() -> None:
    h = Harness(max_subs=2)
    await h.feed.set_owners([OWNER_A, OWNER_B, OWNER_C])
    async with h:
        assert [b for _, b in h.sock.methods()] == [OWNER_A, OWNER_B]
        assert h.feed.stats()["wanted"] == 2


async def test_a_new_list_subscribes_the_new_and_unsubscribes_the_dropped() -> None:
    h = Harness()
    await h.feed.set_owners([OWNER_A, OWNER_B])
    async with h:
        h.sock.push(answer(1, 10))
        h.sock.push(answer(2, 11))
        await settle()
        await h.feed.set_owners([OWNER_B, OWNER_C])
        await settle()
        assert h.sock.methods()[2:] == [("programUnsubscribe", 10), ("programSubscribe", OWNER_C)]
        h.sock.push(notification(10, account(OWNER_A)))  # a late notification of the dropped owner
        await settle()
        assert h.got == [] and h.feed.stats()["unknown"] == 1


async def test_an_owner_dropped_while_its_subscribe_is_pending_is_unsubscribed_on_the_answer() -> None:
    h = Harness()
    await h.feed.set_owners([OWNER_A])
    async with h:
        await h.feed.set_owners([])
        h.sock.push(answer(1, 55))
        await settle()
        assert h.feed.subscribed == set()
        assert h.sock.methods()[-1] == ("programUnsubscribe", 55)


async def test_a_refused_subscribe_is_counted_and_asked_again_on_the_next_list() -> None:
    h = Harness()
    await h.feed.set_owners([OWNER_A])
    async with h:
        h.sock.push({"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params"}, "id": 1})
        await settle()
        assert h.feed.stats()["refused"] == 1 and h.feed.stats()["pending"] == 0
        await h.feed.set_owners([OWNER_A])
        await settle()
        assert h.sock.methods() == [("programSubscribe", OWNER_A), ("programSubscribe", OWNER_A)]


async def test_a_reconnect_subscribes_every_owner_again() -> None:
    first = FakeSocket()
    h = Harness(first)
    await h.feed.set_owners([OWNER_A, OWNER_B])
    async with h:
        first.push(answer(1, 10))
        first.push(answer(2, 11))
        await settle()
        assert h.feed.subscribed == {OWNER_A, OWNER_B}
        first.inbox.put_nowait(ConnectionError("dropped"))
        await settle()
        second = h.sock
        assert second is not first and h.sleeps == [1.25]
        assert [b for _, b in second.methods()] == [OWNER_A, OWNER_B]  # subscription ids belong to one connection
        assert h.feed.subscribed == set() and h.feed.stats()["connects"] == 2


async def test_the_backoff_doubles_to_sixty_with_jitter() -> None:
    h = Harness(*[OSError("refused") for _ in range(8)])
    async with h:
        assert h.sleeps == [1.25, 2.25, 4.25, 8.25, 16.25, 32.25, 60.25, 60.25]


@pytest.mark.parametrize(("uptime", "next_sleep"), [(60.0, 1.25), (59.9, 4.25)])
async def test_the_backoff_resets_only_after_a_stable_minute(uptime: float, next_sleep: float) -> None:
    stable = FakeSocket()
    h = Harness(OSError("refused"), OSError("refused"), stable)
    async with h:
        assert h.sleeps == [1.25, 2.25] and h.sock is stable
        h.t[0] += uptime
        stable.inbox.put_nowait(ConnectionError("dropped"))
        await settle()
        assert h.sleeps == [1.25, 2.25, next_sleep]


async def test_a_missing_pong_reconnects() -> None:
    deaf = FakeSocket(pong=False)
    h = Harness(deaf, ping_every=0.01, pong_timeout=0.01)
    async with h:
        for _ in range(100):
            if h.feed.stats()["connects"] == 2:
                break
            await asyncio.sleep(0.01)
        assert deaf.pings >= 1 and deaf.closed and h.feed.stats()["pong_timeouts"] == 1
        assert h.feed.stats()["connects"] == 2


async def test_the_url_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness(OSError(f"cannot connect to {URL}"), ConnectionError(URL))
    await h.feed.set_owners([OWNER_A])
    async with h:
        h.sock.push({"jsonrpc": "2.0", "error": {"code": -1, "message": "bad"}, "id": 1})
        await settle()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ws: OSError" in text and "KEY-THAT-MUST-NOT-LEAK" not in text and "rpc.invalid" not in text


def test_why_keeps_the_http_status_but_not_the_message() -> None:
    class Response:
        status_code = 429

    class Refused(Exception):
        def __init__(self) -> None:
            super().__init__(f"server rejected {URL}")
            self.response = Response()

    assert solana_ws.why(Refused()) == "Refused (HTTP 429)"
    assert solana_ws.why(OSError(URL)) == "OSError"


async def test_a_wrong_size_discriminator_or_owner_is_counted_not_passed_on() -> None:
    h = Harness()
    await h.feed.set_owners([OWNER_A])
    async with h:
        h.sock.push(answer(1, 7))
        h.sock.push(notification(7, account(OWNER_A)[:215]))
        h.sock.push(notification(7, b"\0" * 8 + account(OWNER_A)[8:]))
        h.sock.push(notification(7, account(OWNER_B)))  # not the subscribed owner's account
        h.sock.push({"jsonrpc": "2.0", "method": "programNotification", "params": {"subscription": 7}})
        await settle()
        assert h.got == [] and h.feed.stats()["undecodable"] == 4


async def test_down_for_counts_from_start_and_clears_on_connect() -> None:
    h = Harness(OSError("refused"))
    assert h.feed.stats()["down_for_s"] == 0.0
    h.t[0] += 42.0
    assert h.feed.stats()["down_for_s"] == 42.0
    async with h:
        assert h.feed.connected and h.feed.stats()["down_for_s"] is None
