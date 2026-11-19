"""SQLite connection handling. `connect()` is the only way this project opens a database.

`journal_mode = WAL` is stored in the file and persists. `foreign_keys` and `busy_timeout` are
per-connection and default to off / zero, so `connect()` sets them every time. Forgetting them fails
silently: foreign keys are simply not enforced.

WAL lets the hot path read while the cold path writes grades. `busy_timeout` makes a colliding writer
wait instead of raising `database is locked`.

Two programs since D-30 (2026-11-18): the scout writes `data/scout.db`, the trader `data/solana-future-coin.db`. A
program reads the OTHER program's file only with `read_only=True` (SQLite URI `mode=ro`): such a connection can never
write, never creates a missing file (it raises instead), and sets no journal mode. Only `app/db/crossread.py` does that.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

BUSY_TIMEOUT_MS = 5_000


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


async def connect(db_path: str | None = None, *, read_only: bool = False) -> aiosqlite.Connection:
    """Open a connection with this project's pragmas applied. The only way to open the database."""
    path = db_path or settings.db_path
    if read_only:
        # mode=ro: no write, and a missing file is an error (it is never created by a reader).
        conn = await aiosqlite.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn
    _ensure_parent_dir(path)

    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row

    # Persistent — written into the file, so this is a no-op on every connection after the first.
    await conn.execute("PRAGMA journal_mode = WAL")
    # Per-connection, and both default to "off", so they must be set on every connection.
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@asynccontextmanager
async def session(db_path: str | None = None, *, read_only: bool = False) -> AsyncIterator[aiosqlite.Connection]:
    """Connection as a context manager. Commits on clean exit, rolls back on an exception.

    A batch that dies halfway must not leave partial evidence that the next run reads as complete. A read-only
    session never commits (there is nothing to commit; it only closes).
    """
    conn = await connect(db_path, read_only=read_only)
    try:
        yield conn
        if not read_only:
            await conn.commit()
    except BaseException:
        if not read_only:
            await conn.rollback()
        raise
    finally:
        await conn.close()
