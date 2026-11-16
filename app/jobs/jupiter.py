"""Jupiter Perps wallets: record, grade, and turn their trades into copy signals (signal source only).

Every JUPITER_POLL_SECONDS the change feed (`data/jupiter_perps.updated_request`) returns every Position account
updated in the current ~18 h block (and the previous block once, right after the block changes). Each account
whose `updateTime` is newer than what we stored (table `jup_positions` holds the OPEN ones we know):
- open now, unknown to us, opened after our last poll   -> OPEN (a signal if the owner is tracked)
- open now, unknown, opened earlier (we were not looking) -> stored silently (old news, no signal)
- open now and known                                     -> size/price updated (ADD / REDUCE ignored)
- closed now (sizeUsd 0) and known                       -> CLOSE, plus an episode valued at the KuCoin MARK of the
                                                            coin at that moment, and the row is removed
- closed and unknown                                     -> ignored
The first poll, or one after a gap of more than BASELINE_GAP_POLLS polls, is a BASELINE: rows are stored, no
signal. `verify_known` (hourly, and after a gap) re-reads every stored row with getMultipleAccounts, so a close
missed while the bot was down still becomes an episode.

Grading (`grade_owners`), once an owner has JUPITER_MIN_RECORD_DAYS of recording and JUPITER_MIN_EPISODES closed
episodes. No account value is public, so the Hyperliquid rules are mapped like this (DECISIONS D-6):
- drawdown = the largest fall of cumulative episode PnL, divided by the owner's largest position size
- GOLD = PF >= GOLD_MIN_PROFIT_FACTOR, drawdown <= GOLD_MAX_DRAWDOWN, total PnL > 0, no single episode above
  MAX_BIGGEST_WIN_SHARE of the profit, median hold not a scalper's.
The best JUPITER_MAX_TRACKED GOLD owners are tracked; their OPENs reach the copier like Hyperliquid ones.
"""

import statistics
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.config import settings
from app.core import selection
from app.core import wallet_grader as wg
from app.core import wallet_stats as ws
from app.data.jupiter_perps import JupPosition
from app.db import meta, store
from app.trading import shadow
from app.utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "jupiter"
BASELINE_GAP_POLLS = 3
LAST_POLL_KEY = "jup:last_poll"


@dataclass
class SnapshotResult:
    seen: int = 0
    opens: int = 0
    closes: int = 0
    episodes: int = 0
    baseline: bool = False


def episode_pnl(side: str, size_usd: float, entry: float, exit_px: float) -> float:
    if entry <= 0:
        return 0.0
    move = (exit_px - entry) / entry
    return size_usd * (move if side == "long" else -move)


def drawdown_of(eps: list[ws.Episode], scale: float) -> float | None:
    """Largest fall of cumulative PnL from its peak, as a share of `scale` (the owner's largest position)."""
    if not eps or scale <= 0:
        return None
    peak = cum = worst = 0.0
    for e in sorted(eps, key=lambda x: x.closed_ms):
        cum += e.pnl
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return min(1.0, worst / scale)


async def last_poll() -> float | None:
    async with store.session() as conn:
        got = await meta.get(conn, LAST_POLL_KEY)
    return float(got[0]) if got else None


def is_baseline(last: float | None, now: float) -> bool:
    return last is None or now - last > BASELINE_GAP_POLLS * settings.jupiter_poll_seconds


async def _close(
    conn: aiosqlite.Connection, old: dict[str, Any], marks: dict[str, float], tracked: dict[str, str], at: float,
    res: SnapshotResult,
) -> None:  # fmt: skip
    res.closes += 1
    exit_px = marks.get(str(old["coin"]), 0.0)
    if exit_px > 0:
        pnl = episode_pnl(str(old["side"]), float(old["size_usd"]), float(old["entry_px"]), exit_px)
        await conn.execute(
            "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (old["owner"], old["coin"], old["side"], old["size_usd"], old["entry_px"], exit_px,
             float(old["open_time"]), at, pnl),
        )  # fmt: skip
        res.episodes += 1
    if old["owner"] in tracked:
        await _signal(
            conn, str(old["owner"]), str(old["coin"]), "CLOSE", str(old["side"]), tracked[str(old["owner"])],
            float(old["size_usd"]), float(old["entry_px"]), at,
        )  # fmt: skip
    await conn.execute("DELETE FROM jup_positions WHERE pubkey = ?", (old["pubkey"],))


