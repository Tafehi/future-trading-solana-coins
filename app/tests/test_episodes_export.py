"""Jupiter episodes are copied to Parquet once each: new rows only, and a restart neither repeats nor skips."""

from pathlib import Path

from app.db import store
from app.record import episodes_export as ex
from app.record.recorder import Recorder

T = 1_800_000_000.0


async def _add(n: int, start: int = 0) -> None:
    async with store.session() as conn:
        for i in range(start, start + n):
            await conn.execute(
                "INSERT INTO jup_episodes (owner, coin, side, size_usd, entry_px, exit_px, opened_at, closed_at, pnl) "
                "VALUES (?, 'SOL', 'long', 5000, 100, 101, ?, ?, ?)",
                (f"own{i}", T + i, T + i + 60, float(i)),
            )


async def test_only_new_rows_are_exported_once(fresh_db: str, tmp_path: Path) -> None:
    r = Recorder(str(tmp_path))
    await _add(3)
    assert await ex.export_new(r) == 3
    assert await ex.export_new(r) == 0  # nothing new: nothing again
    await _add(1, start=3)
    assert await ex.export_new(Recorder(str(tmp_path))) == 1  # a new Recorder (a restart) resumes from meta
    assert [row["owner"] for row in r.buffers[ex.DATASET]] == ["own0", "own1", "own2"]


async def test_the_rows_land_in_the_shared_bucket_layout(fresh_db: str, tmp_path: Path) -> None:
    r = Recorder(str(tmp_path))
    await _add(2)
    await ex.export_new(r)
    files = r.flush(now=T)
    assert [str(Path(f).relative_to(tmp_path)).split("/part-")[0] for f in files] == [
        "futures/solana-bot/episodes/day=2027-01-15"
    ]
