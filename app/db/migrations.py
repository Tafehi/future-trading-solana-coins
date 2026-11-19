"""Schema and migrations. `migrate()` runs once at startup and brings the file to `SCHEMA_VERSION`.

Each entry in `MIGRATIONS` runs once, in order, inside one transaction, and bumps `meta.schema_version`.
A file newer than this code (a rollback to an old image) is refused, not silently written with an older idea of
the tables.
"""

import time

import aiosqlite

from app.db import store
from app.utils.logging import get_logger

logger = get_logger(__name__)


class MigrationRefused(RuntimeError):
    pass


_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Every wallet we graded. `source` is 'hyperliquid' or 'jupiter'. Only `tracked = 1` wallets are copied.
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'hyperliquid',
    account_value REAL NOT NULL DEFAULT 0,
    month_pnl REAL NOT NULL DEFAULT 0,
    month_roi REAL NOT NULL DEFAULT 0,
    month_volume REAL NOT NULL DEFAULT 0,
    alltime_pnl REAL NOT NULL DEFAULT 0,
    style TEXT NOT NULL DEFAULT 'UNKNOWN',
    tier TEXT NOT NULL DEFAULT 'WHITE',
    trades INTEGER NOT NULL DEFAULT 0,
    win_rate REAL,
    profit_factor REAL,
    max_drawdown REAL,
    median_hold_s REAL,
    coverage REAL,
    window_pnl REAL,
    reason TEXT NOT NULL DEFAULT '',
    graded_at REAL,
    tracked INTEGER NOT NULL DEFAULT 0,
    history_days REAL,
    long_pnl REAL,
    months INTEGER,
    months_up INTEGER,
    losses_7d INTEGER,
    losses_30d INTEGER,
    score REAL,
    count_score REAL,
    real_trades INTEGER,
    loss_rate REAL,
    payoff REAL,
    losses_per_month REAL,
    copy_weight INTEGER,
    last_fill_at REAL
);
CREATE INDEX IF NOT EXISTS wallets_tracked ON wallets (tracked);

-- The last seen open positions of each tracked wallet. Diffing against it gives OPEN / CLOSE / FLIP.
CREATE TABLE IF NOT EXISTS wallet_positions (
    address TEXT NOT NULL,
    coin TEXT NOT NULL,
    size REAL NOT NULL,
    entry_px REAL NOT NULL,
    leverage REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (address, coin)
);
-- When each wallet was last polled; a first poll (or one older than BASELINE_MAX_AGE_S) is only a baseline.
CREATE TABLE IF NOT EXISTS wallet_polls (
    address TEXT PRIMARY KEY,
    polled_at REAL NOT NULL
);

-- Every OPEN / CLOSE seen on a tracked wallet, and what we did with it.
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    wallet TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'hyperliquid',
    coin TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    side TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT '',
    wallet_size REAL,
    wallet_entry_px REAL,
    wallet_leverage REAL,
    handled INTEGER NOT NULL DEFAULT 0,
    copied INTEGER NOT NULL DEFAULT 0,
    refused TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS signals_handled ON signals (handled, id);

CREATE TABLE IF NOT EXISTS account_state (
    account TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    peak_equity REAL NOT NULL DEFAULT 0,
    halted INTEGER NOT NULL DEFAULT 0,
    halt_reason TEXT NOT NULL DEFAULT '',
    paused INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    coin TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    contracts REAL NOT NULL,
    multiplier REAL NOT NULL,
    qty REAL NOT NULL,
    entry_px REAL NOT NULL,
    leverage REAL NOT NULL,
    margin REAL NOT NULL,
    mmr REAL NOT NULL DEFAULT 0,
    liq_px REAL,
    entry_fee REAL NOT NULL DEFAULT 0,
    exit_px REAL,
    exit_fee REAL NOT NULL DEFAULT 0,
    funding REAL NOT NULL DEFAULT 0,
    gross_pnl REAL,
    net_pnl REAL,
    best_px REAL NOT NULL,
    last_mark REAL,
    last_funding_ts REAL NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    reason TEXT NOT NULL DEFAULT '',
    signal_id INTEGER,
    wallet TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT '',
    copy_weight INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    entry_order_id TEXT NOT NULL DEFAULT '',
    stop_order_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS positions_open ON positions (account, status);
CREATE INDEX IF NOT EXISTS positions_closed_at ON positions (closed_at);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    position_id INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ledger_account ON ledger (account);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    account TEXT NOT NULL,
    ts REAL NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY (account, ts)
);

