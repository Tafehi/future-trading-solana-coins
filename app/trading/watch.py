"""The trader's feed (D-30): what the copy-list owners' Jupiter Position accounts do, as OPEN / CLOSE signals, once.

Observations come from two places. Both go on ONE queue, and one worker (`trader.py`) applies them in order:
- the websocket (`data/solana_ws.py`, one programSubscribe per owner): about 1-2 s after the trader's fill;
- the safety-net poll (every FEED_POLL_SECONDS): getMultipleAccounts on the listed owners' known accounts.

`decide(prev, obs, now)` is pure:
- An account's state only moves forward, ordered by (updateTime, slot). An older observation is ignored. One that
  matches what we hold (the socket repeats unchanged data, the poll re-reads it) is a duplicate.
- OPEN when the account was flat, unknown or dust and is now open with sizeUsd >= JUPITER_MIN_SIZE_USD, and the open
  is FRESH: now - openTime <= SIGNAL_MAX_AGE_S. This freshness rule replaces "a baseline on subscribe":
  programSubscribe sends no snapshot, and an open made while the trader restarted is still copied if it is young.
- An open account whose openTime changed was closed and opened again between two looks: CLOSE for the old one, then
  the OPEN rule for the new one.
- CLOSE (recorded only; our own exits decide) when the last state we saw was a real open and the size is now 0.
  A position that was reduced to dust before it closed gets no CLOSE row (record only, nothing is lost).
- Adds and reduces: the state follows, no signal.

`PositionTracker.apply` writes the state (`feed_positions`) and the signals in ONE transaction, and updates its memory
only after the commit. Signals are written only for owners on the CURRENT copy list. An owner that left the list keeps
its state but gets no signal. A unique index on (position_key, open_time, kind) makes a second signal for one open
impossible, whatever saw it (socket, poll, restart).
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass

from app.config import settings
from app.data.jupiter_perps import SOURCE, JupPosition
from app.db import store
from app.trading.copylist import CopyList
from app.utils.logging import get_logger

logger = get_logger(__name__)

OPEN = "OPEN"
CLOSE = "CLOSE"
WS = "ws"
POLL = "poll"


@dataclass(frozen=True)
class Obs:
    """One look at one Position account."""

    pubkey: str
    pos: JupPosition
    slot: int
    via: str  # WS or POLL
    seen_at: float  # wall clock, when we got it


@dataclass(frozen=True)
class State:
    """One Position account as the trader last saw it (a `feed_positions` row)."""

    pubkey: str
    owner: str
    coin: str
    side: str
    size_usd: float
    entry_px: float
    open_time: int
    update_time: int
    slot: int
    via: str
    seen_at: float

    @property
    def order(self) -> tuple[int, int]:
        return (self.update_time, self.slot)

    def same_as(self, p: JupPosition) -> bool:
        return (self.coin, self.side, self.size_usd, self.entry_px, self.open_time, self.update_time) == (
            p.coin, p.side, p.size_usd, p.entry_px, p.open_time, p.update_time,
        )  # fmt: skip


@dataclass(frozen=True)
class Event:
    kind: str  # OPEN or CLOSE
    pubkey: str
    owner: str
    coin: str
    side: str
    size_usd: float
    entry_px: float
    open_time: int  # which open (a CLOSE carries the open it ends)
    via: str
    seen_at: float


def _state(obs: Obs) -> State:
    p = obs.pos
    return State(obs.pubkey, p.owner, p.coin, p.side, p.size_usd, p.entry_px, p.open_time, p.update_time, obs.slot,
                 obs.via, obs.seen_at)  # fmt: skip


def decide(prev: State | None, obs: Obs, *, now: float) -> tuple[State | None, list[Event]]:
    """(the new state, the events) for one observation; (None, []) = ignore it (older, or a duplicate). Pure."""
    p = obs.pos
    if prev is not None:
        if (p.update_time, obs.slot) < prev.order:
            return None, []  # older than what we hold: the state never moves back
        if prev.same_as(p):
            return None, []  # the same state again (socket repeat, or the poll re-reading it)
    events: list[Event] = []
    minimum = settings.jupiter_min_size_usd
    was_open = prev is not None and prev.size_usd >= minimum  # dust was never a real open
    if prev is not None and was_open and (not p.is_open or p.open_time != prev.open_time):
        events.append(Event(CLOSE, obs.pubkey, prev.owner, prev.coin, prev.side, prev.size_usd, prev.entry_px,
                            prev.open_time, obs.via, obs.seen_at))  # fmt: skip
        was_open = False
    fresh = now - p.open_time <= settings.signal_max_age_s
    if p.is_open and p.size_usd >= minimum and not was_open and fresh:
        events.append(Event(OPEN, obs.pubkey, p.owner, p.coin, p.side, p.size_usd, p.entry_px, p.open_time, obs.via,
                            obs.seen_at))  # fmt: skip
    return _state(obs), events


def short(addr: str) -> str:
    return f"{addr[:6]}…" if len(addr) > 8 else addr


class PositionTracker:
    """The feed's memory: every account the trader has seen, loaded from `feed_positions` at boot."""

    def __init__(self) -> None:
        self.states: dict[str, State] = {}
        self.applied = 0  # observations that changed a state
        self.ignored = 0  # older or duplicate observations
        self.signals = 0  # signal rows written
        self.last_lag_s: float | None = None  # seconds from the trader's open to our OPEN signal

    async def load(self) -> int:
        async with store.session() as conn:
            async with conn.execute("SELECT * FROM feed_positions") as cur:
                rows = await cur.fetchall()
        self.states = {
            str(r["pubkey"]): State(str(r["pubkey"]), str(r["owner"]), str(r["coin"]), str(r["side"]),
                                    float(r["size_usd"]), float(r["entry_px"]), int(r["open_time"]),
                                    int(r["update_time"]), int(r["slot"]), str(r["via"]), float(r["seen_at"]))
            for r in rows
        }  # fmt: skip
        return len(self.states)

    def keys_of(self, owners: Iterable[str]) -> set[str]:
        """Accounts seen for these owners (the poll adds them to the scout's known accounts)."""
        wanted = set(owners)
        return {k for k, s in self.states.items() if s.owner in wanted}

    async def apply(self, obs: Obs, copylist: CopyList, *, now: float | None = None) -> list[int]:
        """Apply one observation. Returns the ids of the signal rows written (empty: nothing new)."""
        at = time.time() if now is None else now
        new, events = decide(self.states.get(obs.pubkey), obs, now=at)
        if new is None:
            self.ignored += 1
            return []
        ids: list[int] = []
        async with store.session() as conn:
            await conn.execute(
                "INSERT INTO feed_positions (pubkey, owner, coin, side, size_usd, entry_px, open_time, update_time, "
                "slot, seen_at, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(pubkey) DO UPDATE SET "
                "owner = excluded.owner, coin = excluded.coin, side = excluded.side, size_usd = excluded.size_usd, "
                "entry_px = excluded.entry_px, open_time = excluded.open_time, update_time = excluded.update_time, "
                "slot = excluded.slot, seen_at = excluded.seen_at, via = excluded.via",
                (new.pubkey, new.owner, new.coin, new.side, new.size_usd, new.entry_px, new.open_time,
                 new.update_time, new.slot, new.seen_at, new.via),
            )  # fmt: skip
            for ev in events:
                entry = copylist.get(ev.owner)
                if entry is None:
                    continue  # not on the current list: the state is kept, no signal
                cur = await conn.execute(
                    "INSERT INTO signals (ts, wallet, source, coin, kind, side, tier, wallet_size, wallet_entry_px, "
                    "wallet_leverage, position_key, open_time, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (ev.seen_at, ev.owner, SOURCE, ev.coin, ev.kind, ev.side, entry.tier, ev.size_usd, ev.entry_px,
                     ev.pubkey, ev.open_time, ev.via),
                )  # fmt: skip
                if cur.rowcount and cur.lastrowid:
                    ids.append(int(cur.lastrowid))
                    if ev.kind == OPEN:
                        self.last_lag_s = ev.seen_at - ev.open_time
                        logger.info("signal #%d OPEN %s %s %s $%.0f via %s, %.1fs after the open",
                                    cur.lastrowid, short(ev.owner), ev.coin, ev.side, ev.size_usd, ev.via,
                                    self.last_lag_s)  # fmt: skip
                    else:
                        logger.info("signal #%d CLOSE %s %s %s via %s", cur.lastrowid, short(ev.owner), ev.coin,
                                    ev.side, ev.via)  # fmt: skip
        self.states[obs.pubkey] = new
        self.applied += 1
        self.signals += len(ids)
        return ids

    def stats(self) -> dict[str, object]:
        return {
            "accounts": len(self.states),
            "applied": self.applied,
            "ignored": self.ignored,
            "signals": self.signals,
            "last_open_lag_s": None if self.last_lag_s is None else round(self.last_lag_s, 1),
        }