async def _store(conn: aiosqlite.Connection, p: JupPosition, at: float) -> None:
    await conn.execute(
        "INSERT INTO jup_positions (pubkey, owner, coin, side, size_usd, entry_px, open_time, seen_at, update_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(pubkey) DO UPDATE SET size_usd = excluded.size_usd, "
        "entry_px = excluded.entry_px, seen_at = excluded.seen_at, update_time = excluded.update_time",
        (p.pubkey, p.owner, p.coin, p.side, p.size_usd, p.entry_px, p.open_time, at, p.update_time),
    )


async def process_updates(
    positions: list[JupPosition], marks: dict[str, float], *, now: float | None = None
) -> SnapshotResult:
    """Apply one change-feed answer (see the module docstring)."""
    at = time.time() if now is None else now
    res = SnapshotResult(seen=len(positions))
    async with store.session() as conn:
        got = await meta.get(conn, LAST_POLL_KEY)
        last = float(got[0]) if got else None
        res.baseline = is_baseline(last, at)
        async with conn.execute("SELECT * FROM jup_positions") as cur:
            known = {str(r["pubkey"]): dict(r) for r in await cur.fetchall()}
        async with conn.execute(
            "SELECT address, tier FROM wallets WHERE (tracked = 1 OR watched = 1) AND source = ?", (SOURCE,)
        ) as cur:
            tracked = {str(r["address"]): str(r["tier"]) for r in await cur.fetchall()}
        for p in positions:
            old = known.get(p.pubkey)
            if old is not None and p.update_time <= int(old.get("update_time") or 0):
                continue  # nothing new on this account
            if p.is_open:
                if p.size_usd < settings.jupiter_min_size_usd:
                    if old is not None:
                        await conn.execute("DELETE FROM jup_positions WHERE pubkey = ?", (p.pubkey,))
                    continue
                await conn.execute(
                    "INSERT INTO jup_owners (owner, first_seen, max_open_usd) VALUES (?, ?, ?) ON CONFLICT(owner) "
                    "DO UPDATE SET max_open_usd = MAX(jup_owners.max_open_usd, excluded.max_open_usd)",
                    (p.owner, at, p.size_usd),
                )
                fresh_open = old is None and not res.baseline and last is not None and p.open_time > last - 5
                await _store(conn, p, at)
                if fresh_open:
                    res.opens += 1
                    if p.owner in tracked:
                        await _signal(
                            conn, p.owner, p.coin, "OPEN", p.side, tracked[p.owner], p.size_usd, p.entry_px, at
                        )
            elif old is not None:
                await _close(conn, old, marks, tracked, at, res)
        await meta.put(conn, LAST_POLL_KEY, at, now=at)
    return res


async def known_pubkeys() -> list[str]:
    async with store.session() as conn:
        async with conn.execute("SELECT pubkey FROM jup_positions ORDER BY pubkey") as cur:
            return [str(r["pubkey"]) for r in await cur.fetchall()]


async def verify_known(
    current: dict[str, JupPosition | None], marks: dict[str, float], *, now: float | None = None
) -> int:
    """Close every stored row that the chain says is closed (or gone). Returns how many were closed."""
    at = time.time() if now is None else now
    res = SnapshotResult()
    async with store.session() as conn:
        async with conn.execute("SELECT * FROM jup_positions") as cur:
            known = {str(r["pubkey"]): dict(r) for r in await cur.fetchall()}
        async with conn.execute(
            "SELECT address, tier FROM wallets WHERE (tracked = 1 OR watched = 1) AND source = ?", (SOURCE,)
        ) as cur:
            tracked = {str(r["address"]): str(r["tier"]) for r in await cur.fetchall()}
        for key, p in current.items():
            old = known.get(key)
            if old is None:
                continue
            if p is None or not p.is_open:
                await _close(conn, old, marks, tracked, at, res)
            elif p.update_time > int(old.get("update_time") or 0):
                await _store(conn, p, at)
    return res.closes


