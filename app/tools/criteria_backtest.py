"""Which wallet rules pick the MOST wallets that stay good afterwards? A walk-forward test on a downloaded database:
`python -m app.tools.criteria_backtest --db FILE [--db FILE2] [--out DIR] [--folds 3] [--horizon-days 10]`.

Run on the laptop after `scripts/collect_evidence.sh` (it runs this too). With several `--db` the file with the most
`jup_episodes` rows is used (the scout's `scout.db`, the trader's file, or a backup from before D-30). Every file is
opened read-only (`mode=ro`). No network, no AWS.

Method (no look-ahead: a rule only sees what the scout knew at the time):
- One cutoff T per fold, `horizon` days apart; the last one `horizon` days before the data ends.
- PAST at T: the owner's episodes CLOSED before T, graded with the bot's own math (`core/wallet_stats.stats`,
  `jobs/jupiter.drawdown_of`, `jobs/jupiter.proof`), so a rule here means the same as in `settings.toml`.
- FUTURE at T: the owner's trades of at least JUPITER_MIN_SIZE_USD OPENED in [T, T + horizon) and closed by the end
  of the data: the opens we would have copied after picking the wallet at T. A trade open across T is in neither.
- A future trade's return = its price move in the owner's direction in %, minus two taker fees (TAKER_FEE_PCT each):
  a copy that entered and left at the owner's prices. NOT our exits (6% stop, trail, 72 h): the shadow copies on the
  box measure those, but they take days.
- GOOD wallet in a fold: at least FUTURE_MIN_TRADES future trades, mean return > 0 and profit factor >= GOOD_MIN_PF
  (the go-live bar). ACTIVE wallet: at least one future trade.
- Per rule set and fold: picked, active, good, and over ALL picked wallets' future trades the pooled mean return and
  profit factor (what copying the whole group would have earned per trade).
- ROBUST rule: pooled PF >= GOOD_MIN_PF and pooled mean > 0 in EVERY fold (a rule that works in one window is luck).
  Ranked by the good wallets in its WORST fold, then by all good wallets, then by its worst pooled PF.

Known bias: the database holds only owners the bot saw trading in November 2026, so wallets that stopped earlier are
missing. That flatters the early folds most; the last fold is the fairest. The current rules (read from settings)
are always in the report, so a change shows what it buys.
"""

import argparse
import csv
import itertools
import sqlite3
import statistics
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import settings
from app.core import wallet_grader as wg
from app.core import wallet_stats as ws
from app.jobs import jupiter

TAKER_FEE_PCT = 0.06  # KuCoin USDT-M taker fee, per side
FUTURE_MIN_TRADES = 3
GOOD_MIN_PF = 1.3  # = the go-live check's profit factor bar
NO_LIMIT = 1e9
DAY = 86400.0

# The grid. Every value is a choice a setting can make (see `Rule`).
GRID: dict[str, list[float]] = {
    "min_trades": [5, 8, 10, 15, 20, 30],
    "min_pf": [1.2, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0],
    "small_sample_proof": [1, 0],
    "max_dd": [0.25, 0.35, 0.5, NO_LIMIT],
    "no_scalpers": [1, 0],
    "max_win_share": [0.5, 1.0],
    "min_win_rate": [0.0, 0.4, 0.5, 0.6],
    "max_idle_days": [3, 7, 14, NO_LIMIT],
    "min_median_size": [0.0, 5000.0, 20000.0],
}


@dataclass(frozen=True)
class Trade:
    coin: str
    side: str
    opened: float
    closed: float
    pnl: float
    size: float
    entry: float
    exit: float


@dataclass(frozen=True)
class Rule:
    min_trades: int  # JUPITER_MIN_EPISODES
    min_pf: float  # the profit factor needed (SILVER_MIN_PROFIT_FACTOR for the whole pool)
    small_sample_proof: int  # 1 = D-29: below 15 trades the needed PF grows, no-loss counts PF = trades
    max_dd: float  # GOLD_MAX_DRAWDOWN (NO_LIMIT = SILVER: no drawdown check)
    no_scalpers: int  # 1 = median hold below SCALPER_MAX_MEDIAN_HOLD_MIN is refused (COPY_FAST_TRADERS false)
    max_win_share: float  # MAX_BIGGEST_WIN_SHARE (1.0 = no limit)
    min_win_rate: float  # not a setting yet (0 = no limit)
    max_idle_days: float  # not a setting yet: days since the owner's last close (NO_LIMIT = none)
    min_median_size: float  # not a setting yet: the owner's median position size in USD


