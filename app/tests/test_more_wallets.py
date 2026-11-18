"""The wallet report runs on an empty database (the Hyperliquid stream tests left with that source, D-19)."""

import pytest


async def test_wallets_report_runs_on_an_empty_database(fresh_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    from app.tools import wallets_report

    await wallets_report.run(None)
    out = capsys.readouterr().out
    assert "Wallet pool" in out and "with their past read (api)" in out and "chain fallback calls today 0 /" in out
    assert "trade stream" not in out  # no Hyperliquid section any more
