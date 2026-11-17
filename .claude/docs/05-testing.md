# 05 — Testing

Back to the index: [CLAUDE.md](../../CLAUDE.md).

- **One command:** `./precommit.sh` (exit code = failed stages; `MUTANTS=0` skips the mutants for speed). Before a push
  also `scripts/verify_clone.sh` (the checks and tests on a fresh clone of HEAD, DEBUG D-4).
- **Offline:** `app/tests/conftest.py` blocks internet sockets and DNS. Every source takes an `httpx.MockTransport`.
- **Golden test** (`test_wallet_trace_golden.py`): the PLAN §2.2 case from `fixtures/wallet_trace_hl_case.json` — every
  verdict, the twins, the funder's HYPE sum (0.91567001), no LIKELY BOT.
- **Rules at the boundary value** (`test_wallet_trace_rules.py`): every classify rule, the exit door, the pass-through
  window, one-shot, round lots, the balance check, system addresses, the twin score — each exactly AT its limit and
  one step past it.
- **Tool tests** (`test_wallet_trace_tool.py`): paging, cache (a re-run costs 0 calls), call budget, weights, 429 wait,
  walk, report, CLI and cache refuse folders outside `audit/`, the tool imports with no env, and no trading or old-bot
  code comes back.
- **Solana** (`test_wallet_trace_sol.py`): a live Jupiter trader is PERP TRADER, a live pump.fun trader gets a verdict,
  swaps and transfers decode from the wallet's own balances, the funder, the RPC URL never in an error.
- **Decoders** (`test_jupiter.py`, `test_jupiter_history.py`): real mainnet Position accounts and close transactions.
- **Fixtures:** built only by `scripts/make_trace_fixtures.py` from the case cache in `audit/` (run by hand;
  deterministic; fails if a real address or 64-hex string survives). Hyperliquid addresses -> `0xabab…NN`, Solana keys
  -> `fixture-key-NNN`; public program ids and mints stay (`ALLOWED_PUBLIC_KEYS`).
- **Mutants:** `scripts/mutants.sh` breaks each rule (one per classify rule at its boundary, the timing and data
  rules, paging, cache, the decoders, the URL guard) and requires its test to go red. A survivor fails the build.
  Runs with `PYTHONDONTWRITEBYTECODE=1` and clears `__pycache__` per mutant (DEBUG D-1).
- **Repo checks:** `scripts/checks.py` (no secrets, CLAUDE.md size, fixture keys, no 64-hex, no real case address
  outside `audit/`, no source file git-ignored), each with `--self-test`.
