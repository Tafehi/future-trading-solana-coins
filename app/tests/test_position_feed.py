"""The trader's feed (D-30, `trading/watch.py`): observations -> OPEN / CLOSE signals, exactly once.

Boundaries are tested AT the value: freshness exactly SIGNAL_MAX_AGE_S, size exactly JUPITER_MIN_SIZE_USD."""

import pytest

from app.config import settings
from app.data.jupiter_perps import JupPosition
from app.db import store
from app.db.crossread import CopyListRead, Entry
from app.trading.copylist import CopyList
from app.trading.watch import CLOSE, OPEN, POLL, WS, Event, Obs, PositionTracker, decide

NOW = 1_800_000_000.0
OWNER = "OWNER-A"


def p(*, size: float = 5000.0, opened: float = NOW - 10, updated: float | None = None, owner: str = OWNER,
      key: str = "ACC-1", entry: float = 150.0) -> JupPosition:  # fmt: skip
    return JupPosition(key, owner, "SOL", "long", size, entry, int(opened), int(opened if updated is None else updated))


def obs(pos: JupPosition, *, slot: int = 100, via: str = WS, seen: float = NOW) -> Obs:
    return Obs(pos.pubkey, pos, slot, via, seen)


def kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def listed(*owners: str, tracked: bool = True) -> CopyList:
    cl = CopyList()
    entries = [Entry(o, "GOLD", tracked, 10, 1_000_000.0, 8.0, 8.0) for o in owners]
    cl.replace(CopyListRead(entries, NOW, NOW), now=NOW)
    return cl


# ── decide (pure) ─────────────────────────────────────────────────────────────────────────


def test_a_fresh_open_on_an_unknown_account_is_an_open() -> None:
    new, events = decide(None, obs(p()), now=NOW)
    assert new is not None and kinds(events) == [OPEN] and events[0].open_time == int(NOW - 10)


@pytest.mark.parametrize(("opened", "now", "is_open"), [
    (NOW - 60, NOW, True),  # exactly SIGNAL_MAX_AGE_S old: still fresh
    (NOW - 61, NOW, False),
    (NOW - 60, NOW + 0.001, False),
])  # fmt: skip
def test_freshness_at_its_boundary(opened: float, now: float, is_open: bool) -> None:
    assert settings.signal_max_age_s == 60.0
    _, events = decide(None, obs(p(opened=opened)), now=now)
    assert kinds(events) == ([OPEN] if is_open else [])


@pytest.mark.parametrize(("size", "is_open"), [(1000.0, True), (999.99, False)])
def test_size_at_the_minimum(size: float, is_open: bool) -> None:
    assert settings.jupiter_min_size_usd == 1000.0
    new, events = decide(None, obs(p(size=size)), now=NOW)
    assert new is not None and kinds(events) == ([OPEN] if is_open else [])


def test_an_old_open_seen_for_the_first_time_is_state_only() -> None:
    """No snapshot on subscribe: an account first seen open long after its open is old news."""
    new, events = decide(None, obs(p(opened=NOW - 3600)), now=NOW)
    assert new is not None and new.size_usd == 5000.0 and events == []


def test_the_same_state_again_is_a_duplicate_on_any_slot() -> None:
    first, _ = decide(None, obs(p()), now=NOW)
    assert decide(first, obs(p(), slot=100), now=NOW + 1) == (None, [])  # the socket repeats unchanged data
    assert decide(first, obs(p(), slot=250, via=POLL), now=NOW + 1) == (None, [])  # the poll re-reads it


def test_the_state_never_moves_back() -> None:
    held, _ = decide(None, obs(p(updated=NOW - 5), slot=200), now=NOW)
    older = obs(p(size=9000.0, updated=NOW - 6), slot=300)  # older updateTime wins over a higher slot
    assert decide(held, older, now=NOW) == (None, [])
    same_second_earlier_slot = obs(p(size=9000.0, updated=NOW - 5), slot=199)
    assert decide(held, same_second_earlier_slot, now=NOW) == (None, [])
    same_second_later_slot = obs(p(size=9000.0, updated=NOW - 5), slot=201)
    new, events = decide(held, same_second_later_slot, now=NOW)
    assert new is not None and new.size_usd == 9000.0 and events == []


def test_add_and_reduce_follow_the_state_without_a_signal() -> None:
    held, _ = decide(None, obs(p()), now=NOW)
    bigger, events = decide(held, obs(p(size=9000.0, updated=NOW - 5), slot=101), now=NOW)
    assert bigger is not None and events == []
    smaller, events = decide(bigger, obs(p(size=2000.0, updated=NOW - 4), slot=102), now=NOW)
    assert smaller is not None and smaller.size_usd == 2000.0 and events == []


def test_an_add_on_an_open_of_exactly_the_minimum_is_not_a_second_open() -> None:
    held, _ = decide(None, obs(p(size=1000.0)), now=NOW)
    _, events = decide(held, obs(p(size=5000.0, updated=NOW - 5), slot=101), now=NOW)
    assert events == []


