# 05 — Testing

Back to the index: [CLAUDE.md](../../CLAUDE.md).

- **One command:** `./precommit.sh` (exit code = failed stages; `MUTANTS=0` skips the mutants for speed). The same
  stages run in CI.
- **Offline:** `app/tests/conftest.py` blocks internet sockets and DNS. Use `httpx.MockTransport` or the fakes in
  `app/tests/helpers.py`. `fresh_db` gives each test its own migrated database and seeded $10k paper account.
- **Fixture rule (DEBUG D-6):** keep only the bytes a test reads; no free-standing addresses, signatures or 64-hex hashes
  (placeholders instead). `scripts/checks.py fixture-keys / hex64` enforce it.
- **Real data fixtures (2026-11-13):** `jupiter_positions.json` (three Position accounts), `jupiter_close_tx.json`
  and `jupiter_more_txs.json` (real closing transactions + one failed one). The KuCoin REST fixtures and
  `hl_fills.json` / `hl_clearinghouse.json` (the shared grading types) came from ai-kucoin-future. The Hyperliquid
  leaderboard, meta, portfolio and trade-stream fixtures and their tests left with that source (2026-11-14).
- **Mutants:** `scripts/mutants.sh` breaks each key rule (drift tolerance, live refusal, contract rules, fresh-book
  delay, signing, digest once-only, funding once, Jupiter diff and layout, replace gap, staleness, stop boundary,
  recorder cap and atomic write, shadow/selection/SILVER rules, Jupiter history budget; since the split also the
  `BOT_NAME` stamp, the `solana-bot/` folders, the Jupiter coin map, and every wallet-tracer rule) and requires its test to go red. A survivor fails the build. Runs with
  `PYTHONDONTWRITEBYTECODE=1` and clears `__pycache__` per mutant (DEBUG D-1).
- **Boundaries are tested at the value** (`<=` vs `>`): stop at exactly 6%, risk limit exactly at the level, drift
  exactly $0.01, replace gap exactly 1.0, alarm repeat exactly 3600 s.
- **Wallet tracer** (doc 09): golden test from the anonymised case study, every classify rule at its boundary, one
  mutant per rule, Telegram + Secrets Manager on fakes. Fixtures come from `scripts/make_trace_fixtures.py` (run by
  hand from `audit/`). The bot never imports the tracer (subprocess test).
- **Repo checks:** `scripts/checks.py` (user-data size, heredocs parse, no secrets, CLAUDE.md size, no `environment:`,
  `evidence-read-only`, `host-guards`, `case-addresses`), each with `--self-test`.
- **Before every push:** `scripts/verify_clone.sh` runs the checks and tests on a fresh clone of HEAD (DEBUG D-4).
