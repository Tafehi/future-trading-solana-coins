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
  exactly $0.01, replace gap exactly 1.0, alarm repeat exactly 3600 s; since D-30 also freshness exactly 60 s,
  size exactly $1,000, copy-list age exactly 600 s, websocket down exactly 300 s, backoff reset after exactly 60 s up.
- **The two programs (D-30):** `test_programs.py` (subprocess imports: the scout loads no trading / Telegram code,
  the trader no grading / history code; the runtime's `wake`; the settings that keep the files apart; the pick's time
  and fail-closed retry; the trader end to end from the scout's file to a paper position), `test_position_feed.py`
  (`decide()` and the once-only signal), `test_solana_ws.py` (a fake socket: subscribe shape, diff, reconnect,
  backoff, pong, the URL never logged), `test_copylist.py` (read-only cross reads, LIST_STALE / NOT_TRACKED /
  SHADOW_ONLY, a dropped wallet's copy still exits), `test_health.py` (each program's loops = its limits). Copier
  tests use `helpers.TableCopyList` (reads the test database's `wallets` on every lookup). Accounts in websocket tests
  are a fixture account with rewritten fields (`helpers.position_account`), so no address is typed.
- **The wallet-rule backtest** (`test_criteria_backtest.py`): no look-ahead tested AT the cutoff (closed at the
  cutoff = not yet known; opened at it = a copy), the fee formula, pooled numbers by hand, the bit masks equal to the
  plain rule check on 400 random rule sets, "robust" needs every fold, today's rules read from settings, the file is
  only read. 6 mutants.
- **A survivor can mean the test is weak** (LESSON L-15): build a "same path, different spelling" input as a plain
  string (`pathlib` drops `/./` by itself), and keep one owner per invariant.
- **Wallet tracer** (doc 09): golden test from the anonymised case study, every classify rule at its boundary, one
  mutant per rule, Telegram + Secrets Manager on fakes. Fixtures come from `scripts/make_trace_fixtures.py` (run by
  hand from `audit/`). The bot never imports the tracer (subprocess test).
- **Repo checks:** `scripts/checks.py` (user-data size, heredocs parse, no secrets, CLAUDE.md size, no `environment:`,
  `evidence-read-only`, `host-guards` (incl. backup and restore of both files), `compose` (`entrypoint:` only, no image
  ENTRYPOINT, LESSON L-14), `case-addresses`), each with `--self-test`.
- **Before every push:** `scripts/verify_clone.sh` runs the checks and tests on a fresh clone of HEAD (DEBUG D-4).
