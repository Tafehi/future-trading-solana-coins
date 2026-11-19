"""The wallet-rule backtest (`app/tools/criteria_backtest.py`): no look-ahead (tested AT the cutoff), the same answer
from the bit masks and from `passes`, the pooled numbers, today's rules from settings, and a read-only database."""

import hashlib
import math
import random
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.db import migrations, store
from app.tools import criteria_backtest as cb

DAY = 86400.0
END = 1_800_000_000.0


def trade(opened: float, move_pct: float, *, hold_h: float = 2.0, size: float = 5000.0, side: str = "long",
          coin: str = "SOL") -> cb.Trade:  # fmt: skip
    exit_px = 100.0 * (1 + move_pct / 100) if side == "long" else 100.0 * (1 - move_pct / 100)
    return cb.Trade(coin, side, opened, opened + hold_h * 3600, size * move_pct / 100, size, 100.0, exit_px)


async def write_db(path: str, owners: dict[str, list[cb.Trade]]) -> None:
    await migrations.migrate(path)
    async with store.session(path) as conn:
        for owner, ts in owners.items():
            await conn.execute(
                "INSERT INTO jup_owners (owner, first_seen, history_from, max_open_usd) VALUES (?, ?, ?, 5000)",
                (owner, END, END - 90 * DAY),
            )
            for t in ts:
                await conn.execute(
                    "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, "
                    "pnl, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'api')",
                    (owner, t.coin, t.side, t.size, t.entry, t.exit, t.opened, t.closed, t.pnl),
                )


def test_a_return_is_the_owners_move_minus_two_taker_fees() -> None:
    assert cb.ret_pct(trade(0, 10.0)) == pytest.approx(10.0 - 0.12)
    assert cb.ret_pct(trade(0, 10.0, side="short")) == pytest.approx(10.0 - 0.12)  # price fell 10%: a short won
    assert cb.ret_pct(trade(0, -10.0, side="short")) == pytest.approx(-10.0 - 0.12)


def test_no_look_ahead_at_the_cutoff() -> None:
    t = END - 10 * DAY
    h = 10 * DAY
    closed_before = cb.Trade("SOL", "long", t - 3600, t - 1e-3, 50, 5000, 100, 101)
    closed_at = cb.Trade("SOL", "long", t - 3600, t, 50, 5000, 100, 101)  # closed AT the cutoff: not yet known
    opened_at = cb.Trade("SOL", "long", t, t + 3600, 50, 5000, 100, 101)  # opened AT the cutoff: a copy
    across = cb.Trade("SOL", "long", t - 60, t + 3600, 50, 5000, 100, 101)  # open across the cutoff: neither
    next_window = cb.Trade("SOL", "long", t + h, t + h + 60, 50, 5000, 100, 101)
    small = cb.Trade("SOL", "long", t + 60, t + 120, 5, settings.jupiter_min_size_usd - 0.01, 100, 101)
    ts = [closed_before, closed_at, opened_at, across, next_window, small]
    past = cb.past_at(ts, END - 90 * DAY, t)
    assert past is not None and past.n == 1  # only the trade closed BEFORE the cutoff
    assert cb.future_at(ts, t, h).n == 1  # only the trade opened at the cutoff, within the horizon, big enough


@pytest.fixture
def two_owners() -> dict[str, list[cb.Trade]]:
    cut = END - 10 * DAY
    good_past = [trade(cut - (20 - i) * DAY, 3.0 if i % 5 else -1.0) for i in range(10)]  # 8 wins, 2 small losses
    bad_past = [trade(cut - (20 - i) * DAY, -2.0) for i in range(10)]
    great_future = [trade(cut + i * DAY, 4.0) for i in range(5)]
    return {
        "GOOD": good_past + [trade(cut + i * DAY, 2.0) for i in range(4)],
        "BAD": bad_past + great_future,  # a great future cannot buy a pick: the rule never sees it
        "END": [trade(END - 3600 * 3, 1.0)],  # sets the end of the data
    }


def test_only_the_past_decides_who_is_picked(two_owners: dict[str, list[cb.Trade]]) -> None:
    fold = cb.folds(two_owners, {}, k=1, horizon_days=10)[0]
    res = fold.evaluate(cb.current_rules()["now: GOLD+SILVER pool"])
    assert (res.picked, res.active, res.good, res.trades) == (1, 1, 1, 4)
    assert res.pooled_mean == pytest.approx(2.0 - 0.12)


def test_the_pooled_numbers_cover_every_picked_trade() -> None:
    cut = END - 10 * DAY
    past = [trade(cut - (20 - i) * DAY, 3.0 if i % 5 else -1.0) for i in range(10)]
    owners = {
        "A": past + [trade(cut + DAY, 5.12), trade(cut + 2 * DAY, -1.88)],  # +5.00% and -2.00% after fees
        "B": past + [trade(cut + 3 * DAY, 3.12)],  # +3.00%
        "END": [trade(END - 3 * 3600, 1.0)],
    }
    res = cb.folds(owners, {}, k=1, horizon_days=10)[0].evaluate(cb.current_rules()["now: GOLD+SILVER pool"])
    assert res.trades == 3 and res.pooled_pf == pytest.approx(8.0 / 2.0) and res.pooled_mean == pytest.approx(2.0)
    assert res.good == 0  # A: 2 trades and B: 1 trade, both below FUTURE_MIN_TRADES


