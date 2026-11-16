"""Test configuration (ported from ai-kucoin-future).

Settings are read at import time by `app/config.py`, so required values are set at module scope before any `app.*`
import. Tests run on the code defaults (`SETTINGS_FILE=unset`); `test_config.py` loads the real file explicitly.

Tests run offline: `socket.socket` and `getaddrinfo` raise for internet families, so an accidental network call
fails loudly. Inject `httpx.MockTransport` instead. AF_UNIX stays allowed (asyncio needs it).

`fresh_db` gives a test its own migrated SQLite file and a seeded paper account.
"""

import json
import os
import socket
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest


def _default(name: str, value: str) -> None:
    if not os.environ.get(name):
        os.environ[name] = value


_default("SETTINGS_FILE", "unset")
_default("MODE", "paper")
_default("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="solana-future-coin-test-"), "test.db"))
_default("LOG_DIR", tempfile.mkdtemp(prefix="solana-future-coin-test-logs-"))
_default("REPORT_DIR", tempfile.mkdtemp(prefix="solana-future-coin-test-reports-"))
_default("SPOOL_DIR", tempfile.mkdtemp(prefix="solana-future-coin-test-spool-"))
_default("TELEGRAM_BOT_TOKEN", "unset")
_default("TELEGRAM_CHAT_ID", "unset")
_default("HEARTBEAT_URL", "unset")
_default("SOLANA_RPC_URL", "unset")
_default("KUCOIN_API_KEY", "unset")
_default("KUCOIN_API_SECRET", "unset")
_default("KUCOIN_API_PASSPHRASE", "unset")

_BLOCKED_FAMILIES = {socket.AF_INET, socket.AF_INET6}


class _NoNetwork(socket.socket):
    def __init__(self, family: int = socket.AF_INET, *args: object, **kwargs: object) -> None:
        if family in _BLOCKED_FAMILIES:
            raise RuntimeError("A test tried to open a network socket. Inject an httpx.MockTransport instead.")
        super().__init__(family, *args, **kwargs)  # type: ignore[arg-type]


socket.socket = _NoNetwork  # type: ignore[misc]


def _no_dns(*args: object, **kwargs: object) -> object:
    raise RuntimeError("A test tried to resolve a hostname. Tests run offline — see conftest.py.")


socket.getaddrinfo = _no_dns  # type: ignore[assignment]

FIXTURES = Path(__file__).with_name("fixtures")


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
async def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    from app.config import settings
    from app.db import migrations, store
    from app.trading import ledger

    path = str(tmp_path / "bot.db")
    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(settings, "report_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "spool_dir", str(tmp_path / "spool"))
    monkeypatch.setattr(settings, "stop_file", str(tmp_path / "STOP"))
    await migrations.migrate()
    async with store.session() as conn:
        await ledger.seed(conn, settings.account, settings.start_balance, now=1_000_000.0)
    yield path
