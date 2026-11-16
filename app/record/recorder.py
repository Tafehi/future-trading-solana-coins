"""Record futures data that cannot be fetched later, as Parquet, for the shared bucket (plan: "record now").

Every RECORD_SECONDS rows are added to in-memory buffers; every FLUSH_SECONDS each buffer is written to
`SPOOL_DIR/<dataset>/day=YYYY-MM-DD/part-HHMM-<ms>.parquet` (first as `.tmp`, then renamed, so the host timer
`solana-future-coin-spool` never moves a half-written file). The host timer moves the spool to
`s3://bigdata-shared-history-<account>/futures/...`, the same layout as the Solana data.

Datasets (all with `ts_ms` = our clock). Since the split (2026-11-14, D-19) this bot records only what is its own;
the KuCoin market data (`futures/kucoin/contracts`, `futures/kucoin/book_top`) is recorded by the KuCoin bot
(future-trading-kucoins), and recording it twice would double the files for nothing:
- futures/jupiter/positions     every open Jupiter Perps position >= JUPITER_MIN_SIZE_USD at each snapshot
- futures/solana-bot/grades     the daily point-in-time Jupiter wallet list (tier, score, weight, tracked) —
                                backtests use the list as it was on each day, never today's (no look-ahead)
- futures/solana-bot/positions  our own closed positions, for comparing paper with live later
- futures/solana-bot/shadow     our closed shadow copies
The `solana-bot/` folder keeps these apart from the KuCoin bot's `grades` and `bot/` folders under the same prefix.
"""

import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.config import settings
from app.data.jupiter_perps import JupPosition
from app.utils.logging import get_logger

logger = get_logger(__name__)

FLUSH_SECONDS = 300.0
# Per dataset, while the spool cannot be written (~1 day of contracts rows at 60 s).
MAX_BUFFER_ROWS = 1_000_000
PREFIX = "futures"


class Recorder:
    def __init__(self, spool_dir: str | None = None) -> None:
        self.spool = spool_dir or settings.spool_dir
        self.buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.last_flush = time.time()

    def add_jupiter(self, positions: list[JupPosition], *, now: float) -> None:
        ts = int(now * 1000)
        for p in positions:
            if p.size_usd < settings.jupiter_min_size_usd:
                continue
            self.buffers["jupiter/positions"].append(
                {"ts_ms": ts, "pubkey": p.pubkey, "owner": p.owner, "coin": p.coin, "side": p.side,
                 "size_usd": p.size_usd, "entry_px": p.entry_px, "open_time": p.open_time}
            )  # fmt: skip

    def add_rows(self, dataset: str, rows: list[dict[str, Any]]) -> None:
        self.buffers[dataset].extend(rows)

    def due(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.last_flush >= FLUSH_SECONDS

    def flush(self, *, now: float | None = None) -> list[str]:
        """Write every non-empty buffer to the spool. Returns the files written."""
        at = time.time() if now is None else now
        dt = datetime.fromtimestamp(at, UTC)
        written: list[str] = []
        for dataset, rows in list(self.buffers.items()):
            if not rows:
                continue
            folder = os.path.join(self.spool, PREFIX, dataset, f"day={dt:%Y-%m-%d}")
            path = os.path.join(folder, f"part-{dt:%H%M}-{int(at * 1000)}.parquet")
            try:
                os.makedirs(folder, exist_ok=True)
                pq.write_table(pa.Table.from_pylist(rows), path + ".tmp", compression="zstd")
                os.replace(path + ".tmp", path)
            except (OSError, pa.ArrowException) as exc:
                if len(rows) > MAX_BUFFER_ROWS:
                    # A spool that keeps failing (full disk) must not take the bot's memory with it: keep the newest.
                    del rows[: len(rows) - MAX_BUFFER_ROWS]
                logger.error("recorder: writing %s failed, keeping %d rows: %s", dataset, len(rows), exc)
                continue
            written.append(path)
            self.buffers[dataset] = []
        self.last_flush = at
        return written
