# 03 — Configuration and secrets

Back to the index: [CLAUDE.md](../../CLAUDE.md).

## Layers

1. Environment (on the box: `/opt/solana-future-coin/.env`, rendered hourly from Secrets Manager `Kucoin-bot`).
2. `app/settings.toml` (non-secret; a test fails on an unknown key or a secret key in it).
3. Defaults in `app/config.py` (each with a comment saying why).

## The secret `Kucoin-bot` (eu-central-1, hand-made by the user, never touched by CI)

Shared with the KuCoin bot (`future-trading-kucoins`) for now (user, 2026-11-14). Live needs this bot's own key and
secret first (BUG B-3).

| Key | Needed for | State 2026-11-13 |
|---|---|---|
| `KUCOIN_API_KEY`, `KUCOIN_API_SECRET` | live orders | present, but IP-locked to the KuCoin box: unusable from here (B-3) |
| `KUCOIN_API_PASSPHRASE` | live orders (KuCoin signs every private call with it) | present since 2026-11-13, unused until live (B-3) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | the digest and alarms (same chat as the KuCoin bot; `BOT_NAME` tells them apart) | present |
| `SOLANA_RPC_URL` | the ONLY source: Jupiter change feed and history (Chainstack HTTPS URL) | present since 2026-11-13, never used from a box yet — B-5 |
| `HEARTBEAT_URL` | optional healthchecks.io ping | optional |

`solana-future-coin-env-refresh` renders every key (`jq to_entries`), so adding a key needs no code change; the bot restarts
only when the file changed.

## Mode switches

- `MODE = "paper" | "live"`. Live refuses to boot (non-zero exit) without key, secret, passphrase and
  `LIVE_START_BALANCE > 0`; then the pre-flight (`main.live_preflight`) refuses unless a signed call works, futures
  equity >= `LIVE_START_BALANCE` and KuCoin holds no position the bot does not know.
- `PAPER_EPOCH` change = a fresh paper account (old rows kept under the old name).
- Kill switch: the file `data/STOP` (host: `/opt/solana-future-coin/data/STOP`) closes everything and stops; while it
  exists the process idles at boot.

## Settings that changed in the split (2026-11-14, D-19)

- Removed (Hyperliquid only): `HL_*`, `POLL_SECONDS`, `MAX_TRACKED_WALLETS`, `WALLET_CHECK_MINUTES`, the discovery
  prefilter (`DISCOVERY_INTERVAL_HOURS`, `MAX_ACCOUNT_VALUE_USD`, `MIN/MAX_MONTH_ROI`, `MAX_WALLETS_TO_GRADE`,
  `GRADE_TOP_SHARE`, `SKIP_VAULTS`, `LONG_HISTORY_DAYS`, `GRADE_HISTORY_DAYS`, `MAX_FILL_PAGES`),
  `SHADOW_MAX_WALLETS`, `SHADOW_POLL_SECONDS`, `HL_STREAM_*`, `RECORD_BOOK_TOP_N`, and the Hyperliquid weight check.
  A removed key left in `settings.toml` fails `test_settings_file_has_no_secret_and_only_known_keys`.
- Added: `BOT_NAME` (default `Jupiter-KuCoin`), the name in every Telegram message (D-21).
- `DB_PATH` default is `data/solana-future-coin.db`.
