# CLAUDE.md — future-trading-solana-coins (the `solana-future-coin` bot + the wallet tracer)

A futures copy-trading bot: it grades public traders on **Jupiter Perps (Solana)** and copies their opens onto
**KuCoin USDT-M perpetuals**, with its own exits. Python 3.12, two programs (SCOUT + TRADER, D-30) in two
containers from one image, on its own t4g.micro in Frankfurt. Paper first; paper is treated exactly like real money. Next to it, a read-only **wallet tracer** (laptop tool, PLAN.md) that
follows the money behind a wallet. Split on 2026-11-14 from `future-trading-coins` (now `future-trading-kucoins`).

## Docs — read the one you need

| File | Read it when |
|---|---|
| [.claude/docs/01-architecture.md](.claude/docs/01-architecture.md) | You need the module map, the loops, or why KuCoin trades but does not supply wallets |
| [.claude/docs/02-strategy.md](.claude/docs/02-strategy.md) | You touch Jupiter grading, selection, gates, sizing, exits or the go-live gates |
| [.claude/docs/03-config.md](.claude/docs/03-config.md) | You add a setting or a secret key, or switch MODE |
| [.claude/docs/04-deploy.md](.claude/docs/04-deploy.md) | You touch `infra/`, the workflows, host scripts, or deploy for the first time |
| [.claude/docs/05-testing.md](.claude/docs/05-testing.md) | You add or change a test, or a mutant survives |
| [.claude/docs/06-runbook.md](.claude/docs/06-runbook.md) | You operate the box, read status, use the kill switch, or go live |
| [.claude/docs/07-changelog.md](.claude/docs/07-changelog.md) | You want what changed and when |
| [.claude/docs/08-data.md](.claude/docs/08-data.md) | You read or change the Parquet data in the shared bucket |
| [.claude/docs/09-wallet-tracer.md](.claude/docs/09-wallet-tracer.md) | You run or change the wallet tracer (laptop tool; never imported by the bot) |
| [PLAN.md](PLAN.md) | The tracer's brief; phases 0-5 built. Phase 6 (tracer -> wallet selection) is next (D-28) |
| `BUG.md` / `DEBUG.md` / `LESSON.md` / `DECISIONS.md` | Open defects / symptom->cause->guard / lessons / decisions with "revisit when" |

## Current state (2026-11-19)

- **Two programs (D-30, user: "Split + websocket"), running since 2026-11-19 (`e9f94dc`):** the SCOUT (`python -m app.scout`,
  `solana-future-coin-scout`, /health 8080) finds, grades and picks, writes `data/scout.db`, never trades or sends
  Telegram. The TRADER (`python -m app.trader`, `solana-future-coin-trader`, /health 8081) follows the picked wallets
  on a Solana websocket (+ a 30-s poll), copies, exits, keeps the ledger, sends Telegram, writes
  `data/solana-future-coin.db`. Copy delay: about 1-3 s instead of about 30 s. B-1 closes when `statusCheck.sh` ends
  `ALL OK` with both containers; B-27 watches the websocket on Chainstack.
- The box was rebuilt on 2026-11-18 (D-26 reversed, D-28): new IP, a fresh $10,000 paper account.
- **Wallet rules (D-31, 2026-11-19, from `tools/criteria_backtest.py`):** biggest single win <= 50% of the profit,
  win rate >= 40%, a close in the last 14 days. Old pool 93 wallets (pooled PF 1.12 / 0.98 / 1.44 in the backtest
  windows), new pool 46 (1.93 / 1.34 / 1.90). The shadow copies decide whether it holds (B-31).
- **D-30 verified on the box** (10:17 UTC backups): seed 1,336 owners, first websocket OPEN 0.9 s after the wallet's.
- **Next (D-28, user):** change how wallets are found: start from Hyperliquid holder wallets (big PnL, $0 volume),
  follow the money back with the tracer to the hidden bot wallets, and copy what those bots trade. Not built yet.
