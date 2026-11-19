"""The recorder: Parquet files land complete in the shared-bucket layout; a failing spool keeps rows, bounded."""

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.data.jupiter_perps import JupPosition
from app.record import recorder as rec

T = 1_800_000_000.0  # 2027-01-15


def test_flush_writes_the_shared_bucket_layout(tmp_path: Path) -> None:
    r = rec.Recorder(str(tmp_path))
    r.add_jupiter(
        [
            JupPosition("pk", "own", "SOL", "long", 5000.0, 100.0, 1, 2),
            JupPosition("p2", "o2", "BTC", "short", 9e3, 1, 1, 2),
        ],
        now=T,
    )
    r.add_jupiter([JupPosition("dust", "own", "SOL", "long", 5.0, 100.0, 1, 2)], now=T)  # below JUPITER_MIN_SIZE_USD
    r.add_rows("solana-bot/grades", [{"address": "a", "tier": "GOLD"}])
    files = r.flush(now=T)
    rel = sorted(str(Path(f).relative_to(tmp_path)).split("/part-")[0] for f in files)
    assert rel == [
        "futures/jupiter/positions/day=2027-01-15",
        "futures/solana-bot/grades/day=2027-01-15",
    ]
    assert not list(tmp_path.rglob("*.tmp"))  # only complete files are left for the upload timer
    table = pq.read_table([f for f in files if "jupiter" in f][0])
    assert table.num_rows == 2 and "owner" in table.column_names and "size_usd" in table.column_names
    assert r.buffers["jupiter/positions"] == []


def test_programs_record_into_the_solana_bot_folders_only() -> None:
    """D-19: this bot's rows never land in the KuCoin bot's folders, and it records no KuCoin market data. Since D-30
    the scout records the grades, the trader its positions and shadow copies."""
    src = "".join((Path(__file__).parents[1] / f).read_text() for f in ("scout.py", "trader.py"))
    for dataset in ("solana-bot/grades", "solana-bot/positions", "solana-bot/shadow"):
        assert f'add_rows("{dataset}"' in src
    for foreign in ('add_rows("grades"', 'add_rows("bot/', "add_contracts", "add_book"):
        assert foreign not in src


def test_a_failing_spool_keeps_rows_but_never_more_than_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "spool"
    blocker.write_text("a file where the folder should be")  # makedirs fails
    monkeypatch.setattr(rec, "MAX_BUFFER_ROWS", 5)
    r = rec.Recorder(str(blocker))
    r.add_rows("solana-bot/positions", [{"i": i} for i in range(8)])
    assert r.flush(now=T) == []
    assert [row["i"] for row in r.buffers["solana-bot/positions"]] == [3, 4, 5, 6, 7]  # the newest 5 kept