async def _signal(
    conn: object, owner: str, coin: str, kind: str, side: str, tier: str, size_usd: float, entry: float, at: float
) -> None:
    await conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO signals (ts, wallet, source, coin, kind, side, tier, wallet_size, wallet_entry_px, "
        "wallet_leverage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (at, owner, SOURCE, coin, kind, side, tier, size_usd, entry),
    )


def grade_episodes(
    eps: list[ws.Episode], *, max_size: float, recorded_days: float
) -> tuple[wg.Grade, ws.WalletStats, float | None]:
    """A Jupiter owner's grade from its recorded episodes. Pure; see the module docstring for the rules."""
    st = ws.stats(eps, [], set(), equity=0.0)
    dd = drawdown_of(eps, max_size)
    style = wg.SWING
    if st.median_hold_s is not None and st.median_hold_s < settings.scalper_max_median_hold_min * 60:
        style = wg.SCALPER
    elif st.median_hold_s is not None and st.median_hold_s >= settings.position_min_median_hold_hours * 3600:
        style = wg.POSITION
    if recorded_days < settings.jupiter_min_record_days or st.trades < settings.jupiter_min_episodes:
        return wg.Grade(style, wg.WHITE, f"recording: {recorded_days:.1f} days, {st.trades} episodes"), st, dd
    if style == wg.SCALPER and not settings.copy_fast_traders:
        return wg.Grade(style, wg.BLACK, "scalper style cannot be copied with a delay"), st, dd
    if st.total_pnl <= 0:
        return wg.Grade(style, wg.BLACK, f"lost money over {st.trades} episodes"), st, dd
    pf = st.profit_factor
    figures = f"{st.trades} episodes, PF {pf or 0:.2f}, DD {dd or 0:.0%}, {recorded_days:.0f}d recorded"
    if st.biggest_win_share is not None and st.biggest_win_share > settings.max_biggest_win_share:
        return wg.Grade(style, wg.BRONZE, f"{figures}: one trade made {st.biggest_win_share:.0%} of the profit"), st, dd
    if pf is not None and pf >= settings.gold_min_profit_factor and dd is not None and dd <= settings.gold_max_drawdown:
        return wg.Grade(style, wg.GOLD, figures), st, dd
    if pf is not None and pf >= settings.silver_min_profit_factor:
        return wg.Grade(style, wg.SILVER, figures), st, dd
    return wg.Grade(style, wg.BRONZE, figures), st, dd


async def _owners_to_grade(at: float) -> list[tuple[str, float]]:
    """(owner, history start) for owners with enough history and a new episode since their last grade."""
    since = at - settings.jupiter_min_record_days * 86400
    async with store.session() as conn:
        async with conn.execute(
            "SELECT o.owner, MIN(o.first_seen, COALESCE(o.history_from, o.first_seen)) AS start, COUNT(e.id) AS n, "
            "MAX(e.closed_at) AS last_close, w.graded_at AS graded_at FROM jup_owners o "
            "JOIN jup_episodes e ON e.owner = o.owner LEFT JOIN wallets w ON w.address = o.owner "
            "GROUP BY o.owner HAVING start <= ? AND n >= ? AND (graded_at IS NULL OR last_close > graded_at)",
            (since, settings.jupiter_min_episodes),
        ) as cur:
            return [(str(r["owner"]), float(r["start"])) for r in await cur.fetchall()]