- **Mode:** paper, account `paper-1`, $10,000, 3x isolated, max 10 positions, 6% margin per full-weight trade.
- **Secret `solana-future-bot`** (still in AWS): the bot's keys, and the tracer's Telegram token + Solana URL (D-27).
  **Live is blocked** (B-3): the KuCoin key is the KuCoin bot's. `MODE=live` refuses to boot until
  `KUCOIN_KEY_DEDICATED=true`.
- **Wallet pool (D-29, 2026-11-18):** every recorded owner's past from Jupiter's free trade API (chain = fallback,
  20k Chainstack calls/day); graded from 8 episodes (stronger proof below 15, weight <= 3), re-graded daily; GOLD +
  SILVER (weight <= 3), shadow copies decide the order. Measured: 5 -> 30 passing of 317 live owners.
- **Telegram:** the trader reports every 45 min, ALWAYS (`[PAPER] Jupiter-KuCoin`); the tracer sends one summary per
  run. The scout sends nothing.
- **Tracer:** `python -m app.tools.wallet_trace <addr> --chain hl|sol`; reads its secret with the user's AWS login.
- Checks: `./precommit.sh` prints its own counts (tests, mutants); that output is the source of truth, not this file.

## Rules that must be obeyed every session

1. **Never use local AWS (or any cloud) credentials.** No `aws`, no remote `terraform`, never run the tracer without
   `--no-aws`. Ask the user to run it. **Never put a token, key, credential or URL with a key in git** — and no
   key-LIKE string either (public addresses, signatures, 64-hex hashes in fixtures: placeholders). `scripts/checks.py`
   enforces it (DEBUG D-6, `case-addresses`).
2. **Git only when the user asks.** Commit/push as `Tafehi` / `ehsan.tafehi@gmail.com` (set locally; check before
   every push); never the `@dnb.no` address, no Claude co-author line. **A push to main deploys** (terraform apply).
3. **Paper until the user flips it.** The bot never goes live by itself; `MODE=live` needs the passphrase, a start
   balance and a passing pre-flight. Do not weaken `_live_needs_everything` or `live_preflight`. Not before B-3 closes.
4. **Paper = live except the executor** (D-3). Any change to gates, sizing, exits or the ledger applies to both. Paper
   must keep refusing what KuCoin refuses (`executor.check_rules`) and filling on a fresh book.
5. **Telegram (the trader) sends only the 45-min report and alarms** (D-8, D-24); every bot message carries
   `BOT_NAME` (D-21). The scout never sends (tested). The tracer's summary uses short addresses only.
6. **`./precommit.sh` must print ALL STAGES PASS before any commit, and `scripts/verify_clone.sh` before any push.**
7. **Bug loop:** grep DEBUG.md / LESSON.md for the symptom first. Every fix ships a test that fails on the old code
   (add a mutant to `scripts/mutants.sh`). Test boundaries at the boundary value.
8. **Verify external APIs against current docs or real data before relying on them.** Record what was checked and when
   in the module docstring.
9. **Docs ship with the change:** topic doc, changelog, and BUG/DEBUG/LESSON/DECISIONS as relevant.
10. **Own names only** (D-19, D-22): every AWS name starts `solana-future-coin`, every recorded folder is `jupiter/` or
    `solana-bot/`.
11. **The tracer stays separate until D-28 is built:** the bot never imports `app.tools.wallet_trace` (tested). Never
    log an RPC URL or the Telegram token.
12. **Ask, don't assume** when a request is unclear — and ask AGAIN when an answer does not pick an option, before
    anything destructive (LESSON L-13).
13. **Two programs, two files (D-30):** each program writes only its own SQLite file; every read of the other's file
    goes through `app/db/crossread.py`, read-only. The scout loads no trading or Telegram code, the trader no grading
    or history code (tested). Compose picks the program with `entrypoint:`, never `command:` (LESSON L-14; checked).

Keep this file under 40,000 characters (target under 10,000; `scripts/checks.py` enforces the hard limit).
