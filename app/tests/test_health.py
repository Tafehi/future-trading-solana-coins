"""/health, one per program (D-30). Each must watch exactly the loops its program runs (DEBUG D-11: it once watched two
loops that no longer existed, so the box was "unhealthy" forever), and each limit is tested AT its boundary value."""

import re
from pathlib import Path

import pytest

from app.api import health
from app.config import settings
from app.db import crossread, meta, store

APP = Path(__file__).resolve().parents[1]
NOW = 2_000_000_000.0


def _loops(program: str) -> set[str]:
    return set(re.findall(r'runtime\.loop\(\s*"([a-z_]+)"', (APP / program).read_text()))


def test_scout_health_watches_exactly_the_loops_the_scout_runs() -> None:
    assert set(health.SCOUT_LIMITS_S) == _loops("scout.py")


def test_trader_health_watches_exactly_the_loops_the_trader_runs() -> None:
    assert set(health.TRADER_LIMITS_S) == _loops("trader.py") - {"reconcile"}  # reconcile: live only


def test_reconcile_is_watched_in_live_mode_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "reconcile" not in health.trader_limits()
    monkeypatch.setattr(settings, "mode", "live")
    assert health.trader_limits()["reconcile"] == health.RECONCILE_LIMIT_S


async def _beat_all(names: dict[str, int], *, skip: str = "", at: float = NOW) -> None:
    async with store.session() as conn:
        for name in names:
            if name != skip:
                await meta.beat(conn, name, now=at)


async def _scout_state(*, poll_age: float | None = 1.0, pick_age: float | None = 1.0) -> None:
    async with store.session() as conn:
        if poll_age is not None:
            await meta.put(conn, crossread.LAST_POLL_KEY, NOW - poll_age)
        if pick_age is not None:
            await meta.put(conn, crossread.PICKED_AT_KEY, NOW - pick_age)


async def test_scout_is_ok_when_every_loop_beat_and_it_picked(fresh_db: str) -> None:
    await _beat_all(health.SCOUT_LIMITS_S)
    await _scout_state()
    code, body = await health.scout_snapshot(now=NOW)
    assert code == 200 and body["status"] == "ok" and body["program"] == "scout"


async def test_scout_names_a_quiet_loop(fresh_db: str) -> None:
    await _beat_all(health.SCOUT_LIMITS_S, skip="grade")
    await _scout_state()
    code, body = await health.scout_snapshot(now=NOW)
    assert code == 503 and body["status"] == "stale: grade"


@pytest.mark.parametrize(("age", "ok"), [(None, False), (600.0, True), (600.1, False)])
async def test_scout_pick_age_at_the_copy_list_limit(fresh_db: str, age: float | None, ok: bool) -> None:
    """The trader refuses opens once the pick is older than COPYLIST_MAX_AGE_S: the scout's health says it too."""
    assert settings.copylist_max_age_s == 600.0
    await _beat_all(health.SCOUT_LIMITS_S)
    await _scout_state(pick_age=age)
    code, body = await health.scout_snapshot(now=NOW)
    assert (code == 200) is ok and (ok or body["status"] == "stale: pick")


@pytest.mark.parametrize(("age", "ok"), [(None, False), (300.0, True), (300.1, False)])
async def test_scout_feed_poll_age_at_its_limit(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, age: float | None, ok: bool
) -> None:
    """A loop beats even when its pass fails: the last SUCCESSFUL poll is what shows a broken feed."""
    monkeypatch.setattr(settings, "solana_rpc_url", "https://rpc.invalid/placeholder")
    await _beat_all(health.SCOUT_LIMITS_S)
    await _scout_state(poll_age=age)
    code, body = await health.scout_snapshot(now=NOW)
    assert (code == 200) is ok and (ok or body["status"] == "stale: feed poll")


async def test_scout_without_a_solana_url_does_not_fail_on_the_feed(fresh_db: str) -> None:
    await _beat_all(health.SCOUT_LIMITS_S)
    await _scout_state(poll_age=None)
    code, _ = await health.scout_snapshot(now=NOW)
    assert code == 200


def _feed(*, enabled: bool = True, down: float | None = None) -> dict[str, object]:
    return {"enabled": enabled, "connected": down is None, "down_for_s": down}


async def _trader(
    *, list_age: float | None = 1.0, feed: dict[str, object] | None = None, worker: bool = True
) -> tuple[int, dict[str, object]]:
    return await health.trader_snapshot(
        list_age=list_age, listed={"tracked": 1, "watched": 0}, feed=feed if feed is not None else _feed(),
        worker_alive=worker, now=NOW,
    )  # fmt: skip


async def test_trader_is_ok_when_everything_is_fresh(fresh_db: str) -> None:
    await _beat_all(health.TRADER_LIMITS_S)
    code, body = await _trader()
    assert code == 200 and body["status"] == "ok" and body["program"] == "trader"


async def test_trader_names_a_quiet_loop(fresh_db: str) -> None:
    await _beat_all(health.TRADER_LIMITS_S, skip="feedpoll")
    code, body = await _trader()
    assert code == 503 and body["status"] == "stale: feedpoll"


@pytest.mark.parametrize(("age", "ok"), [(None, False), (600.0, True), (600.1, False)])
async def test_trader_copy_list_age_at_its_limit(fresh_db: str, age: float | None, ok: bool) -> None:
    await _beat_all(health.TRADER_LIMITS_S)
    code, body = await _trader(list_age=age)
    assert (code == 200) is ok and (ok or body["status"] == "stale: copy list")


@pytest.mark.parametrize(("down", "ok"), [(None, True), (300.0, True), (300.1, False)])
async def test_trader_websocket_down_at_its_limit(fresh_db: str, down: float | None, ok: bool) -> None:
    await _beat_all(health.TRADER_LIMITS_S)
    code, body = await _trader(feed=_feed(down=down))
    assert (code == 200) is ok and (ok or body["status"] == "stale: websocket")


async def test_trader_with_the_websocket_off_does_not_fail_on_it(fresh_db: str) -> None:
    await _beat_all(health.TRADER_LIMITS_S)
    code, _ = await _trader(feed=_feed(enabled=False, down=9999.0))
    assert code == 200


async def test_trader_names_a_dead_feed_worker(fresh_db: str) -> None:
    await _beat_all(health.TRADER_LIMITS_S)
    code, body = await _trader(worker=False)
    assert code == 503 and body["status"] == "stale: feed worker"
