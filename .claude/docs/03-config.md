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
| `SOLANA_RPC_URL` | the ONLY source: the scout's change feed and history, the trader's safety-net poll (Chainstack HTTPS URL) | present since 2026-11-13 |
| `SOLANA_WS_URL` | optional: the trader's websocket. Unset = `SOLANA_RPC_URL` with `https://` -> `wss://` (a Chainstack Global Node serves WSS on the same host and key, D-30). A SECRET (the key is inside): never in `settings.toml`, never logged | not set (derived) |
| `HEARTBEAT_URL` | optional healthchecks.io ping | optional |

`solana-future-coin-env-refresh` renders every key (`jq to_entries`), so adding a key needs no code change; the bot restarts
only when the file changed.

## Mode switches

- `MODE = "paper" | "live"`. Live refuses to boot (non-zero exit) without key, secret, passphrase and
  `LIVE_START_BALANCE > 0`; then the pre-flight (`trader.live_preflight`) refuses unless a signed call works, futures
  equity >= `LIVE_START_BALANCE` and KuCoin holds no position the bot does not know.
- `PAPER_EPOCH` change = a fresh paper account (old rows kept under the old name).
- Kill switch: the file `data/STOP` (host: `/opt/solana-future-coin/data/STOP`): the TRADER closes everything and
  stops; while it exists the trader idles at boot. The scout ignores it (it never trades) and keeps recording.

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

## Settings added on 2026-11-19 (D-30: scout + trader, the websocket feed)

| Setting | Default | What |
|---|---|---|
| `SCOUT_DB_PATH` | `data/scout.db` | the scout's own file (the scout sets `DB_PATH` to it at start, `runtime.use_db`) |
| `TRADER_DB_PATH` | `data/solana-future-coin.db` | the trader's own file (the paper account of before); tools default to it |
| `SCOUT_HEALTH_PORT`, `TRADER_HEALTH_PORT` | 8080, 8081 | replace `HEALTH_PORT` |
| `SOLANA_WS_URL` | derived | see the secret table above |
| `SOLANA_WS_MAX_SUBSCRIPTIONS` | 100 | one `programSubscribe` per owner (filters are AND only); above it the poll alone covers the rest |
| `FEED_POLL_SECONDS` | 30 | the trader's safety-net poll (`getMultipleAccounts`, 100 accounts a call) |
| `COPYLIST_MAX_AGE_S` | 600 | the scout's pick older than this -> new opens refused `LIST_STALE`; exits continue |
| `COPYLIST_REFRESH_S` | 60 | how often the trader reads the copy list |

Boot refuses one file for both programs (`SCOUT_DB_PATH` == `TRADER_DB_PATH` after normalising the path) and
`COPYLIST_MAX_AGE_S` below two picks (`2 x JUPITER_GRADE_MINUTES x 60`; exactly two is allowed). Per container (in
`docker-compose.yml`, not settings): `LOG_DIR` (`/app/logs/scout`, `/app/logs/trader`) and `HEALTH_URL` (the image's
healthcheck).

## Settings changed on 2026-11-19 (D-31: three copy-list rules from the backtest)

| Setting | Default | What |
|---|---|---|
| `MAX_BIGGEST_WIN_SHARE` | 0.5 (was 1.0, off) | an owner whose biggest single win is more than half of its total profit is BRONZE |
| `JUPITER_MIN_WIN_RATE` | 0.4 | an owner whose trades win less often is BRONZE (0 = off) |
| `JUPITER_MAX_IDLE_DAYS` | 14 | an owner with no closed trade in this many days is not picked (neither tracked nor watched) |

All three are in `app/settings.toml` too. The wallet-rule backtest reads them, so its "now" line always shows the
rules the scout runs.
