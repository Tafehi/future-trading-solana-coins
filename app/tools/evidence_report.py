"""Turn downloaded databases into files an analyst can read:
`python -m app.tools.evidence_report --db TRADER_FILE [--scout-db SCOUT_FILE] --out DIR`.

Run by `scripts/collect_evidence.sh` on the operator's machine, never on the box. Opens every file READ-ONLY
(`mode=ro`: a downloaded backup is evidence and is never migrated or changed). Since D-30 the wallet pool lives in the
SCOUT's file: with `--scout-db` it is attached (read-only) as `scout`, and the pool and the shadow record are read
from it and joined with the trader's shadow copies. Without it (a backup from before D-30) everything comes from
`--db`. It writes:

- `tables/<table>.csv`  every table of `--db`, every row (positions, signals, shadow copies, ledger, ...); the
                        scout's tables as `tables/scout-<table>.csv` (wallets, jup_owners, jup_episodes, ...)
- `summary.md`          the results by wallet, source, reason, tier and UTC day; open positions; the wallet pool by
                        source and tier; each tracked/watched wallet's shadow-copy record

No network, no AWS. Every number comes from the file, so the summary can be checked against the CSVs.
"""

import argparse
import csv
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def connect_ro(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn: sqlite3.Connection, schema: str = "main") -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"  # noqa: S608 — main or scout
    )
    return [str(r["name"]) for r in rows]