def test_the_small_sample_proof_raises_the_bar_below_15_trades() -> None:
    cut = END - 10 * DAY
    # 9 trades, PF exactly 2.0: 6 wins of +1%, 3 losses of -1%.
    ts = [trade(cut - (20 - i) * DAY, 1.0 if i < 6 else -1.0) for i in range(9)]
    p = cb.past_at(ts, END - 90 * DAY, cut)
    assert p is not None and p.pf_plain == pytest.approx(2.0)
    base = cb.current_rules()["now: GOLD+SILVER pool"]
    plain = cb.Rule(8, 1.8, 0, cb.NO_LIMIT, 1, 1.0, 0.0, cb.NO_LIMIT, 0.0)
    proof = cb.Rule(8, 1.8, 1, cb.NO_LIMIT, 1, 1.0, 0.0, cb.NO_LIMIT, 0.0)
    assert base.small_sample_proof == 1
    assert cb.passes(plain, p) and not cb.passes(proof, p)  # proof needs 1.8 x sqrt(15/9) = 2.32
    assert math.isclose(p.proof_scale, math.sqrt(15 / 9))


def _random_owners(seed: int) -> dict[str, list[cb.Trade]]:
    rnd = random.Random(seed)  # noqa: S311 — test data, not security
    out: dict[str, list[cb.Trade]] = {}
    for o in range(40):
        ts = []
        for _ in range(rnd.randint(1, 40)):
            opened = END - rnd.uniform(0, 89) * DAY
            ts.append(trade(opened, rnd.gauss(0.3, 3.0), hold_h=rnd.uniform(0.1, 60), size=rnd.uniform(500, 40000),
                            side=rnd.choice(["long", "short"])))  # fmt: skip
        out[f"O{o}"] = sorted(ts, key=lambda t: t.closed)
    return out


def test_the_bit_masks_give_the_same_answer_as_passes() -> None:
    fs = cb.folds(_random_owners(7), {}, k=2, horizon_days=10)
    rules = list(cb.grid())
    sample = random.Random(11).sample(rules, 400) + list(cb.current_rules().values())  # noqa: S311
    for f in fs:
        for r in sample:
            assert f.evaluate(r) == f.evaluate_plain(r), r


def test_robust_needs_every_fold() -> None:
    ok = cb.FoldResult(5, 5, 3, 20, 0.5, 1.6)
    weak = cb.FoldResult(5, 5, 4, 20, 0.5, 1.29)  # PF just under the bar
    r = cb.current_rules()["now: GOLD only"]
    assert cb.Scored(r, [ok, ok]).robust and not cb.Scored(r, [ok, weak]).robust
    assert cb.rank([cb.Scored(r, [ok, weak])]) == []
    assert cb.FoldResult(5, 5, 3, 20, 0.5, 1.3).ok  # exactly the bar passes


def test_todays_rules_come_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    pool, gold = cb.current_rules()["now: GOLD+SILVER pool"], cb.current_rules()["now: GOLD only"]
    assert (pool.min_trades, pool.min_pf, pool.max_dd) == (settings.jupiter_min_episodes,
                                                           settings.silver_min_profit_factor, cb.NO_LIMIT)  # fmt: skip
    assert (gold.min_pf, gold.max_dd) == (settings.gold_min_profit_factor, settings.gold_max_drawdown)
    assert (pool.max_win_share, pool.min_win_rate, pool.max_idle_days) == (  # D-31
        settings.max_biggest_win_share, settings.jupiter_min_win_rate, settings.jupiter_max_idle_days)  # fmt: skip
    monkeypatch.setattr(settings, "jupiter_min_episodes", 12)
    assert cb.current_rules()["now: GOLD only"].min_trades == 12  # follows the setting, never a copy of it


async def test_the_database_is_only_read_and_the_report_is_written(
    tmp_path: Path, two_owners: dict[str, list[cb.Trade]], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    db = str(tmp_path / "bucket" / "solana-future-coin.db")
    Path(db).parent.mkdir()
    await write_db(db, two_owners)
    before = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    monkeypatch.setattr(sys, "argv", ["criteria_backtest", "--db", db, "--folds", "1"])
    cb.main()
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before
    out = tmp_path / "analysis" / "criteria"
    md = (out / "criteria.md").read_text()
    assert "now: GOLD+SILVER pool" in md and "before D-29" in md and (out / "criteria.csv").is_file()
    assert "Best robust rule sets" in capsys.readouterr().out
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        cb.connect_ro(str(missing))
    assert not missing.exists()
