"""Configuration singleton.

Three layers, highest priority first:

1. Environment variables. On the instance they come from the hand-made Secrets Manager secret `solana-future-bot`
   (rendered into `/opt/solana-future-coin/.env` by the `solana-future-coin-env` host timer). Secrets live ONLY
   here. The secret is shared with the KuCoin bot (future-trading-kucoins) for now (user, 2026-11-14; BUG B-3).
2. `app/settings.toml` — every non-secret setting. `SETTINGS_FILE` points elsewhere; `unset` or blank disables
   it (the tests do this, so they run on the code defaults).
3. The defaults in this file.

The model validators at the bottom refuse to boot on a bad combination. The most important one:
`MODE=live` without every KuCoin credential and a starting balance exits non-zero before any loop starts.
"""

import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

UNSET = "unset"
PAPER = "paper"
LIVE = "live"

DEFAULT_SETTINGS_FILE = Path(__file__).with_name("settings.toml")

# Keys that must never appear in settings.toml (a test enforces it).
SECRET_KEYS = (
    "KUCOIN_API_KEY",
    "KUCOIN_API_SECRET",
    "KUCOIN_API_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SOLANA_RPC_URL",
    "SOLANA_WS_URL",
    "HEARTBEAT_URL",
)


def settings_file() -> Path | None:
    """The TOML file to layer under the environment, or `None` for code defaults only."""
    raw = _blank_if_unset(os.environ.get("SETTINGS_FILE", str(DEFAULT_SETTINGS_FILE)))
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _blank_if_unset(v: str) -> str:
    raw = (v or "").strip()
    return "" if raw.lower() == UNSET else raw


