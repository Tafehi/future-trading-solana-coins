"""/health: it must watch exactly the loops main.py runs (DEBUG D-11: it watched two loops that no longer exist)."""

import re
from pathlib import Path

from app.api import health
from app.db import meta, store

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def test_health_watches_exactly_the_loops_main_runs() -> None:
    loops = set(re.findall(r'self\._loop\("([a-z_]+)"', MAIN.read_text())) - {"reconcile"}  # reconcile: live only
    assert set(health.LIMITS_S) == loops


async def test_health_is_ok_when_every_loop_beat(fresh_db: str) -> None:
    async with store.session() as conn:
        for name in health.LIMITS_S:
            await meta.beat(conn, name)
    code, body = await health.snapshot()
    assert code == 200 and body["status"] == "ok"


async def test_health_names_a_quiet_loop(fresh_db: str) -> None:
    async with store.session() as conn:
        for name in health.LIMITS_S:
            if name != "jupiter":
                await meta.beat(conn, name)
    code, body = await health.snapshot()
    assert code == 503 and body["status"] == "stale: jupiter"
