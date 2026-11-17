# 09 — Wallet tracer (`app/tools/wallet_trace/`)

Back to the index: [CLAUDE.md](../../CLAUDE.md). The brief is [PLAN.md](../../PLAN.md); this file is what was built.

A **read-only research tool for the laptop**: for any wallet it says what kind of wallet it is, where its money came from
and went (N steps), which wallets have the same owner, and whether it trades futures, spot or both. Hyperliquid and
Solana. Since 2026-11-17 it is the whole repo: the copy-trading bot was removed (D-26), and
`test_no_trading_or_bot_code_comes_back` keeps it read-only. No AWS, no Telegram, no key.

## Run it

```bash
.venv/bin/python -m app.tools.wallet_trace <addr> [<addr> ...] --chain hl --depth 2 --top 3
.venv/bin/python -m app.tools.wallet_trace <solana-addr> --chain sol --depth 1 --top 3
#   --max-calls 500   stop after this many API calls; the report lists the wallets not visited
#   --refresh         ignore the cache (answers are still saved)
#   --rpc-url URL     another Solana RPC (default: the free public one; never printed)
#   --sol-usd PRICE   SOL price for USD values (default: Hyperliquid's public SOL mid)
```

- Output: `audit/wallet_trace/<UTC time>/report.md`, `edges.csv`, `tree.txt`. Cache:
  `audit/wallet_trace/cache/<chain>/<address>/<query>.json`. Both git-ignored (real addresses); the CLI and the cache
  refuse any folder outside `audit/`.
- Labels: `audit/wallet_trace/labels.toml`, a `[labels]` table of `address = "name"` (exchanges, bot apps). The walk
  stops at a labelled wallet. Outside git (user, 2026-11-17).
- Speed (measured 2026-11-17): Hyperliquid ~11 calls per wallet; a depth-2, top-3 run from one holder = 11 wallets,
  190 calls, 3.5 min. A re-run of the same trace answers from the cache. Solana: about 1 call per transaction read
  (100 newest + the 3 oldest per wallet), ~1 call a second on the public RPC.

## Modules

| File | Role |
|---|---|
| `settings.py` | every threshold in one block (classify, links, walk, pacing). Starting points fitted to the case, not truths |
| `model.py` | `Profile` (PLAN §5 numbers), `Verdict`, `Transfer`, `Edge`, `Clue`, `TwinScore` |
| `cache.py` | raw answers on disk; `inside_audit` guard |
| `hl_source.py` | Hyperliquid info client: paging by time, weight budget, 429 wait, vault names. What was checked and when: its docstring |
| `profile.py` | Hyperliquid raw -> Profile, including the balance replay (`complete`) |
| `sol_source.py`, `sol_profile.py` | Solana RPC (via `data/solana_rpc.SolanaRpc`) and its decoding: transfers, swaps, funder, Jupiter Perps |
| `classify.py` | rules 1-9, first match wins, with reasons |
| `links.py` | transfer edges, twin score and groups |
| `walk.py` | breadth-first walk with depth, top-N, stop list, call budget |
| `report.py`, `__main__.py` | report files; CLI |

## Rules learned on real data (each has a test and a mutant)

- **Balance replay = completeness.** Every movement of every token is summed and compared with the live balance. A gap
  means missing history, and the wallet is `INCOMPLETE` (best guess still printed). The funder's HYPE sums to
  0.91567001 against the live 0.91567001 (PLAN §2.2).
- **Staking comes from `delegatorHistory`**: the ledger missed three staking moves of a real wallet (DEBUG D-8).
- **TWAP slices are not in `userFillsByTime`**: `userTwapSliceFillsByTime` is read too (DEBUG D-9).
- **A liquidation leaves no row for the margin it takes**: USDC gets a note, not INCOMPLETE.
- **Airdrop dust can vanish with no ledger row**: a gap under `BALANCE_USD_TOL` ($10) is a note.
- **toPerp -> withdraw within `EXIT_DOOR_MINUTES` is money leaving, not trading** (on Hyperliquid you can only withdraw
  from the perp side).
- **CASH-OUT measures money-in to money-out**, not ledger life: a cash-out wallet that got an airdrop months later had a
  389-day ledger life although the cash-out took one hour.
- **Twins:** all shared airdrops together count as ONE clue (max 3 points), and airdrops alone never make a group:
  early wallets share dozens of airdrop seconds with strangers. The case twins pass on two shared small senders.
- **Solana:** ask for transaction version 1 (SIMD-0385 is live; version 0 gets error -32015). The public RPC throttles
  at ~2 calls/s, so Solana runs at 1/s. A swap's SOL side is the wallet's SOL change minus the network fee (a real
  pump.fun sell moved 0.0023 SOL). Only the newest transactions are read, so Solana verdicts are marked unsure
  (`partial`), never `INCOMPLETE`.

## Tests and fixtures

- `test_wallet_trace_golden.py`: PLAN §2.2 from `fixtures/wallet_trace_hl_case.json` (16 case wallets, anonymised).
- `test_wallet_trace_rules.py`: every rule AT its boundary value.
- `test_wallet_trace_tool.py`: paging, cache (a re-run costs 0 calls), budget, 429 wait, walk, report, CLI guard,
  the tool imports with no env, no trading code comes back.
- `test_wallet_trace_sol.py`: a live Jupiter Perps trader is `PERP TRADER`, a live pump.fun trader gets a verdict with
  reasons (`fixtures/wallet_trace_sol_case.json`), swap and transfer decoding, the funder, the URL never in an error.
- Fixtures are built by `scripts/make_trace_fixtures.py` from the case cache in `audit/` (run by hand; deterministic).
  Hyperliquid addresses become `0xabab…NN`, Solana keys `fixture-key-NNN`; hashes and signatures are removed; public
  program ids and mints stay (they are in `scripts/checks.py ALLOWED_PUBLIC_KEYS`). Jupiter events are stored decoded,
  because their bytes carry the owner.

## Not in v1 (said in every report)

Arbitrum after a Hyperliquid `withdraw`, HyperEVM after a send to a system address, exchange hot wallets: the report
prints the explorer link. Phase 6 (the tracer feeding a copy bot's wallet selection) is not built, and the copy bot is gone: the user
decides, and it needs its own DECISIONS row.
