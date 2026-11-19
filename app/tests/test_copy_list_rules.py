"""D-31 (2026-11-19): the three copy-list rules the wallet-rule backtest chose, each tested AT its boundary value.

- The biggest single win may be at most MAX_BIGGEST_WIN_SHARE (0.5) of the owner's profit: exactly half passes.
- At least JUPITER_MIN_WIN_RATE (40%) of the trades must win: exactly 40% passes.
- `pick_owners` skips an owner whose last close is older than JUPITER_MAX_IDLE_DAYS (14): exactly 14 days passes.
"""

from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS_FILE, Settings, settings
from app.core import wallet_grader as wg
from app.core import wallet_stats as ws
from app.db import store
from app.jobs import jupiter

NOW = 1_800_000_000.0
DAY = 86400.0


def eps(*pnls: float) -> list[ws.Episode]:
    """One SOL long per day, held 2 h, $10,000 each."""
    return [ws.Episode("SOL", "long", int(i * DAY * 1000), int((i * DAY + 7200) * 1000), p, 10_000.0)
            for i, p in enumerate(pnls)]  # fmt: skip


def grade(e: list[ws.Episode]) -> wg.Grade:
    return jupiter.grade_episodes(e, max_size=10_000.0, recorded_days=60.0)[0]


def test_the_d31_values_are_the_defaults_and_in_the_settings_file(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (settings.max_biggest_win_share, settings.jupiter_min_win_rate, settings.jupiter_max_idle_days) == (
        0.5, 0.4, 14.0)  # fmt: skip
    monkeypatch.setenv("SETTINGS_FILE", str(Path(DEFAULT_SETTINGS_FILE)))
    s = Settings()
    assert (s.max_biggest_win_share, s.jupiter_min_win_rate, s.jupiter_max_idle_days) == (0.5, 0.4, 14.0)


def test_a_biggest_win_of_exactly_half_the_profit_passes() -> None:
    # 20 trades: one win of 500, 13 wins of 50, 6 losses of 25 -> total 1,000, biggest share exactly 0.5.
    half = eps(500.0, *[50.0] * 13, *[-25.0] * 6)
    assert grade(half).tier == wg.GOLD
    above = eps(501.0, *[50.0] * 13, *[-25.0] * 6)  # 501 / 1,001 > 0.5
    g = grade(above)
    assert g.tier == wg.BRONZE and "one trade made 50% of the profit" in g.reason


def test_a_win_rate_of_exactly_40_percent_passes() -> None:
    exactly = eps(*[100.0] * 8, *[-20.0] * 12)  # 8 of 20 win; PF 3.3, no single big win
    assert grade(exactly).tier == wg.GOLD
    below = eps(*[100.0] * 7, *[-20.0] * 13)  # 7 of 20 = 35%
    g = grade(below)
    assert g.tier == wg.BRONZE and "wins 35% of its trades (needs 40%)" in g.reason


async def test_an_owner_idle_longer_than_14_days_is_not_picked(fresh_db: str) -> None:
    async with store.session() as conn:
        for address, last in (("ON-TIME", NOW - 14 * DAY), ("LATE", NOW - 14 * DAY - 1)):
            await conn.execute(
                "INSERT INTO wallets (address, source, tier, graded_at, count_score, last_fill_at) "
                "VALUES (?, 'jupiter', 'GOLD', ?, 5, ?)",
                (address, NOW, last),
            )
    assert await jupiter.pick_owners(now=NOW) == 1
    async with store.session() as conn:
        async with conn.execute("SELECT address, tracked, watched FROM wallets ORDER BY address") as cur:
            flags = {str(r["address"]): (int(r["tracked"]), int(r["watched"])) for r in await cur.fetchall()}
    assert flags == {"LATE": (0, 0), "ON-TIME": (1, 0)}  # a last close exactly 14 days ago still counts