@dataclass(frozen=True)
class Past:
    n: int
    pf_plain: float | None
    pf_proof: float | None
    proof_scale: float
    dd: float | None
    total: float
    median_hold_s: float | None
    win_share: float | None
    win_rate: float | None
    idle_days: float
    median_size: float
    record_days: float
    score: float


@dataclass(frozen=True)
class Future:
    n: int
    total: float  # sum of returns, %
    gains: float
    losses: float  # positive number

    @property
    def mean(self) -> float:
        return self.total / self.n if self.n else 0.0

    @property
    def pf(self) -> float | None:
        return None if self.n == 0 else (99.0 if self.losses <= 1e-12 else self.gains / self.losses)

    @property
    def good(self) -> bool:
        pf = self.pf
        return self.n >= FUTURE_MIN_TRADES and self.mean > 0 and pf is not None and pf >= GOOD_MIN_PF


def connect_ro(db: str) -> sqlite3.Connection:
    if not Path(db).is_file():
        raise FileNotFoundError(db)
    conn = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def episode_count(db: str) -> int:
    conn = connect_ro(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM jup_episodes").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def load(db: str) -> tuple[dict[str, list[Trade]], dict[str, float]]:
    """owner -> its episodes (oldest close first), and owner -> the start of its known history."""
    conn = connect_ro(db)
    try:
        trades: dict[str, list[Trade]] = {}
        for r in conn.execute(
            "SELECT owner, coin, side, opened_at, closed_at, pnl, size_usd, entry_px, exit_px FROM jup_episodes "
            "ORDER BY closed_at"
        ):
            trades.setdefault(str(r["owner"]), []).append(
                Trade(str(r["coin"]), str(r["side"]), float(r["opened_at"]), float(r["closed_at"]), float(r["pnl"]),
                      float(r["size_usd"]), float(r["entry_px"]), float(r["exit_px"]))
            )  # fmt: skip
        starts = {
            str(r["owner"]): float(r["start"])
            for r in conn.execute(
                "SELECT owner, MIN(first_seen, COALESCE(history_from, first_seen)) AS start FROM jup_owners"
            )
        }  # fmt: skip
    finally:
        conn.close()
    return trades, starts


def ret_pct(t: Trade) -> float:
    """The owner's price move in its direction, in %, minus our two taker fees."""
    if t.entry <= 0:
        return 0.0
    move = (t.exit - t.entry) / t.entry * 100.0
    return (move if t.side == "long" else -move) - 2 * TAKER_FEE_PCT


def past_at(trades: list[Trade], start: float, cutoff: float) -> Past | None:
    eps = [t for t in trades if t.closed < cutoff]
    if not eps:
        return None
    w = [ws.Episode(t.coin, t.side, int(t.opened * 1000), int(t.closed * 1000), t.pnl, t.size) for t in eps]
    st = ws.stats(w, [], set(), equity=0.0)
    dd = jupiter.drawdown_of(w, max(t.size for t in eps))
    pf_proof, scale = jupiter.proof(st)
    return Past(st.trades, st.profit_factor, pf_proof, scale, dd, st.total_pnl, st.median_hold_s, st.biggest_win_share,
                st.win_rate, (cutoff - max(t.closed for t in eps)) / DAY, statistics.median(t.size for t in eps),
                (cutoff - min(start, eps[0].opened)) / DAY, wg.score(st, dd))  # fmt: skip


def future_at(trades: list[Trade], cutoff: float, horizon_s: float) -> Future:
    rets = [ret_pct(t) for t in trades
            if cutoff <= t.opened < cutoff + horizon_s and t.size >= settings.jupiter_min_size_usd]  # fmt: skip
    gains = sum(r for r in rets if r > 0)
    return Future(len(rets), sum(rets), gains, -sum(r for r in rets if r < 0))


def passes(r: Rule, p: Past) -> bool:
    if p.total <= 0 or p.n < r.min_trades or p.record_days < settings.jupiter_min_record_days:
        return False
    pf, need = (p.pf_proof, r.min_pf * p.proof_scale) if r.small_sample_proof else (p.pf_plain, r.min_pf)
    if pf is None or pf < need:
        return False
    if r.max_dd < NO_LIMIT and (p.dd is None or p.dd > r.max_dd):
        return False
    if r.no_scalpers and (p.median_hold_s is None or p.median_hold_s < settings.scalper_max_median_hold_min * 60):
        return False
    if p.win_share is not None and p.win_share > r.max_win_share:
        return False
    if r.min_win_rate > 0 and (p.win_rate is None or p.win_rate < r.min_win_rate):
        return False
    return p.idle_days <= r.max_idle_days and p.median_size >= r.min_median_size


def current_rules() -> dict[str, Rule]:
    """Today's rules, from settings (the whole pool, GOLD + SILVER, and GOLD alone), and two older pools for
    comparison: before D-31 (no win-share, win-rate or activity rule) and before D-29 (15 trades)."""
    now = {"no_scalpers": int(not settings.copy_fast_traders), "max_win_share": settings.max_biggest_win_share,
           "min_win_rate": settings.jupiter_min_win_rate, "max_idle_days": settings.jupiter_max_idle_days,
           "min_median_size": 0.0}  # fmt: skip
    old = {"no_scalpers": 1, "max_win_share": 1.0, "min_win_rate": 0.0, "max_idle_days": NO_LIMIT,
           "min_median_size": 0.0}  # fmt: skip
    n = settings.jupiter_min_episodes
    return {
        "now: GOLD+SILVER pool": Rule(n, settings.silver_min_profit_factor, 1, NO_LIMIT, **now),  # type: ignore[arg-type]
        "now: GOLD only": Rule(n, settings.gold_min_profit_factor, 1, settings.gold_max_drawdown, **now),  # type: ignore[arg-type]
        "before D-31: GOLD+SILVER": Rule(8, 1.4, 1, NO_LIMIT, **old),  # type: ignore[arg-type]
        "before D-29: GOLD+SILVER": Rule(15, 1.4, 0, NO_LIMIT, **old),  # type: ignore[arg-type]
    }


def grid() -> Iterator[Rule]:
    keys = list(GRID)
    for values in itertools.product(*(GRID[k] for k in keys)):
        kw = dict(zip(keys, values, strict=True))
        yield Rule(int(kw["min_trades"]), float(kw["min_pf"]), int(kw["small_sample_proof"]), float(kw["max_dd"]),
                   int(kw["no_scalpers"]), float(kw["max_win_share"]), float(kw["min_win_rate"]),
                   float(kw["max_idle_days"]), float(kw["min_median_size"]))  # fmt: skip


@dataclass(frozen=True)
class FoldResult:
    picked: int
    active: int
    good: int
    trades: int
    pooled_mean: float
    pooled_pf: float | None

    @property
    def ok(self) -> bool:
        return self.pooled_pf is not None and self.pooled_pf >= GOOD_MIN_PF and self.pooled_mean > 0


def _pooled(picked: int, active: int, good: int, futures: list[Future]) -> FoldResult:
    n = sum(f.n for f in futures)
    tot = sum(f.total for f in futures)
    gains = sum(f.gains for f in futures)
    losses = sum(f.losses for f in futures)
    pf = None if n == 0 else (99.0 if losses <= 1e-12 else gains / losses)
    return FoldResult(picked, active, good, n, tot / n if n else 0.0, pf)


@dataclass
class Fold:
    """One cutoff. `evaluate` uses bit masks (one per condition value, owner i = bit i) so 60,000 rule sets run in
    seconds; `evaluate_plain` is the same answer from `passes`, owner by owner (a test keeps them equal)."""

    cutoff: float
    owners: list[str]
    pasts: list[Past]
    futures: list[Future]

    def __post_init__(self) -> None:
        self._masks: dict[tuple[str, object], int] = {}
        self._cache: dict[int, FoldResult] = {}
        self.all = (1 << len(self.pasts)) - 1
        self.active = self._bits(lambda i: self.futures[i].n > 0)
        self.good = self._bits(lambda i: self.futures[i].good)

    def _bits(self, pred: object) -> int:
        m = 0
        for i in range(len(self.pasts)):
            if pred(i):  # type: ignore[operator]
                m |= 1 << i
        return m

    def _m(self, name: str, value: object, pred: object) -> int:
        key = (name, value)
        got = self._masks.get(key)
        if got is None:
            got = self._bits(lambda i: pred(self.pasts[i]))  # type: ignore[operator]
            self._masks[key] = got
        return got

    def mask_of(self, r: Rule) -> int:
        """The owners `r` picks; condition for condition the same as `passes`."""
        hold = settings.scalper_max_median_hold_min * 60
        m = self._m("base", None, lambda p: p.total > 0 and p.record_days >= settings.jupiter_min_record_days)
        m &= self._m("min_trades", r.min_trades, lambda p: p.n >= r.min_trades)
        if r.small_sample_proof:
            m &= self._m("pf", (r.min_pf, 1),
                         lambda p: p.pf_proof is not None and p.pf_proof >= r.min_pf * p.proof_scale)  # fmt: skip
        else:
            m &= self._m("pf", (r.min_pf, 0), lambda p: p.pf_plain is not None and p.pf_plain >= r.min_pf)
        if r.max_dd < NO_LIMIT:
            m &= self._m("dd", r.max_dd, lambda p: p.dd is not None and p.dd <= r.max_dd)
        if r.no_scalpers:
            m &= self._m("scalper", None, lambda p: p.median_hold_s is not None and p.median_hold_s >= hold)
        m &= self._m("win_share", r.max_win_share, lambda p: p.win_share is None or p.win_share <= r.max_win_share)
        if r.min_win_rate > 0:
            m &= self._m("win_rate", r.min_win_rate, lambda p: p.win_rate is not None and p.win_rate >= r.min_win_rate)
        m &= self._m("idle", r.max_idle_days, lambda p: p.idle_days <= r.max_idle_days)
        m &= self._m("size", r.min_median_size, lambda p: p.median_size >= r.min_median_size)
        return m

    def evaluate(self, r: Rule) -> FoldResult:
        sel = self.mask_of(r)
        hit = self._cache.get(sel)
        if hit is None:
            futures, m = [], sel & self.active
            while m:
                low = m & -m
                futures.append(self.futures[low.bit_length() - 1])
                m ^= low
            hit = _pooled(sel.bit_count(), (sel & self.active).bit_count(), (sel & self.good).bit_count(), futures)
            self._cache[sel] = hit
        return hit

    def evaluate_plain(self, r: Rule) -> FoldResult:
        picked = [i for i, p in enumerate(self.pasts) if passes(r, p)]
        active = [self.futures[i] for i in picked if self.futures[i].n > 0]
        return _pooled(len(picked), len(active), sum(1 for f in active if f.good), active)


def folds(trades: dict[str, list[Trade]], starts: dict[str, float], *, k: int, horizon_days: float) -> list[Fold]:
    end = max(t.closed for ts in trades.values() for t in ts)
    h = horizon_days * DAY
    out: list[Fold] = []
    for i in range(k, 0, -1):
        cutoff = end - i * h
        owners, pasts, futures = [], [], []
        for owner, ts in trades.items():
            p = past_at(ts, starts.get(owner, ts[0].opened), cutoff)
            if p is None:
                continue
            owners.append(owner)
            pasts.append(p)
            futures.append(future_at(ts, cutoff, h))
        out.append(Fold(cutoff, owners, pasts, futures))
    return out


@dataclass(frozen=True)
class Scored:
    rule: Rule
    results: list[FoldResult]

    @property
    def robust(self) -> bool:
        return all(x.ok for x in self.results)

    @property
    def key(self) -> tuple[int, int, float]:
        worst_pf = min((x.pooled_pf or 0.0) for x in self.results)
        return (min(x.good for x in self.results), sum(x.good for x in self.results), worst_pf)


def run_all(fs: list[Fold], rules: list[Rule]) -> list[Scored]:
    return [Scored(r, [f.evaluate(r) for f in fs]) for r in rules]


def rank(scored: list[Scored]) -> list[Scored]:
    return sorted((s for s in scored if s.robust), key=lambda s: s.key, reverse=True)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _line(name: str, s: Scored) -> str:
    cells = " | ".join(f"{x.good}/{x.active}/{x.picked} · PF {x.pooled_pf or 0:.2f} · {x.pooled_mean:+.2f}%"
                       for x in s.results)  # fmt: skip
    return f"| {name} | {'yes' if s.robust else 'no'} | {cells} |"


def _rule_text(r: Rule) -> str:
    proof = " (+small-sample proof)" if r.small_sample_proof else ""
    parts = [f"trades >= {r.min_trades}", f"PF >= {r.min_pf:g}{proof}"]
    if r.max_dd < NO_LIMIT:
        parts.append(f"DD <= {r.max_dd:.0%}")
    if r.no_scalpers:
        parts.append("no scalpers")
    if r.max_win_share < 1.0:
        parts.append(f"best win <= {r.max_win_share:.0%} of profit")
    if r.min_win_rate > 0:
        parts.append(f"win rate >= {r.min_win_rate:.0%}")
    if r.max_idle_days < NO_LIMIT:
        parts.append(f"traded in the last {r.max_idle_days:g} d")
    if r.min_median_size > 0:
        parts.append(f"median size >= ${r.min_median_size:,.0f}")
    return ", ".join(parts)


def report(fs: list[Fold], scored: list[Scored], baselines: dict[str, Scored], db: str, top: int = 15) -> str:
    ranked = rank(scored)
    cut = " | ".join(f"fold {i + 1}: from {_day(f.cutoff)}" for i, f in enumerate(fs))
    lines = [
        "# Wallet rules: which pick the most wallets that stay good?",
        "",
        f"Data: `{db}`. {len(fs[-1].owners)} owners with history; {len(scored)} rule sets; {cut} "
        f"(each fold = {_day(fs[0].cutoff)} + the horizon). Cell = good/active/picked · pooled PF · pooled mean per "
        f"trade (fees in). GOOD = >= {FUTURE_MIN_TRADES} later trades, mean > 0, PF >= {GOOD_MIN_PF}. ROBUST = pooled "
        f"PF >= {GOOD_MIN_PF} and mean > 0 in every fold.",
        "",
        "## Today's rules",
        "",
        "| rule | robust | " + " | ".join(f"fold {i + 1}" for i in range(len(fs))) + " |",
        "|---|---|" + "---|" * len(fs),
        *(_line(name, s) for name, s in baselines.items()),
        "",
        f"## Best robust rule sets (most good wallets in the worst fold), {len(ranked)} robust of {len(scored)}",
        "",
        "| # | robust | " + " | ".join(f"fold {i + 1}" for i in range(len(fs))) + " |",
        "|---|---|" + "---|" * len(fs),
    ]
    for i, s in enumerate(ranked[:top], 1):
        lines.append(_line(str(i), s))
    lines += ["", "Rules of the table above:", ""]
    lines += [f"{i}. {_rule_text(s.rule)}" for i, s in enumerate(ranked[:top], 1)]
    lines += ["", "## What each choice is worth (the best robust rule set with that value)", ""]
    for key in GRID:
        best: dict[float, Scored] = {}
        for s in ranked:
            v = float(getattr(s.rule, key))
            if v not in best:
                best[v] = s
        cells = [f"{v:g}: {best[v].key[0]} good (worst fold)" if v in best else f"{v:g}: none robust"
                 for v in GRID[key]]  # fmt: skip
        lines.append(f"- `{key}`: " + " · ".join(cells))
    lines += [
        "",
        "Limits: the returns use the owners' own exits, not ours; the owner set is biased to wallets still trading in",
        "November 2026 (the last fold is the fairest). Decide with the shadow copies too.",
    ]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, scored: list[Scored]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        k = len(scored[0].results) if scored else 0
        head = list(asdict(scored[0].rule)) if scored else []
        cols = ("picked", "active", "good", "trades", "pf", "mean")
        w.writerow([*head, "robust", "worst_good", *(f"f{i + 1}_{c}" for i in range(k) for c in cols)])
        for s in scored:
            row: list[object] = [*asdict(s.rule).values(), int(s.robust), s.key[0]]
            for x in s.results:
                row += [x.picked, x.active, x.good, x.trades, round(x.pooled_pf or 0.0, 3), round(x.pooled_mean, 3)]
            w.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", action="append", required=True, help="a downloaded database (repeat: the fullest wins)")
    parser.add_argument("--out", default=None, help="folder for criteria.md and criteria.csv")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--horizon-days", type=float, default=10.0)
    args = parser.parse_args()
    db = max(args.db, key=episode_count)
    out = Path(args.out) if args.out else Path(db).resolve().parent.parent / "analysis" / "criteria"
    trades, starts = load(db)
    if not trades:
        raise SystemExit(f"{db}: no jup_episodes rows")
    fs = folds(trades, starts, k=args.folds, horizon_days=args.horizon_days)
    rules = list(grid())
    scored = run_all(fs, rules)
    baselines = {name: Scored(r, [f.evaluate(r) for f in fs]) for name, r in current_rules().items()}
    out.mkdir(parents=True, exist_ok=True)
    text = report(fs, scored, baselines, db)
    (out / "criteria.md").write_text(text)
    write_csv(out / "criteria.csv", scored)
    print(text)
    print(f"written: {out / 'criteria.md'}, {out / 'criteria.csv'}")


if __name__ == "__main__":
    main()
