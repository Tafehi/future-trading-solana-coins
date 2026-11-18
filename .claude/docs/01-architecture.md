# 01 — Architecture

Back to the index: [CLAUDE.md](../../CLAUDE.md).

## What it is

One Python 3.12 asyncio process in one container on its own EC2 box (t4g.micro, Frankfurt). It finds the best traders
on **Jupiter Perps** (Solana), where positions are public, copies their opens onto **KuCoin USDT-M perpetuals**, and
closes with its own exits. Paper and live share every line of code except the class that sends orders.

Split on 2026-11-14 from `future-trading-coins` (D-19). The Hyperliquid source stayed there (that repo is now
`future-trading-kucoins`). The two bots share nothing at runtime except the KuCoin account key (each in its own secret: `solana-future-bot` here), the Telegram chat
and the shared history bucket (each with its own folders).

```
Jupiter change feed + chain history (close events) -> episodes -> grade -> selection: tracked (copied)
                                                                           and watched (shadow only)
                                                  | change feed (every JUPITER_POLL_SECONDS)
                                                  v
                    OPEN/CLOSE signals -> gates -> sizing -> Executor (Paper | KuCoin)
                                                                  |
                         ledger (SQLite) <- fills, fees, funding, exits (risk loop)
                               |
     15-min Telegram digest [PAPER] Jupiter-KuCoin · Parquet recorder -> shared bucket futures/jupiter, futures/solana-bot
     · /health · statusCheck
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
| `data/jupiter_perps.py`, `solana_rpc.py` | Position account decoder, change-feed and history RPC calls |
| `data/jupiter_events.py` | close events inside a transaction -> episodes with real PnL (item 1) |
| `core/selection.py` | tracked vs watched, shadow results first, SILVER cap (items 4, 5) |
| `core/wallet_stats.py`, `wallet_grader.py`, `signals.py` | episodes, PF, drawdown, tiers, weights, diff (shared rules) |
| `core/gates.py`, `golive.py` | market / probe / account gates; go-live gates |
| `jobs/jupiter.py` | Jupiter change feed -> episodes, signals; grading and re-pick |
| `jobs/jupiter_history.py` | backfill Jupiter owners from the chain inside a daily call budget (item 1) |
| `trading/shadow.py` | shadow copies of every tracked/watched wallet, no money (item 4) |
| `trading/executor.py`, `paper_executor.py`, `kucoin_executor.py` | one interface, contract rules |
| `trading/futures_paper.py`, `ledger.py`, `risk.py`, `copier.py` | fill math, money record, exits/funding/reconcile, signal->position |
| `notifications/telegram.py`, `messages.py`, `digest.py` | client, text (stamp `[PAPER] <BOT_NAME>`, D-21), 15-min windows and alarms |
| `record/recorder.py` | Parquet spool for the shared bucket: `jupiter/positions`, `solana-bot/*` (D-20) |
| `api/health.py`, `main.py`, `tools/` | /health, loops, `golive_check`, `trade_log`, `wallets_report` |
| `tools/wallet_trace/` | **not part of the bot**: read-only wallet tracer for the laptop (Hyperliquid + Solana), never imported by `main.py` (D-25, doc 09). Next: it changes how wallets are found (D-28) |

## Loops (`app/main.py`)

jupiter `JUPITER_POLL_SECONDS` (+ hourly verify, re-grade every `JUPITER_GRADE_MINUTES`) · jhistory 60 s · events 1 s
· risk `RISK_LOOP_SECONDS` (+ shadow exits) · cold `COLD_LOOP_SECONDS` (markets, funding, ledger check, digest,
recorder) · reconcile (live only). Each beats `meta beat:<name>`; `/health` names a quiet loop.
Network calls never run inside a SQLite write transaction (ai-kucoin-future D-17).

## Account model

One ledger account: `paper-<PAPER_EPOCH>` or `live`. Rows never mix. Identity checked every 5 min:
`cash + open margin == deposits + closed gross - fees + funding`; drift > $0.01 (rounded, DEBUG D-3) halts.
The ledger is this bot's own (its own SQLite file `data/solana-future-coin.db` on its own box); it never sees the KuCoin bot's.