async def grade_owners(*, now: float | None = None) -> int:
    """Grade every owner with enough history (recorded or read from the chain, item 1) and a new closed episode, then
    pick tracked / watched Jupiter owners with `core/selection` (same rule as Hyperliquid). Returns how many are
    tracked."""
    at = time.time() if now is None else now
    owners = await _owners_to_grade(at)
    for owner, start in owners:
        async with store.session() as conn:
            async with conn.execute(
                "SELECT coin, side, opened_at, closed_at, pnl, size_usd FROM jup_episodes WHERE owner = ? "
                "ORDER BY closed_at",
                (owner,),
            ) as cur:
                rows = list(await cur.fetchall())
        eps = [
            ws.Episode(str(r["coin"]), str(r["side"]), int(float(r["opened_at"]) * 1000),
                       int(float(r["closed_at"]) * 1000), float(r["pnl"]), float(r["size_usd"]))
            for r in rows
        ]  # fmt: skip
        max_size = max(float(r["size_usd"]) for r in rows)
        recorded = (at - start) / 86400
        grade, st, dd = grade_episodes(eps, max_size=max_size, recorded_days=recorded)
        now_ms = int(at * 1000)
        window = min(recorded, settings.weight_window_days)
        losses = ws.recent_losses(eps, now_ms=now_ms, days=window, equity=0.0)
        weight = wg.copy_weight(wg.losses_per_month(losses, window))
        parts = wg.wallet_score(eps, now_ms=now_ms, equity=0.0, long=None)
        async with store.session() as conn:
            await conn.execute(
                "INSERT INTO wallets (address, source, style, tier, trades, win_rate, profit_factor, max_drawdown, "
                "median_hold_s, window_pnl, reason, graded_at, history_days, score, copy_weight, account_value, "
                "last_fill_at, count_score) VALUES (?, 'jupiter', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET style=excluded.style, tier=excluded.tier, trades=excluded.trades, "
                "win_rate=excluded.win_rate, profit_factor=excluded.profit_factor, max_drawdown=excluded.max_drawdown, "
                "median_hold_s=excluded.median_hold_s, window_pnl=excluded.window_pnl, reason=excluded.reason, "
                "graded_at=excluded.graded_at, history_days=excluded.history_days, score=excluded.score, "
                "copy_weight=excluded.copy_weight, account_value=excluded.account_value, "
                "last_fill_at=excluded.last_fill_at, count_score=excluded.count_score",
                (owner, grade.style, grade.tier, st.trades, st.win_rate, st.profit_factor, dd, st.median_hold_s,
                 st.total_pnl, grade.reason, at, recorded, parts.score, weight, statistics.median(
                     [float(r["size_usd"]) for r in rows]), float(rows[-1]["closed_at"]), wg.score(st, dd)),
            )  # fmt: skip
    return await pick_owners(now=at, graded=len(owners))


async def pick_owners(*, now: float | None = None, graded: int = 0) -> int:
    """Tracked / watched Jupiter owners by `core/selection.pick`. The grade score is stored in `count_score`."""
    at = time.time() if now is None else now
    shadows = await shadow.stats(now=at)
    async with store.session() as conn:
        async with conn.execute(
            "SELECT address, tier, count_score FROM wallets WHERE source = ? AND graded_at >= ?",
            (SOURCE, at - settings.track_max_grade_age_hours * 3600),
        ) as cur:
            rows = await cur.fetchall()
        cands = []
        for r in rows:
            sh = shadows.get(str(r["address"]))
            cands.append(
                selection.Candidate(str(r["address"]), str(r["tier"]), float(r["count_score"] or 0.0),
                                    sh.n if sh else 0, sh.mean if sh else None)
            )  # fmt: skip
        tracked, watched = selection.pick(
            cands, max_total=settings.jupiter_max_tracked, max_silver=settings.max_tracked_silver
        )
        await conn.execute("UPDATE wallets SET tracked = 0, watched = 0 WHERE source = ?", (SOURCE,))
        for owner in tracked:
            await conn.execute("UPDATE wallets SET tracked = 1 WHERE address = ?", (owner,))
        for owner in watched:
            await conn.execute("UPDATE wallets SET watched = 1 WHERE address = ?", (owner,))
    if graded:
        logger.info("jupiter: graded %d owners, tracking %d, watching %d", graded, len(tracked), len(watched))
    return len(tracked)
