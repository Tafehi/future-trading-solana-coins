"""Settings: live refuses to boot without every credential; settings.toml holds no secret and loads."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_SETTINGS_FILE, SECRET_KEYS, Settings


def _live(**extra: str) -> dict[str, str]:
    return {
        "MODE": "live",
        "KUCOIN_API_KEY": "k",
        "KUCOIN_API_SECRET": "s",
        "KUCOIN_API_PASSPHRASE": "p",
        "LIVE_START_BALANCE": "500",
        "KUCOIN_KEY_DEDICATED": "true",
    } | extra


def test_live_with_everything_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _live().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.is_live and s.account == "live" and s.start_balance == 500.0


@pytest.mark.parametrize("missing", ["KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE"])
def test_live_without_a_credential_refuses(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for k, v in _live(**{missing: "unset"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match=missing):
        Settings()


def test_live_without_a_balance_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _live(LIVE_START_BALANCE="0").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="LIVE_START_BALANCE"):
        Settings()


def test_live_on_the_shared_kucoin_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """User, 2026-11-16: both futures bots share one KuCoin key; live must refuse until each has its own (B-3)."""
    for k, v in _live(KUCOIN_KEY_DEDICATED="false").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="KUCOIN_KEY_DEDICATED"):
        Settings()
    monkeypatch.delenv("KUCOIN_KEY_DEDICATED")  # the default is the shared key
    with pytest.raises(ValidationError, match="KUCOIN_KEY_DEDICATED"):
        Settings()


def test_the_golive_check_reminds_about_the_shared_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.core import golive

    gate = {g.name: g for g in golive.gates([], halted=False)}["own KuCoin key"]
    assert not gate.passed and "sub-account" in gate.detail
    monkeypatch.setattr(settings, "kucoin_key_dedicated", True)
    assert {g.name: g for g in golive.gates([], halted=False)}["own KuCoin key"].passed


def test_unknown_mode_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "real")
    with pytest.raises(ValidationError):
        Settings()


def test_paper_account_name_follows_the_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_EPOCH", "7")
    s = Settings()
    assert s.account == "paper-7" and s.start_balance == 10_000.0


def test_settings_file_has_no_secret_and_only_known_keys() -> None:
    data = tomllib.loads(Path(DEFAULT_SETTINGS_FILE).read_text())
    assert not set(data) & set(SECRET_KEYS)
    known = {f.alias for f in Settings.model_fields.values()}
    assert set(data) <= known, f"unknown keys in settings.toml: {set(data) - known}"


def test_settings_file_loads_and_is_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTINGS_FILE", str(DEFAULT_SETTINGS_FILE))
    monkeypatch.delenv("MODE", raising=False)
    s = Settings()
    assert s.mode == "paper" and s.paper_start_balance == 10_000.0 and s.leverage == 3 and s.digest_minutes == 45
    # User, 2026-11-13: the wallet list is re-graded every 5 minutes, not hourly.
    assert s.jupiter_grade_minutes == 5.0
    # Items 1, 4, 5 in the file (the 90-day Hyperliquid window left with that source, D-19).
    assert (s.copy_silver, s.silver_max_weight, s.shadow_enabled, s.jupiter_history_enabled) == (True, 3, True, True)
    # Both bots post to one Telegram chat: this one names itself.
    assert s.bot_name == "Jupiter-KuCoin"


def test_scout_uses_the_wallet_list_intervals() -> None:
    """The wallet-list job reads its cadence from settings, not a hard-coded hour (the scout grades since D-30)."""
    src = (Path(DEFAULT_SETTINGS_FILE).parent / "scout.py").read_text()
    assert 'claim("jupiter_grade", interval_s=settings.jupiter_grade_minutes * 60)' in src
