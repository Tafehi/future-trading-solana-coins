"""Jupiter wallet history as Parquet (user, 2026-11-16: "record wallet history").

Every closed Jupiter episode the bot records or reads from the chain lives in the `jup_episodes` table; that table is
what grading uses. This copies the NEW rows (by id, so chain backfills of old trades are exported when they arrive) to
the recorder at each flush, as `futures/solana-bot/episodes` in the shared bucket. The last exported id is kept in
`meta`, so a restart neither repeats nor skips rows.
"""

from app.db import meta, store
from app.record.recorder import Recorder

DATASET = "solana-bot/episodes"
KEY = "rec:jup_episodes_last_id"
MAX_ROWS_PER_PASS = 50_000  # a big chain backfill is exported over several flushes, not in one huge file


async def export_new(recorder: Recorder) -> int:
    """Queue the jup_episodes rows added since the last export. Returns how many."""
    async with store.session() as conn:
        got = await meta.get(conn, KEY)
        last = int(got[0]) if got else 0
        async with conn.execute(
            "SELECT * FROM jup_episodes WHERE id > ? ORDER BY id LIMIT ?", (last, MAX_ROWS_PER_PASS)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            recorder.add_rows(DATASET, rows)
            await meta.put(conn, KEY, max(int(r["id"]) for r in rows))
    return len(rows)
