# 03 — Configuration and secrets

Back to the index: [CLAUDE.md](../../CLAUDE.md).

## Layers

1. Environment (on the box: `/opt/solana-future-coin/.env`, rendered hourly from Secrets Manager `solana-future-bot`).
2. `app/settings.toml` (non-secret; a test fails on an unknown key or a secret key in it).
3. Defaults in `app/config.py` (each with a comment saying why).

## The secret `solana-future-bot` (eu-central-1, hand-made by the user, never touched by CI)

This bot's own secret since 2026-11-16 (user); the old shared secret is deleted, the KuCoin bot reads
`kucoin-future-bot`. The KuCoin key inside is still the same account key as the KuCoin bot's: live needs this bot's
own KuCoin sub-account and key first (BUG B-3). `hostSetup.sh` section 0 rewrites the `.env` script, so a secret rename
reaches a running box; an unreadable secret keeps the last `.env` and `statusCheck.sh` section 3 warns.

| Key | Needed for | State |
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
- `DIGEST_MINUTES = 45`, `DIGEST_ALWAYS = true` (user, 2026-11-16): a Telegram report every 45 min even without a
  trade (open positions + account); `false` = only windows with a trade. The minutes must divide 1440.
- `DB_PATH` default is `data/solana-future-coin.db`.

## Settings added on 2026-11-18 (D-29: more wallets pass)

| Setting | Default | What |
|---|---|---|
| `JUPITER_HISTORY_SOURCE` | `api` | `api` = an owner's past from Jupiter's free trade API (the chain read only when it fails); `chain` = Chainstack only, as before |
| `JUPITER_API_RPS` | 2 | pace of the trade API (it answered 10/s without a 429 on 2026-11-18) |
| `JUPITER_HISTORY_OWNERS_PER_PASS` | 80 | owners read per minute (the jhistory loop) |
| `JUPITER_HISTORY_MIN_SIZE_USD` | 1,000 | was 10,000: every recorded owner gets its past read |
| `JUPITER_MIN_EPISODES` | 8 | was 15 |
| `JUPITER_MIN_RECORD_DAYS` | 1 | was 14: the API gives every owner 90 days of record on its first read |
| `JUPITER_FULL_PROOF_EPISODES` | 15 | below this the needed PF grows as sqrt(15 / episodes), and a record with no loss counts PF = its episode count |
| `JUPITER_SMALL_SAMPLE_MAX_WEIGHT` | 3 | copy weight cap for a record below `JUPITER_FULL_PROOF_EPISODES` |
| `JUPITER_REGRADE_HOURS` | 24 | a grade is redone at least this often, so a slow wallet does not drop out after 72 h |

`JUPITER_HISTORY_CALLS_PER_DAY` (20,000) still caps Chainstack calls, now for the chain fallback only. Boot refuses
`JUPITER_MIN_EPISODES > JUPITER_FULL_PROOF_EPISODES` and an unknown `JUPITER_HISTORY_SOURCE`.

