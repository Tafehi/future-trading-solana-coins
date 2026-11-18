# 08 — Data written to the shared bucket

Back to the index: [CLAUDE.md](../../CLAUDE.md). Writer: `app/record/recorder.py`; uploader: host timer
`solana-future-coin-spool` (`aws s3 mv`, only `futures/*.parquet`, never `.tmp`).

`s3://bigdata-shared-history-267850879774/futures/<dataset>/day=YYYY-MM-DD/part-HHMM-<ms>.parquet` (zstd, one file per
dataset per 5-minute flush; read every file in a day folder).

This bot writes only its own folders (D-20). The KuCoin market data (`kucoin/contracts`, `kucoin/book_top`) and the
Hyperliquid wallets' `grades` / `bot/*` come from the KuCoin bot (`future-trading-kucoins`), not from here.

| Dataset | Every | Columns |
|---|---|---|
| `jupiter/positions` | each Jupiter poll, open positions >= $1k updated in the current block | ts_ms, pubkey, owner, coin, side, size_usd, entry_px, open_time |
| `solana-bot/grades` | daily | snapshot_ms, address, source, tier, style, trades, profit_factor, max_drawdown, score, copy_weight, tracked, graded_at, reason |
| `solana-bot/positions` | each flush, positions closed since the last | every `positions` column (paper and live, `account` says which) |
| `solana-bot/shadow` | each flush, shadow copies closed since the last (item 4) | every `shadow_positions` column (wallet, source, tier, entry/exit, reason, ret_pct) |
| `solana-bot/episodes` | each flush, new `jup_episodes` rows (2026-11-16) | every `jup_episodes` column: owner, coin, side, size_usd, entry/exit px, opened/closed_at, pnl, origin (feed/chain), position_key — the Jupiter wallet history grading uses |

Why: wallet positions cannot be fetched later; the daily grade snapshot lets a backtest use the wallet list as it was
on that day (no look-ahead). Bucket policy: Pump-fun-S3 D-127 / D-129; D-129 (in code there, 2026-11-16) adds
`solana-future-coin-role`. Until it is pushed, uploads get AccessDenied and files wait in the spool (BUG B-4).