def test_open_to_zero_is_a_close_of_that_open() -> None:
    held, _ = decide(None, obs(p()), now=NOW)
    new, events = decide(held, obs(p(size=0.0, updated=NOW + 30), slot=500), now=NOW + 30)
    assert new is not None and kinds(events) == [CLOSE] and events[0].open_time == int(NOW - 10)


def test_dust_grown_while_fresh_is_an_open_and_dust_closed_is_no_close() -> None:
    dust, events = decide(None, obs(p(size=500.0)), now=NOW)
    assert events == []
    grown, events = decide(dust, obs(p(size=5000.0, updated=NOW - 5), slot=101), now=NOW)
    assert kinds(events) == [OPEN]
    _, events = decide(dust, obs(p(size=0.0, updated=NOW - 5), slot=101), now=NOW)
    assert events == []


def test_closed_and_opened_again_between_two_looks() -> None:
    held, _ = decide(None, obs(p(opened=NOW - 3600)), now=NOW)
    reopened = obs(p(opened=NOW - 20), slot=900)
    _, events = decide(held, reopened, now=NOW)
    assert kinds(events) == [CLOSE, OPEN] and events[0].open_time == int(NOW - 3600)
    _, late = decide(held, obs(p(opened=NOW - 200), slot=900), now=NOW)
    assert kinds(late) == [CLOSE]  # the new open is too old to copy


def test_a_closed_account_we_never_saw_open_is_state_only() -> None:
    new, events = decide(None, obs(p(size=0.0)), now=NOW)
    assert new is not None and events == []


# ── PositionTracker (database) ────────────────────────────────────────────────────────────


async def _signals() -> list[dict[str, object]]:
    async with store.session() as conn:
        async with conn.execute("SELECT * FROM signals ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def test_websocket_then_poll_is_one_signal_with_its_source(fresh_db: str) -> None:
    t = PositionTracker()
    cl = listed(OWNER)
    ids = await t.apply(obs(p(), seen=NOW + 1.5), cl, now=NOW + 1.5)
    assert len(ids) == 1
    assert await t.apply(obs(p(), slot=180, via=POLL, seen=NOW + 20), cl, now=NOW + 20) == []
    rows = await _signals()
    assert len(rows) == 1
    s = rows[0]
    assert (s["kind"], s["wallet"], s["source"], s["coin"], s["side"], s["tier"], s["via"]) == (
        OPEN,
        OWNER,
        "jupiter",
        "SOL",
        "long",
        "GOLD",
        WS,
    )
    assert (s["position_key"], s["open_time"], s["wallet_size"], s["wallet_entry_px"]) == (
        "ACC-1",
        int(NOW - 10),
        5000.0,
        150.0,
    )
    assert s["ts"] == NOW + 1.5 and s["handled"] == 0  # ts = when we saw it; the copier picks it up
    assert (t.applied, t.ignored, t.signals) == (1, 1, 1) and t.last_lag_s == pytest.approx(11.5)


async def test_an_owner_off_the_list_keeps_its_state_but_gets_no_signal(fresh_db: str) -> None:
    t = PositionTracker()
    assert await t.apply(obs(p()), listed("SOMEONE-ELSE"), now=NOW) == []
    assert await _signals() == []
    assert t.states["ACC-1"].size_usd == 5000.0 and t.keys_of([OWNER]) == {"ACC-1"}


async def test_a_watched_owner_gets_its_signal_for_the_shadow_copy(fresh_db: str) -> None:
    t = PositionTracker()
    assert len(await t.apply(obs(p()), listed(OWNER, tracked=False), now=NOW)) == 1


async def test_a_restart_never_signals_twice(fresh_db: str) -> None:
    cl = listed(OWNER)
    await PositionTracker().apply(obs(p()), cl, now=NOW)
    again = PositionTracker()
    assert await again.load() == 1  # the state survives in feed_positions
    assert await again.apply(obs(p(), slot=300, via=POLL), cl, now=NOW + 5) == []
    async with store.session() as conn:
        await conn.execute("DELETE FROM feed_positions")  # even with the state lost ...
    wiped = PositionTracker()
    assert await wiped.load() == 0
    assert await wiped.apply(obs(p(), slot=300, via=POLL), cl, now=NOW + 5) == []  # ... the unique index holds
    assert len(await _signals()) == 1


async def test_open_then_close_are_two_rows(fresh_db: str) -> None:
    t = PositionTracker()
    cl = listed(OWNER)
    await t.apply(obs(p()), cl, now=NOW)
    assert len(await t.apply(obs(p(size=0.0, updated=NOW + 60), slot=900), cl, now=NOW + 60)) == 1
    assert [(r["kind"], r["open_time"]) for r in await _signals()] == [(OPEN, int(NOW - 10)), (CLOSE, int(NOW - 10))]
