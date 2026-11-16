"""SQLite connection handling. `connect()` is the only way this project opens a database.

`journal_mode = WAL` is stored in the file and persists. `foreign_keys` and `busy_timeout` are
per-connection and default to off / zero, so `connect()` sets them every time. Forgetting them fails
silently: foreign keys are simply not enforced.

WAL lets the hot path read while the cold path writes grades. `busy_timeout` makes a colliding writer
wait instead of raising `database is locked`.
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


async def connect(db_path: str | None = None) -> aiosqlite.Connection:
    """Open a connection with this project's pragmas applied. The only way to open the database."""
    path = db_path or settings.db_path
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
async def session(db_path: str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """Connection as a context manager. Commits on clean exit, rolls back on an exception.

    A batch that dies halfway must not leave partial evidence that the next run reads as complete.
    """
    conn = await connect(db_path)
    try:
        yield conn
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()