-- Jupiter Perps: the last snapshot of each open position account, and the closed episodes built from them.
CREATE TABLE IF NOT EXISTS jup_positions (
    pubkey TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usd REAL NOT NULL,
    entry_px REAL NOT NULL,
    open_time INTEGER NOT NULL,
    seen_at REAL NOT NULL,
    update_time INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jup_positions_owner ON jup_positions (owner);
CREATE TABLE IF NOT EXISTS jup_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usd REAL NOT NULL,
    entry_px REAL NOT NULL,
    exit_px REAL NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL NOT NULL,
    pnl REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jup_episodes_owner ON jup_episodes (owner, closed_at);
CREATE TABLE IF NOT EXISTS jup_owners (
    owner TEXT PRIMARY KEY,
    first_seen REAL NOT NULL
);
"""

# 2026-11-13, items 1-5 (more wallets): shadow copies, the watch list, the Hyperliquid trade stream, Jupiter history.
_V2 = """
ALTER TABLE wallets ADD COLUMN watched INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS wallets_watched ON wallets (watched);

-- Shadow copies: every GOLD/SILVER wallet's opens copied without money (app/trading/shadow.py). Returns are price %
-- in our favour, net of two taker fees, so they do not depend on size or leverage.
CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    source TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT '',
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_px REAL NOT NULL,
    best_px REAL NOT NULL,
    liq_px REAL,
    taker_fee REAL NOT NULL,
    opened_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    exit_px REAL,
    closed_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    ret_pct REAL
);
CREATE INDEX IF NOT EXISTS shadow_open ON shadow_positions (status, symbol);
CREATE INDEX IF NOT EXISTS shadow_wallet ON shadow_positions (wallet, closed_at);

-- Who trades on Hyperliquid, from the public trade stream (app/data/hl_stream.py).
CREATE TABLE IF NOT EXISTS hl_seen (
    address TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    trades INTEGER NOT NULL DEFAULT 0,
    volume_usd REAL NOT NULL DEFAULT 0,
    prescreened_at REAL
);
CREATE INDEX IF NOT EXISTS hl_seen_volume ON hl_seen (last_seen, volume_usd);

-- Jupiter history from the chain (app/jobs/jupiter_history.py).
ALTER TABLE jup_owners ADD COLUMN backfilled_at REAL;
ALTER TABLE jup_owners ADD COLUMN history_from REAL;
ALTER TABLE jup_owners ADD COLUMN max_open_usd REAL NOT NULL DEFAULT 0;
ALTER TABLE jup_episodes ADD COLUMN origin TEXT NOT NULL DEFAULT 'feed';
ALTER TABLE jup_episodes ADD COLUMN position_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS jup_episodes_chain ON jup_episodes (position_key, opened_at) WHERE origin = 'chain';
"""

# 2026-11-18 (D-29): an owner's past from Jupiter's trade API (origin 'api'). The owners already read from the chain
# are read once more: the chain decoder missed liquidation fees (DEBUG D-12) and every open fee, so their episodes were
# optimistic; the API read replaces them.
_V3 = """
CREATE UNIQUE INDEX IF NOT EXISTS jup_episodes_api ON jup_episodes (position_key, opened_at) WHERE origin = 'api';
UPDATE jup_owners SET backfilled_at = NULL WHERE backfilled_at IS NOT NULL;
"""

# 2026-11-18 (D-30): the trader's own view of the copy-list owners' Position accounts (websocket + safety-net poll),
# and signals that know which account and which open they came from. The unique index makes a second signal for the
# same open impossible, whatever saw it (socket, poll, a restart). Both program files share this schema; each program
# writes only its own tables.
_V4 = """
CREATE TABLE IF NOT EXISTS feed_positions (
    pubkey TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usd REAL NOT NULL,
    entry_px REAL NOT NULL,
    open_time INTEGER NOT NULL,
    update_time INTEGER NOT NULL,
    slot INTEGER NOT NULL DEFAULT 0,
    seen_at REAL NOT NULL,
    via TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feed_positions_owner ON feed_positions (owner);
ALTER TABLE signals ADD COLUMN position_key TEXT;
ALTER TABLE signals ADD COLUMN open_time INTEGER;
ALTER TABLE signals ADD COLUMN via TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS signals_once ON signals (position_key, open_time, kind)
    WHERE position_key IS NOT NULL;
"""

MIGRATIONS: list[str] = [_V1, _V2, _V3, _V4]
SCHEMA_VERSION = len(MIGRATIONS)


async def _version(conn: aiosqlite.Connection) -> int:
    await conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL)")
    async with conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur:
        row = await cur.fetchone()
    return int(row["value"]) if row else 0


async def migrate(db_path: str | None = None) -> int:
    """Bring the database to SCHEMA_VERSION. Returns the version reached."""
    async with store.session(db_path) as conn:
        current = await _version(conn)
        if current > SCHEMA_VERSION:
            raise MigrationRefused(
                f"database is at schema {current}, this code knows {SCHEMA_VERSION}. Refusing to write to it."
            )
        for i in range(current, SCHEMA_VERSION):
            await conn.executescript(MIGRATIONS[i])
            await conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (str(i + 1), time.time()),
            )
            logger.info("migrations: schema %d applied", i + 1)
    return SCHEMA_VERSION
