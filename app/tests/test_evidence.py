"""The offline evidence report (`app/tools/evidence_report.py`) that scripts/collect_evidence.sh runs on a download.

It must open the database read-only, write every table as CSV, and sum the results the same way the ledger does.
"""

import csv
import sqlite3
from pathlib import Path

import pytest

from app.tools import evidence_report as er

T = 1_800_000_000.0


def _db(path: Path) -> str:
    db = str(path / "e.db")
    c = sqlite3.connect(db)
    c.executescript(
        """
        CREATE TABLE positions (id INTEGER PRIMARY KEY, account TEXT, symbol TEXT, side TEXT, source TEXT, tier TEXT,
            wallet TEXT, entry_px REAL, margin REAL, last_mark REAL, net_pnl REAL, reason TEXT, status TEXT,
            opened_at REAL, closed_at REAL);
        CREATE TABLE wallets (address TEXT PRIMARY KEY, source TEXT, tier TEXT, tracked INTEGER, watched INTEGER);
        CREATE TABLE shadow_positions (id INTEGER PRIMARY KEY, wallet TEXT, status TEXT, ret_pct REAL);
        """
    )
    rows = [
        (1, "paper-1", "ENAUSDTM", "long", "hyperliquid", "SILVER", "0xa", 1.0, 100.0, None, -18.41, "STOP_LOSS"),
        (2, "paper-1", "AVAXUSDTM", "long", "hyperliquid", "SILVER", "0xa", 1.0, 100.0, None, -20.05, "STOP_LOSS"),
        (3, "paper-1", "XMRUSDTM", "short", "hyperliquid", "SILVER", "0xb", 1.0, 100.0, None, 28.80, "TRAIL"),
    ]
    for r in rows:
        c.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'CLOSED',?,?)", (*r, T, T + 3600))
    c.execute(
        "INSERT INTO positions VALUES (4,'paper-1','SOLUSDTM','long','hyperliquid','GOLD','0xc',70,280,71,"
        "NULL,'','OPEN',?,NULL)",
        (T,),
    )
    c.executemany(
        "INSERT INTO wallets VALUES (?,?,?,?,?)",
        [
            ("0xa", "hyperliquid", "SILVER", 1, 0),
            ("0xb", "hyperliquid", "SILVER", 1, 0),
            ("0xz", "hyperliquid", "BLACK", 0, 0),
        ],
    )
    c.executemany(
        "INSERT INTO shadow_positions VALUES (?,?,?,?)", [(1, "0xa", "CLOSED", -6.0), (2, "0xa", "CLOSED", 2.0)]
    )
    c.commit()
    c.close()
    return db


def test_every_table_lands_as_csv_and_the_summary_adds_up(tmp_path: Path) -> None:
    db = _db(tmp_path)
    counts = er.run(db, str(tmp_path / "out"), now=T)
    assert counts == {"positions": 4, "shadow_positions": 2, "wallets": 3}
    with (tmp_path / "out" / "tables" / "wallets.csv").open() as f:
        assert [r["address"] for r in csv.DictReader(f)] == ["0xa", "0xb", "0xz"]
    md = (tmp_path / "out" / "summary.md").read_text()
    assert "3 closed, 1 won, net $-9.66, profit factor 0.75, 1 open." in md  # 28.80 / (18.41 + 20.05)
    assert "| STOP_LOSS | 2 | 2 | -38.46 |" not in md  # won counts only winners
    assert "| STOP_LOSS | 2 | 0 | -38.46 |" in md
    assert "| hyperliquid:0xa | 2 | 0 | -38.46 |" in md  # the worst wallet first
    assert "| hyperliquid | SILVER | 0xa | 1 | 2 | 1 | -2.00 |" in md  # its shadow record


def test_the_database_is_opened_read_only(tmp_path: Path) -> None:
    db = _db(tmp_path)
    conn = er.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM positions")
    conn.close()


def test_two_files_the_pool_comes_from_the_scout(tmp_path: Path) -> None:
    """D-30: the trader's file keeps an old `wallets` table that nobody updates; with --scout-db the pool and the
    shadow record use the scout's wallets, and the scout's tables land as scout-<table>.csv."""
    trader = _db(tmp_path)
    c = sqlite3.connect(trader)
    c.execute("UPDATE wallets SET tracked = 0")  # the stale copy in the trader's file
    c.commit()
    c.close()
    scout = str(tmp_path / "scout.db")
    c = sqlite3.connect(scout)
    c.executescript(
        "CREATE TABLE wallets (address TEXT PRIMARY KEY, source TEXT, tier TEXT, tracked INTEGER, watched INTEGER);"
        "INSERT INTO wallets VALUES ('0xa', 'jupiter', 'GOLD', 1, 0);"
    )
    c.commit()
    c.close()
    counts = er.run(trader, str(tmp_path / "out"), scout_db=scout, now=T)
    assert counts["scout-wallets"] == 1 and counts["wallets"] == 3
    md = (tmp_path / "out" / "summary.md").read_text()
    assert "| jupiter | GOLD | 1 | 1 | 0 |" in md  # the scout's pool, not the stale one
    assert "| jupiter | GOLD | 0xa | 1 | 2 | 1 | -2.00 |" in md  # joined with the trader's shadow copies
    with pytest.raises(sqlite3.OperationalError):
        conn = er.connect_ro(trader)
        conn.execute("ATTACH DATABASE ? AS scout", (f"file:{scout}?mode=ro",))
        conn.execute("DELETE FROM scout.wallets")  # attached read-only too