def dump_tables(conn: sqlite3.Connection, out: Path, schema: str = "main", prefix: str = "") -> dict[str, int]:
    """Every table of `schema` to `out/tables/<prefix><name>.csv`. Returns rows per (prefixed) table."""
    folder = out / "tables"
    folder.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name in tables(conn, schema):
        cur = conn.execute(f'SELECT * FROM {schema}."{name}"')  # noqa: S608 — names come from sqlite_master
        cols = [d[0] for d in cur.description]
        n = 0
        with (folder / f"{prefix}{name}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for row in cur:
                w.writerow(list(row))
                n += 1
        counts[prefix + name] = n
    return counts


def _has(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    return table in tables(conn, schema)


def _pool_schema(conn: sqlite3.Connection) -> str:
    """Where the wallet pool is: the attached scout file (D-30), else the one file of an older backup."""
    attached = {str(r[1]) for r in conn.execute("PRAGMA database_list")}
    return "scout" if "scout" in attached else "main"


def _md_table(head: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        out.append("| " + " | ".join(_cell(v) for v in r) + " |")
    return out


def _cell(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


# The only grouping expressions `_group` accepts (title -> SQL). Never built from input.
GROUPS = {
    "By source": "source",
    "By exit reason": "reason",
    "By tier": "tier",
    "By UTC day (closed)": "date(closed_at, 'unixepoch')",
    "By wallet (worst first)": "source || ':' || wallet",
}


def _group(conn: sqlite3.Connection, title: str, account: str) -> list[tuple[Any, ...]]:
    """Closed positions of `account` grouped by GROUPS[title], worst net first."""
    key = GROUPS[title]
    sql = (
        f"SELECT {key} AS k, COUNT(*) AS n, SUM(net_pnl > 0) AS won, COALESCE(SUM(net_pnl), 0) AS net, "  # noqa: S608
        "COALESCE(AVG(net_pnl), 0) AS avg, COALESCE(MIN(net_pnl), 0) AS worst, COALESCE(MAX(net_pnl), 0) AS best "
        "FROM positions WHERE status = 'CLOSED' AND account = ? GROUP BY k ORDER BY net ASC"
    )
    return [tuple(r) for r in conn.execute(sql, (account,))]


def summary(conn: sqlite3.Connection, *, now: float | None = None) -> str:
    """The markdown summary. Pure apart from reading `conn`."""
    at = datetime.fromtimestamp(now, UTC) if now else datetime.now(UTC)
    lines = [f"# Evidence summary ({at:%Y-%m-%d %H:%M} UTC)", ""]
    if not _has(conn, "positions"):
        return "\n".join([*lines, "No positions table in this database."]) + "\n"
    accounts = [str(r[0]) for r in conn.execute("SELECT DISTINCT account FROM positions ORDER BY account")]
    head = ["key", "closed", "won", "net $", "avg $", "worst $", "best $"]
    for acct in accounts or ["(none)"]:
        tot = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_pnl), 0), SUM(net_pnl > 0), "
            "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN net_pnl END), 0), "
            "COALESCE(-SUM(CASE WHEN net_pnl < 0 THEN net_pnl END), 0) FROM positions "
            "WHERE status = 'CLOSED' AND account = ?",
            (acct,),
        ).fetchone()
        n, net, won, gains, losses = int(tot[0]), float(tot[1]), int(tot[2] or 0), float(tot[3]), float(tot[4])
        pf = f"{gains / losses:.2f}" if losses > 0 else "-"
        n_open = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'OPEN' AND account = ?", (acct,)
        ).fetchone()[0]
        lines += [
            f"## Account `{acct}`",
            "",
            f"{n} closed, {won} won, net ${net:+,.2f}, profit factor {pf}, {n_open} open.",
            "",
        ]
        for title in GROUPS:
            lines += [f"### {title}", "", *_md_table(head, _group(conn, title, acct)), ""]
        opened = conn.execute(
            "SELECT id, symbol, side, source, tier, wallet, entry_px, margin, last_mark, "
            "datetime(opened_at, 'unixepoch') FROM positions WHERE status = 'OPEN' AND account = ? ORDER BY id",
            (acct,),
        ).fetchall()
        lines += [
            "### Open positions",
            "",
            *_md_table(
                ["#", "symbol", "side", "source", "tier", "wallet", "entry", "margin $", "last mark", "opened UTC"],
                [tuple(r) for r in opened],
            ),
            "",
        ]
    ws = _pool_schema(conn)  # "main" or "scout", never outside input: the two f-strings below are safe
    pool_sql = (
        "SELECT source, tier, COUNT(*), COALESCE(SUM(tracked), 0), COALESCE(SUM(watched), 0) "  # noqa: S608
        f"FROM {ws}.wallets GROUP BY source, tier ORDER BY source, tier"
    )
    shadow_sql = (
        "SELECT w.source, w.tier, w.address, w.tracked, COUNT(s.id), SUM(s.ret_pct > 0), "  # noqa: S608
        f"COALESCE(AVG(s.ret_pct), 0) FROM {ws}.wallets w JOIN main.shadow_positions s ON s.wallet = w.address "
        "AND s.status = 'CLOSED' WHERE w.tracked = 1 OR w.watched = 1 GROUP BY w.address "
        "ORDER BY AVG(s.ret_pct) ASC"
    )
    if _has(conn, "wallets", ws):
        pool = conn.execute(pool_sql).fetchall()
        lines += [
            "## Wallet pool (every graded wallet)",
            "",
            *_md_table(["source", "tier", "graded", "tracked", "watched"], [tuple(r) for r in pool]),
            "",
        ]
    if _has(conn, "shadow_positions") and _has(conn, "wallets", ws):
        shadow = conn.execute(shadow_sql).fetchall()
        lines += [
            "## Shadow copies of tracked / watched wallets (closed, worst mean first)",
            "",
            *_md_table(
                ["source", "tier", "wallet", "tracked", "copies", "won", "mean ret %"], [tuple(r) for r in shadow]
            ),
            "",
        ]
    return "\n".join(lines) + "\n"


def run(db: str, out: str, *, scout_db: str | None = None, now: float | None = None) -> dict[str, int]:
    dest = Path(out)
    dest.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db)
    try:
        counts = dump_tables(conn, dest)
        if scout_db:
            conn.execute("ATTACH DATABASE ? AS scout", (f"file:{scout_db}?mode=ro",))
            counts |= dump_tables(conn, dest, "scout", "scout-")
        (dest / "summary.md").write_text(summary(conn, now=now))
    finally:
        conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="the trader's file (or a single-file backup from before D-30)")
    parser.add_argument("--scout-db", default=None, help="the scout's file (D-30): the wallet pool")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    counts = run(args.db, args.out, scout_db=args.scout_db)
    for name, n in counts.items():
        print(f"{name:20} {n:8} rows")


if __name__ == "__main__":
    main()
