# 01 — Architecture

Back to the index: [CLAUDE.md](../../CLAUDE.md).

## What it is

Two Python 3.12 asyncio programs from one image, in two containers on one EC2 box (t4g.micro, Frankfurt), since
2026-11-19 (D-30). The bot finds the best traders on **Jupiter Perps** (Solana), where positions are public, copies
their opens onto **KuCoin USDT-M perpetuals**, and closes with its own exits. Paper and live share every line of code
except the class that sends orders.

- **SCOUT** (`python -m app.scout`, container `solana-future-coin-scout`, /health 8080): finds, records, grades and
  picks wallets. Writes only `data/scout.db`. Never trades, never sends Telegram (a test proves it loads no trading or
  Telegram code).
- **TRADER** (`python -m app.trader`, container `solana-future-coin-trader`, /health 8081): its own websocket feed of
  the picked wallets, the copier, exits, ledger, Telegram, kill switch. Writes only `data/solana-future-coin.db` (the
  paper account). Never grades (a test proves it loads no grading or history code).
- They share no memory. The only links are read-only reads of the other's file (`app/db/crossread.py`): the trader
  reads the copy list once a minute, the scout reads the trader's shadow record to pick.

Split on 2026-11-14 from `future-trading-coins` (D-19). The Hyperliquid source stayed there (that repo is now
`future-trading-kucoins`). The two bots share nothing at runtime except the KuCoin account key (each in its own secret: `solana-future-bot` here), the Telegram chat
and the shared history bucket (each with its own folders).

```
SCOUT (scout.db)                                          TRADER (solana-future-coin.db)
Jupiter change feed (60 s) + trade API history            copy list (read-only, every 60 s) -> websocket owners
  -> owners, episodes -> grade (5 min) -> pick ---------> Solana websocket (programSubscribe per owner, ~1-2 s)
  tracked + watched + jup:picked_at                        + safety-net poll (getMultipleAccounts, 30 s)
          ^                                                  -> one queue, one worker: decide() -> signals (once)
          | shadow record (read-only)                        -> copier (woken at once): gates -> sizing -> Executor
          +---------------------------------------------- shadow copies, ledger, exits (risk loop), Telegram 45 min
recorder: jupiter/positions, solana-bot/grades, episodes   recorder: solana-bot/positions, solana-bot/shadow
```

## Why KuCoin trades but does not supply wallets

KuCoin has no API that shows other traders' positions (its copy-trade API is for being a lead trader; checked
2026-11-13). Jupiter Perps positions are public by address, so wallets are graded there and copied on KuCoin. Nothing
is traded on Solana (D-4). Trading on Jupiter itself is a possible later step (D-19, "revisit when").

## Modules (`app/`, package name `app`, D-2)