class Settings(BaseSettings):
    # ── Environment ───────────────────────────────────────────────────────────────────────
    aws_region: str = Field(default="eu-central-1", alias="AWS_REGION")
    # The database the RUNNING program writes. Since D-30 each program sets it to its own file at start
    # (`runtime.use_db`): the scout -> SCOUT_DB_PATH, the trader -> TRADER_DB_PATH. Tools default to the trader's.
    db_path: str = Field(default="data/solana-future-coin.db", alias="DB_PATH")
    scout_db_path: str = Field(default="data/scout.db", alias="SCOUT_DB_PATH")
    trader_db_path: str = Field(default="data/solana-future-coin.db", alias="TRADER_DB_PATH")
    report_dir: str = Field(default="data/reports", alias="REPORT_DIR")
    # Parquet files the recorder writes; the host timer `solana-future-coin-spool` moves them to the shared bucket.
    spool_dir: str = Field(default="data/spool", alias="SPOOL_DIR")
    # The kill switch: when this file exists, every position is closed and the process exits.
    stop_file: str = Field(default="data/STOP", alias="STOP_FILE")

    # ── Mode and money ────────────────────────────────────────────────────────────────────
    # `paper` (default) or `live`. Paper uses the same code path as live except the executor (DECISIONS D-3).
    mode: str = Field(default=PAPER, alias="MODE")
    # The paper account's one deposit (user, 2026-11-13: $10,000).
    paper_start_balance: float = Field(default=10_000.0, gt=0.0, alias="PAPER_START_BALANCE")
    # Live: the USDT the user put in the KuCoin futures account. Boot refuses if the real balance is below it.
    live_start_balance: float = Field(default=0.0, ge=0.0, alias="LIVE_START_BALANCE")
    # Changing this starts a fresh paper account on the next boot (old rows kept under the old account name).
    paper_epoch: str = Field(default="1", alias="PAPER_EPOCH")

    # ── Sizing and limits ─────────────────────────────────────────────────────────────────
    leverage: int = Field(default=3, ge=1, le=10, alias="LEVERAGE")
    # Margin per trade = equity x this x size_share(weight). Notional = margin x LEVERAGE.
    position_fraction: float = Field(default=0.06, gt=0.0, le=0.5, alias="POSITION_FRACTION")
    max_open_positions: int = Field(default=10, ge=1, alias="MAX_OPEN_POSITIONS")
    min_order_usd: float = Field(default=10.0, gt=0.0, alias="MIN_ORDER_USD")
    # A new signal may take the slot of the lowest-score open copy when its wallet scores this much higher.
    replace_min_score_gap: float = Field(default=1.0, gt=0.0, alias="REPLACE_MIN_SCORE_GAP")

    # ── Market gate ───────────────────────────────────────────────────────────────────────
    max_spread_bps: float = Field(default=15.0, gt=0.0, alias="MAX_SPREAD_BPS")
    max_slippage_bps: float = Field(default=30.0, gt=0.0, alias="MAX_SLIPPAGE_BPS")
    max_entry_drift_pct: float = Field(default=1.5, gt=0.0, alias="MAX_ENTRY_DRIFT_PCT")
    min_turnover_usd: float = Field(default=500_000.0, ge=0.0, alias="MIN_TURNOVER_USD")
    max_adverse_funding: float = Field(default=0.001, gt=0.0, alias="MAX_ADVERSE_FUNDING")
    # Paper: seconds between the decision and reading the book we fill against (an order's round trip).
    paper_fill_delay_s: float = Field(default=1.0, ge=0.0, le=30.0, alias="PAPER_FILL_DELAY_S")

    # ── Exits (price moves; leverage only scales the result) ──────────────────────────────
    stop_loss_price_pct: float = Field(default=6.0, gt=0.0, alias="STOP_LOSS_PRICE_PCT")
    trail_activate_price_pct: float = Field(default=6.0, gt=0.0, alias="TRAIL_ACTIVATE_PRICE_PCT")
    trail_giveback_price_pct: float = Field(default=3.0, gt=0.0, alias="TRAIL_GIVEBACK_PRICE_PCT")
    max_hold_hours: float = Field(default=72.0, gt=0.0, alias="MAX_HOLD_HOURS")

    # ── Signals and the wallet list ───────────────────────────────────────────────────────
    signal_max_age_s: float = Field(default=60.0, gt=0.0, alias="SIGNAL_MAX_AGE_S")
    # How often Jupiter owners are re-graded and the Jupiter list re-picked. User, 2026-11-13: every 5 min (was 1 h).
    # Cost: a few SQL queries.
    jupiter_grade_minutes: float = Field(default=5.0, ge=1.0, le=1440.0, alias="JUPITER_GRADE_MINUTES")

    # ── Grading (`app/core/wallet_grader.py`, shared rules; the Jupiter mapping is in `app/jobs/jupiter.py`) ──
    # The Hyperliquid-only discovery settings left with the Hyperliquid source (split 2026-11-14, D-19).
    # `min_account_value_usd` stays: `wallet_grader.live_check` (shared code) reads it.
    min_account_value_usd: float = Field(default=25_000.0, ge=0.0, alias="MIN_ACCOUNT_VALUE_USD")
    track_max_grade_age_hours: float = Field(default=72.0, gt=0.0, alias="TRACK_MAX_GRADE_AGE_HOURS")
    scalper_max_median_hold_min: float = Field(default=30.0, gt=0.0, alias="SCALPER_MAX_MEDIAN_HOLD_MIN")
    copy_fast_traders: bool = Field(default=False, alias="COPY_FAST_TRADERS")
    score_window_days: float = Field(default=30.0, gt=0.0, alias="SCORE_WINDOW_DAYS")
    score_min_trades: int = Field(default=5, ge=1, alias="SCORE_MIN_TRADES")
    position_min_median_hold_hours: float = Field(default=72.0, gt=0.0, alias="POSITION_MIN_MEDIAN_HOLD_HOURS")
    mm_min_turnover: float = Field(default=200.0, gt=0.0, alias="MM_MIN_TURNOVER")
    mm_max_abs_roi: float = Field(default=0.02, ge=0.0, alias="MM_MAX_ABS_ROI")
    min_kucoin_coverage: float = Field(default=0.5, ge=0.0, le=1.0, alias="MIN_KUCOIN_COVERAGE")
    gold_min_trades: int = Field(default=15, ge=1, alias="GOLD_MIN_TRADES")
    gold_min_profit_factor: float = Field(default=1.8, gt=0.0, alias="GOLD_MIN_PROFIT_FACTOR")
    gold_max_drawdown: float = Field(default=0.35, gt=0.0, le=1.0, alias="GOLD_MAX_DRAWDOWN")
    silver_min_trades: int = Field(default=10, ge=1, alias="SILVER_MIN_TRADES")
    silver_min_profit_factor: float = Field(default=1.4, gt=0.0, alias="SILVER_MIN_PROFIT_FACTOR")
    silver_max_drawdown: float = Field(default=0.50, gt=0.0, le=1.0, alias="SILVER_MAX_DRAWDOWN")
    max_unrealized_loss_share: float = Field(default=0.05, gt=0.0, le=1.0, alias="MAX_UNREALIZED_LOSS_SHARE")
    # D-31 (2026-11-19, the wallet-rule backtest): an owner whose biggest single win is more than half of its total
    # profit is BRONZE (a lucky trade, not a method). Was 1.0 (off): half of the old pool depended on one trade.
    max_biggest_win_share: float = Field(default=0.5, gt=0.0, le=1.0, alias="MAX_BIGGEST_WIN_SHARE")
    max_inactive_days: float = Field(default=5.0, gt=0.0, alias="MAX_INACTIVE_DAYS")
    max_wallet_leverage: float = Field(default=15.0, gt=0.0, alias="MAX_WALLET_LEVERAGE")
    scratch_loss_share: float = Field(default=0.005, ge=0.0, le=0.1, alias="SCRATCH_LOSS_SHARE")
    gold_min_history_days: float = Field(default=60.0, ge=0.0, alias="GOLD_MIN_HISTORY_DAYS")
    gold_min_months_up_share: float = Field(default=0.55, ge=0.0, le=1.0, alias="GOLD_MIN_MONTHS_UP_SHARE")
    probe_min_usd: float = Field(default=1000.0, ge=0.0, alias="PROBE_MIN_USD")
    probe_min_account_share: float = Field(default=0.01, ge=0.0, le=1.0, alias="PROBE_MIN_ACCOUNT_SHARE")
    probe_exempt_weight: int = Field(default=9, ge=1, le=11, alias="PROBE_EXEMPT_WEIGHT")
    weight_window_days: float = Field(default=70.0, ge=7.0, alias="WEIGHT_WINDOW_DAYS")
    weight_worst_losses_per_month: float = Field(default=3.0, gt=0.0, alias="WEIGHT_WORST_LOSSES_PER_MONTH")

    # ── SILVER wallets at a small size (user, 2026-11-13, item 5) ───────────────────────────
    copy_silver: bool = Field(default=True, alias="COPY_SILVER")
    # A SILVER wallet's copy weight is capped here: 3 = at most 17% of a full trade.
    silver_max_weight: int = Field(default=3, ge=1, le=10, alias="SILVER_MAX_WEIGHT")
    # At most this many SILVER wallets per source on the tracked list (GOLD and proven wallets come first).
    max_tracked_silver: int = Field(default=10, ge=0, alias="MAX_TRACKED_SILVER")

    # ── Shadow copies (user, 2026-11-13, item 4; the idea of Pump-fun-S3 D-126) ─────────────
    # Every GOLD/SILVER wallet's opens are paper-copied WITHOUT money: our real KuCoin entry (best bid/ask + taker fee
    # after the same market gate), our exits on KuCoin mark prices. A wallet whose shadow copies lose is not tracked;
    # one whose shadow copies win ranks first. `app/trading/shadow.py`, `app/core/selection.py`.
    shadow_enabled: bool = Field(default=True, alias="SHADOW_ENABLED")
    # Watched Jupiter wallets need no cap: the change feed sees every owner for free.
    shadow_window_days: float = Field(default=14.0, gt=0.0, alias="SHADOW_WINDOW_DAYS")
    # Fewer closed shadow copies than this in the window = no verdict yet; the grade alone decides.
    shadow_min_trades: int = Field(default=5, ge=1, alias="SHADOW_MIN_TRADES")
    # Mean net return per shadow copy (price %, fees in) at or below this = "copying it loses": not tracked.
    shadow_min_mean_return_pct: float = Field(default=0.0, alias="SHADOW_MIN_MEAN_RETURN_PCT")

    # ── Jupiter Perps on Solana (the only signal source; nothing is traded on Solana, D-4) ──
    jupiter_enabled: bool = Field(default=True, alias="JUPITER_ENABLED")
    solana_rpc_url: str = Field(default="", alias="SOLANA_RPC_URL")
    jupiter_program_id: str = Field(default="PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu", alias="JUPITER_PROGRAM_ID")
    jupiter_poll_seconds: float = Field(default=60.0, ge=15.0, alias="JUPITER_POLL_SECONDS")
    # Positions smaller than this are ignored (dust, tests, probes).
    jupiter_min_size_usd: float = Field(default=1_000.0, ge=0.0, alias="JUPITER_MIN_SIZE_USD")
    # A Jupiter wallet is graded only after this much record and this many closed episodes. Since 2026-11-18 (D-29)
    # every owner's record comes from Jupiter's trade API (90 days on its first read), so the day count rarely waits;
    # was 14 days and 15 episodes. Below JUPITER_FULL_PROOF_EPISODES the needed PF grows (jobs/jupiter.proof).
    jupiter_min_record_days: float = Field(default=1.0, ge=1.0, alias="JUPITER_MIN_RECORD_DAYS")
    jupiter_min_episodes: int = Field(default=8, ge=1, alias="JUPITER_MIN_EPISODES")
    jupiter_full_proof_episodes: int = Field(default=15, ge=1, alias="JUPITER_FULL_PROOF_EPISODES")
    jupiter_small_sample_max_weight: int = Field(default=3, ge=1, le=10, alias="JUPITER_SMALL_SAMPLE_MAX_WEIGHT")
    # D-31 (2026-11-19, `tools/criteria_backtest.py`): at least this share of an owner's trades must win, or it is
    # BRONZE (0 = off); and an owner whose last closed trade is older than JUPITER_MAX_IDLE_DAYS is not picked
    # (neither tracked nor watched): it is not trading now. With MAX_BIGGEST_WIN_SHARE 0.5 these made the pooled
    # profit factor of the picked wallets >= 1.3 in every backtest window (today's pool: 0.98 in one).
    jupiter_min_win_rate: float = Field(default=0.4, ge=0.0, le=1.0, alias="JUPITER_MIN_WIN_RATE")
    jupiter_max_idle_days: float = Field(default=14.0, gt=0.0, alias="JUPITER_MAX_IDLE_DAYS")
    # Re-grade at least this often, so a slow wallet's grade never expires (TRACK_MAX_GRADE_AGE_HOURS) unseen.
    jupiter_regrade_hours: float = Field(default=24.0, gt=0.0, alias="JUPITER_REGRADE_HOURS")
    jupiter_max_tracked: int = Field(default=20, ge=0, alias="JUPITER_MAX_TRACKED")
    # Jupiter history from the chain (user, 2026-11-13, item 1): each candidate owner's closes over the last
    # JUPITER_HISTORY_DAYS are read from its Position accounts' transactions (the close event carries the real PnL
    # and fees, checked against mainnet 2026-11-13), so it can be graded on day one instead of after 14 days of
    # recording. Uses Chainstack calls shared with Pump-fun-S1/S3: hard daily cap below.
    jupiter_history_enabled: bool = Field(default=True, alias="JUPITER_HISTORY_ENABLED")
    # Where an owner's past comes from (D-29): "api" = Jupiter's public trade API (data/jupiter_api.py, one call per
    # 1,000 trades, free; the chain read is the fallback when it fails), "chain" = Chainstack only (as before).
    jupiter_history_source: str = Field(default="api", alias="JUPITER_HISTORY_SOURCE")
    jupiter_api_rps: float = Field(default=2.0, gt=0.0, le=10.0, alias="JUPITER_API_RPS")
    jupiter_history_owners_per_pass: int = Field(default=80, ge=1, alias="JUPITER_HISTORY_OWNERS_PER_PASS")
    jupiter_history_days: float = Field(default=90.0, ge=1.0, alias="JUPITER_HISTORY_DAYS")
    jupiter_history_calls_per_day: int = Field(default=20_000, ge=0, alias="JUPITER_HISTORY_CALLS_PER_DAY")
    jupiter_history_rps: float = Field(default=2.0, gt=0.0, le=20.0, alias="JUPITER_HISTORY_RPS")
    # Owners holding at least this much open (USD) get their past read, biggest first. 1,000 = every recorded owner
    # (the feed's own minimum); was 10,000 while every read cost ~340 Chainstack calls (D-29).
    jupiter_history_min_size_usd: float = Field(default=1_000.0, ge=0.0, alias="JUPITER_HISTORY_MIN_SIZE_USD")
    # Newest transactions per Position account; a very busy owner is graded on its most recent part.
    jupiter_history_max_tx_per_position: int = Field(default=300, ge=10, alias="JUPITER_HISTORY_MAX_TX_PER_POSITION")

    # ── KuCoin ────────────────────────────────────────────────────────────────────────────
    kucoin_futures_url: str = Field(default="https://api-futures.kucoin.com", alias="KUCOIN_FUTURES_URL")
    kucoin_public_rps: float = Field(default=8.0, gt=0.0, alias="KUCOIN_PUBLIC_RPS")
    kucoin_api_key: str = Field(default="", alias="KUCOIN_API_KEY")
    kucoin_api_secret: str = Field(default="", alias="KUCOIN_API_SECRET")
    kucoin_api_passphrase: str = Field(default="", alias="KUCOIN_API_PASSPHRASE")
    kucoin_key_version: str = Field(default="3", alias="KUCOIN_KEY_VERSION")
    # GO-LIVE BLOCKER (user, 2026-11-16: "as soon as we have decided for the real money remind me ... I will add
    # another api key for solana"). Today this bot and the KuCoin bot (future-trading-kucoins) hold the SAME KuCoin
    # account key (each in its own secret since 2026-11-16: solana-future-bot / kucoin-future-bot). Each bot's live
    # pre-flight would refuse the other's positions, live_reconcile would flag them, and the key is IP-locked to the
    # other box. Set this true ONLY when this bot has its own KuCoin sub-account and key, locked to this box's IP.
    # MODE=live refuses to boot until then (BUG B-3), and the go-live check shows it as a failed gate.
    kucoin_key_dedicated: bool = Field(default=False, alias="KUCOIN_KEY_DEDICATED")
    http_timeout_s: float = Field(default=15.0, gt=0.0, alias="HTTP_TIMEOUT_S")

    # ── Recorder (futures data that cannot be fetched later) ─────────────────────────────
    record_enabled: bool = Field(default=True, alias="RECORD_ENABLED")
    record_seconds: float = Field(default=60.0, ge=15.0, alias="RECORD_SECONDS")

    # ── Telegram and heartbeat ────────────────────────────────────────────────────────────
    # Both bots post to the same chat (split 2026-11-14): every message carries this name after [PAPER]/[LIVE].
    bot_name: str = Field(default="Jupiter-KuCoin", min_length=1, alias="BOT_NAME")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    heartbeat_url: str = Field(default="", alias="HEARTBEAT_URL")
    # The Telegram report window. User, 2026-11-16: every 45 min, ALWAYS (was 15 min, only when something traded): a
    # window with no trade still sends the account and the open positions. Windows are aligned to UTC midnight, so the
    # value must divide a day (checked below).
    digest_minutes: int = Field(default=45, ge=1, le=240, alias="DIGEST_MINUTES")
    # true: every window sends a report; false: only windows with a trade (the old behaviour).
    digest_always: bool = Field(default=True, alias="DIGEST_ALWAYS")

    # ── Go-live gates (`app/core/golive.py`) ─────────────────────────────────────────────
    golive_min_trades: int = Field(default=30, ge=1, alias="GOLIVE_MIN_TRADES")
    golive_min_days: int = Field(default=7, ge=1, alias="GOLIVE_MIN_DAYS")
    golive_min_wallets: int = Field(default=3, ge=1, alias="GOLIVE_MIN_WALLETS")
    golive_min_profit_factor: float = Field(default=1.3, gt=0.0, alias="GOLIVE_MIN_PROFIT_FACTOR")

    # ── Loop cadences ─────────────────────────────────────────────────────────────────────
    risk_loop_seconds: float = Field(default=5.0, ge=1.0, alias="RISK_LOOP_SECONDS")
    cold_loop_seconds: float = Field(default=60.0, ge=5.0, alias="COLD_LOOP_SECONDS")
    reconcile_seconds: float = Field(default=60.0, ge=10.0, alias="RECONCILE_SECONDS")
    scout_health_port: int = Field(default=8080, alias="SCOUT_HEALTH_PORT")
    trader_health_port: int = Field(default=8081, alias="TRADER_HEALTH_PORT")

    # ── Trader feed (D-30): Solana websocket push for the copy list, the poll as the safety net ────────────────
    # wss URL with the Chainstack key inside: a SECRET. Empty = derived from SOLANA_RPC_URL (https -> wss, same host and
    # key path on a Chainstack Global Node, docs.chainstack.com read 2026-11-18).
    solana_ws_url: str = Field(default="", alias="SOLANA_WS_URL")
    # One programSubscribe per copy-list owner (filters are AND only); tracked owners first. Owners above the cap are
    # watched by the poll only.
    solana_ws_max_subscriptions: int = Field(default=100, ge=0, alias="SOLANA_WS_MAX_SUBSCRIPTIONS")
    # getMultipleAccounts on the listed owners' known Position accounts: catches what the socket missed.
    feed_poll_seconds: float = Field(default=30.0, ge=10.0, alias="FEED_POLL_SECONDS")
    # The trader refuses new opens (LIST_STALE) when the scout's last pick is older than this; exits continue.
    copylist_max_age_s: float = Field(default=600.0, gt=0.0, alias="COPYLIST_MAX_AGE_S")
    copylist_refresh_s: float = Field(default=60.0, ge=5.0, alias="COPYLIST_REFRESH_S")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Environment first, then `settings.toml`, then the defaults."""
        path = settings_file()
        toml: tuple[PydanticBaseSettingsSource, ...] = (
            () if path is None else (TomlConfigSettingsSource(settings_cls, toml_file=path),)
        )
        return (init_settings, env_settings, dotenv_settings, *toml, file_secret_settings)

    @field_validator(
        "kucoin_api_key",
        "kucoin_api_secret",
        "kucoin_api_passphrase",
        "telegram_bot_token",
        "telegram_chat_id",
        "heartbeat_url",
        "solana_rpc_url",
        "solana_ws_url",
        mode="before",
    )
    @classmethod
    def _unset_is_absent(cls, v: object) -> str:
        return _blank_if_unset(str(v) if v is not None else "")

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_known(cls, v: object) -> str:
        raw = str(v or "").strip().lower()
        if raw not in (PAPER, LIVE):
            raise ValueError(f"MODE must be 'paper' or 'live'; got {v!r}")
        return raw

    @model_validator(mode="after")
    def _live_needs_everything(self) -> "Settings":
        """Live trading refuses to boot without every credential and a starting balance (plan: go-live)."""
        if self.mode != LIVE:
            return self
        missing = [
            name
            for name, value in (
                ("KUCOIN_API_KEY", self.kucoin_api_key),
                ("KUCOIN_API_SECRET", self.kucoin_api_secret),
                ("KUCOIN_API_PASSPHRASE", self.kucoin_api_passphrase),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"MODE=live but {', '.join(missing)} missing from the solana-future-bot secret. Refusing to boot."
            )
        if self.live_start_balance <= 0:
            raise ValueError("MODE=live needs LIVE_START_BALANCE (the real USDT in the futures account). Refusing.")
        if not self.kucoin_key_dedicated:
            raise ValueError(
                "MODE=live but KUCOIN_KEY_DEDICATED is not true: this bot shares the KuCoin account and key with the "
                "KuCoin bot (BUG B-3). Give each bot its own KuCoin sub-account and API key (locked to its own box's "
                "IP), put this bot's key in its secret, then set KUCOIN_KEY_DEDICATED=true. Refusing to boot."
            )
        return self

    @model_validator(mode="after")
    def _tiers_ordered(self) -> "Settings":
        if self.gold_min_profit_factor < self.silver_min_profit_factor:
            raise ValueError("GOLD_MIN_PROFIT_FACTOR is below SILVER_MIN_PROFIT_FACTOR")
        if self.gold_max_drawdown > self.silver_max_drawdown:
            raise ValueError("GOLD_MAX_DRAWDOWN is looser than SILVER_MAX_DRAWDOWN")
        if self.gold_min_trades < self.silver_min_trades:
            raise ValueError("GOLD_MIN_TRADES is below SILVER_MIN_TRADES")
        if self.jupiter_min_episodes > self.jupiter_full_proof_episodes:
            raise ValueError("JUPITER_MIN_EPISODES is above JUPITER_FULL_PROOF_EPISODES")
        if self.jupiter_history_source not in ("api", "chain"):
            raise ValueError(f"JUPITER_HISTORY_SOURCE must be 'api' or 'chain'; got {self.jupiter_history_source!r}")
        return self

    @model_validator(mode="after")
    def _two_programs_two_files(self) -> "Settings":
        """D-30: each program writes its own file; one file for both would let two writers fight over one database."""
        if os.path.abspath(self.scout_db_path) == os.path.abspath(self.trader_db_path):
            raise ValueError("SCOUT_DB_PATH and TRADER_DB_PATH must be different files")
        if self.copylist_max_age_s < 2 * self.jupiter_grade_minutes * 60:
            raise ValueError("COPYLIST_MAX_AGE_S must cover at least two picks (2 x JUPITER_GRADE_MINUTES)")
        return self

    @model_validator(mode="after")
    def _digest_divides_a_day(self) -> "Settings":
        if 1440 % self.digest_minutes:
            raise ValueError(f"DIGEST_MINUTES={self.digest_minutes} must divide 1440 (windows are aligned to midnight)")
        return self

    @model_validator(mode="after")
    def _trail_inside_activation(self) -> "Settings":
        if self.trail_giveback_price_pct >= self.trail_activate_price_pct:
            raise ValueError(
                "TRAIL_GIVEBACK_PRICE_PCT must be below TRAIL_ACTIVATE_PRICE_PCT, or an armed trail closes at a loss"
            )
        return self

    # ── Derived values ────────────────────────────────────────────────────────────────────

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE

    @property
    def account(self) -> str:
        """The ledger account name. Paper and live rows never mix; a new PAPER_EPOCH starts a fresh paper account."""
        return LIVE if self.is_live else f"{PAPER}-{self.paper_epoch}"

    @property
    def start_balance(self) -> float:
        return self.live_start_balance if self.is_live else self.paper_start_balance

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ws_url(self) -> str:
        """The Solana websocket URL: SOLANA_WS_URL, else SOLANA_RPC_URL with https -> wss (never logged: a key)."""
        if self.solana_ws_url:
            return self.solana_ws_url
        url = self.solana_rpc_url
        if url.startswith("https://"):
            return "wss://" + url[len("https://") :]
        if url.startswith("http://"):
            return "ws://" + url[len("http://") :]
        return url


settings = Settings()
