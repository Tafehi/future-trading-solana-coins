# 01 — Architecture

Back to the index: [CLAUDE.md](../../CLAUDE.md).

## What it is

A read-only wallet tracer (PLAN.md). It reads public APIs, builds a profile per wallet, classifies it, links wallets
that probably share an owner, walks the money trail N steps out, and writes a report under `audit/`. No server, no
loop, no database: one command, one run.

## Data flow

```
start address(es)
      │
      ▼
 walk.py  (breadth-first; depth, top-N per direction, stop list, call budget)
      │  for each wallet
      ▼
 hl_source.py / sol_source.py ──► cache.py (audit/wallet_trace/cache: a re-run costs 0 calls)
      │  raw API answers
      ▼
 profile.py / sol_profile.py  → Profile (fills, volumes, transfers, airdrops, staking, balance check ...)
      │
      ├─► classify.py → Verdict + reasons   (rules 1-9, first match wins)
      └─► links.py    → edges + twin scores + groups
      │
      ▼
 report.py → audit/wallet_trace/<UTC time>/report.md, edges.csv, tree.txt
```

## Modules (`app/`)

| Path | Role |
|---|---|
| `tools/wallet_trace/` | the tracer (table of its files: doc 09) |
| `data/http.py` | `RateLimiter`, `WeightBudget`, `_request` with retry on 429/5xx, `DataError` |
| `data/solana_rpc.py` | minimal Solana JSON-RPC client; never puts its URL in an error (DEBUG D-10) |
| `data/jupiter_perps.py` | Jupiter Perps Position account layout and decoder (checked on mainnet, DEBUG D-2) |
| `data/jupiter_events.py` | Jupiter Perps close events inside a transaction -> episodes with real PnL (DEBUG D-5) |
| `utils/logging.py` | logging with `RedactSecrets` (an RPC key in a URL never reaches a log line) |

## Pacing (public APIs, measured 2026-11-17)

- Hyperliquid: 1200 weight a minute per IP; the tracer budgets 900 and waits out a lasting 429. About one call a
  second in practice (most calls weigh 20).
- Solana public RPC: 1 call a second (it throttles at ~2).
- `--max-calls` (default 500) caps a run; the report lists what was not reached.
