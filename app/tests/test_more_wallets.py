"""The wallet report (D-30: the pool from the scout's file, the shadow copies from the trader's, both read-only)."""

from pathlib import Path

import pytest

from app.db import migrations


async def test_wallets_report_runs_on_two_empty_files(fresh_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    from app.tools import wallets_report

    await wallets_report.run(fresh_db, fresh_db)
    out = capsys.readouterr().out
    assert "Wallet pool" in out and "with their past read (api)" in out and "chain fallback calls today 0 /" in out
    assert "Shadow copies: 0 open" in out
    assert "trade stream" not in out  # no Hyperliquid section any more


async def test_wallets_report_never_creates_the_trader_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.tools import wallets_report

    scout = str(tmp_path / "scout.db")
    await migrations.migrate(scout)
    missing = tmp_path / "trader.db"
    await wallets_report.run(scout, str(missing))
    out = capsys.readouterr().out
    assert "trader's file is unreadable" in out and "Shadow copies: ? open" in out
    assert not missing.exists()