| Path | Role |
|---|---|
| `config.py` | env > `settings.toml` > defaults; validators refuse bad combinations (live without passphrase) |
| `data/http.py`, `kucoin.py`, `symbol_map.py` | public KuCoin market data, rate limits, coin -> contract map |
| `data/market.py` | contracts + the map for Jupiter's coins only (`JUPITER_COINS` = SOL, ETH, BTC), cached |
| `data/hyperliquid.py` | **shared data types only** (`Fill`, `WalletPosition`, `AccountState`) and their parsers: the grading code imports them. No Hyperliquid client any more (B-17) |
| `data/kucoin_private.py` | signed KuCoin REST (live only) |
| `data/jupiter_perps.py`, `solana_rpc.py` | Position account decoder (`decode_account` checks size + discriminator), change-feed, history and `positions_at` (with the slot) RPC calls |
| `data/solana_ws.py` | the trader's websocket: programSubscribe per owner, diff, resubscribe on reconnect, own ping, backoff (D-30) |
| `data/jupiter_events.py` | close events inside a transaction -> episodes with real PnL (item 1) |
| `core/selection.py` | tracked vs watched, shadow results first, SILVER cap (items 4, 5) |
| `core/wallet_stats.py`, `wallet_grader.py`, `signals.py` | episodes, PF, drawdown, tiers, weights, diff (shared rules) |
| `core/gates.py`, `golive.py` | market / probe / account gates; go-live gates |
| `jobs/jupiter.py` | (scout) Jupiter change feed -> owners, episodes; grading and the pick (writes `jup:picked_at`; fails closed without the shadow record). No signals since D-30 |
| `jobs/jupiter_history.py` | backfill Jupiter owners from the chain inside a daily call budget (item 1) |
| `trading/shadow.py` | shadow copies of every tracked/watched wallet, no money (item 4); the stats read is in `db/crossread.py` |
| `trading/watch.py` | (trader) the feed's rules: pure `decide()` (freshness, forward-only state, duplicates) and `PositionTracker` (`feed_positions` + signals in one transaction) |
| `trading/copylist.py` | (trader) the scout's picks in memory: age, LIST_STALE, the websocket's owner order |
| `db/crossread.py` | every read of the OTHER program's file, always read-only (`mode=ro`, never creates a file) |
| `trading/executor.py`, `paper_executor.py`, `kucoin_executor.py` | one interface, contract rules |
| `trading/futures_paper.py`, `ledger.py`, `risk.py`, `copier.py` | fill math, money record, exits/funding/reconcile, signal->position |
| `notifications/telegram.py`, `messages.py`, `digest.py` | client, text (stamp `[PAPER] <BOT_NAME>`, D-21), 15-min windows and alarms |
| `record/recorder.py` | Parquet spool for the shared bucket: `jupiter/positions`, `solana-bot/*` (D-20) |
| `runtime.py`, `scout.py`, `trader.py` | the shared loop runner (beats, `wake`), each program's own file (`use_db`), signals; the two programs |
| `api/health.py`, `tools/` | /health per program; `golive_check`, `trade_log` (trader's file), `wallets_report` (both files), all read-only |
| `tools/wallet_trace/` | **not part of the bot**: read-only wallet tracer for the laptop (Hyperliquid + Solana), never imported by `scout.py` or `trader.py` (D-25, doc 09). Next: it changes how wallets are found (D-28) |

## Loops

Each loop beats `meta beat:<name>` in its own program's file; `/health` names a quiet loop (a test reads both files).

- **Scout:** jupiter `JUPITER_POLL_SECONDS` (change feed, hourly verify, recorder) · jhistory 60 s · grade 60 s
  (grades and picks once `JUPITER_GRADE_MINUTES` passed; its own loop, so a failing feed poll cannot stop the picks)
  · cold `COLD_LOOP_SECONDS` (market for the episodes' marks, grades snapshot, episodes export).
- **Trader:** events 1 s and at once on a new signal · risk `RISK_LOOP_SECONDS` (kill switch, exits, shadow exits) ·
  cold `COLD_LOOP_SECONDS` (markets, funding, ledger check, digest, alarms, recorder, heartbeat) · copylist
  `COPYLIST_REFRESH_S` · feedpoll `FEED_POLL_SECONDS` · reconcile (live only). Plus two tasks: the websocket and the
  feed worker (both in the trader's /health).

Network calls never run inside a SQLite write transaction (ai-kucoin-future D-17).

## Account model

One ledger account: `paper-<PAPER_EPOCH>` or `live`. Rows never mix. Identity checked every 5 min:
`cash + open margin == deposits + closed gross - fees + funding`; drift > $0.01 (rounded, DEBUG D-3) halts.
The ledger is this bot's own (the trader's SQLite file `data/solana-future-coin.db` on its own box); it never sees
the KuCoin bot's. Both files carry the same schema (V1-V4); each program writes only its own tables. The trader's file
still holds the single bot's frozen `wallets` and `jup_*` tables (B-28): nothing reads them.
