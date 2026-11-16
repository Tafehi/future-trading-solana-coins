# CLAUDE.md — future-trading-solana-coins (the `solana-future-coin` bot)

A futures copy-trading bot: it grades public traders on **Jupiter Perps (Solana)** and copies their opens onto
**KuCoin USDT-M perpetuals**, with its own exits. Python 3.12, one container, its own t4g.micro in Frankfurt. Paper
first; paper is treated exactly like real money. Split on 2026-11-14 from `future-trading-coins` (now
`future-trading-kucoins`, the Hyperliquid -> KuCoin bot, `../future-trading-coins` locally); DECISIONS D-19.

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
| `BUG.md` / `DEBUG.md` / `LESSON.md` / `DECISIONS.md` | Open defects / symptom->cause->guard / lessons / decisions with "revisit when" |

## Current state (2026-11-16)

- **Box up 2026-11-16:** `solana-future-coin` `i-05ad4a52b74d57842` (eu-central-1a, IP 3.70.194.183), t4g.micro.
- **First push 2026-11-16.** Every AWS name is `solana-future-coin-*` (user, 2026-11-16; was `futsol-bot`), on a
  **t4g.micro** (user). aws-foundation **B-34** applied and Pump-fun-S3 D-129 pushed (user, 2026-11-16) before it.
  B-1 closes when `statusCheck.sh` on the new box ends `ALL OK`.
- **Mode:** paper, account `paper-1`, $10,000, 3x isolated, max 10 positions, 6% margin per full-weight trade.
- **Secret `solana-future-bot`** (user, 2026-11-16): this bot's own; the old shared secret is deleted. Until it exists
  the box keeps its last `.env` (B-9). It still holds the SAME KuCoin account key as the KuCoin bot. Paper makes no
  signed KuCoin call, so the new box's IP is fine. **Live is blocked** (B-3): the key is IP-locked to the KuCoin box and both bots would share one
  KuCoin account. `MODE=live` refuses to boot until `KUCOIN_KEY_DEDICATED=true` (user: "remind me ... I will add
  another api key for solana"); the go-live check prints the reminder. `SOLANA_RPC_URL` is this bot's only source: without it, nothing happens (B-5).
- **Recordings:** `futures/jupiter/` and `futures/solana-bot/` in the shared bucket. Uploads work once Pump-fun-S3 D-129
  is pushed (it lists `solana-future-coin-role`); the bucket needs a new owner when that stack goes ~2026-12-06 (B-4).
- **Evidence:** `scripts/collect_evidence.sh` (run by the user, read-only) downloads the database, box logs
  (`statusCheck.sh --evidence`) and this bot's Parquet; `LEGACY=1` takes the old futcoins database with the Jupiter
  history from before the split. `app/tools/evidence_report.py` writes every table as CSV + `summary.md`.
- **Wallet pool:** Jupiter GOLD + SILVER (weight <= 3), chain history (20k calls/day, B-15), shadow copies decide the
  order. `wallets_report` shows it.
- **Telegram:** same chat as the KuCoin bot. A digest every 15 min only when something was traded, plus alarms. Every
  message is stamped `[PAPER] Jupiter-KuCoin` (`BOT_NAME`).
- Checks: `./precommit.sh` prints its own counts (tests, mutants); that output is the source of truth, not this file.

## Rules that must be obeyed every session

1. **Never use local AWS (or any cloud) credentials.** No `aws`, no remote `terraform`. Ask the user to run it.
   **Never put a token, key, credential or URL with a key in git** — and no key-LIKE string either (public addresses,
   signatures, 64-hex hashes in fixtures: placeholders). `scripts/checks.py` enforces it (DEBUG D-6).
2. **Git only when the user asks.** Commit/push as `Tafehi` / `ehsan.tafehi@gmail.com` (set locally; check before
   every push); never the `@dnb.no` address, no Claude co-author line.
3. **Paper until the user flips it.** The bot never goes live by itself; `MODE=live` needs the passphrase, a start
   balance and a passing pre-flight. Do not weaken `_live_needs_everything` or `live_preflight`. Not before B-3 closes.
4. **Paper = live except the executor** (D-3). Any change to gates, sizing, exits or the ledger applies to both. Paper
   must keep refusing what KuCoin refuses (`executor.check_rules`) and filling on a fresh book.
5. **Telegram sends only real fills (the 15-min digest) and alarms** — no signals, refusals or wallet lists (D-8).
   Every message carries `BOT_NAME` (D-21).
6. **`./precommit.sh` must print ALL STAGES PASS before any commit, and `scripts/verify_clone.sh` before any push**
   (a fresh clone is what CI sees; DEBUG D-4).
7. **Bug loop:** grep DEBUG.md / LESSON.md for the symptom first. Every fix ships a test that fails on the old code
   (add a mutant to `scripts/mutants.sh`). Test boundaries at the boundary value.
8. **Verify external APIs against current docs or real data before relying on them** (KuCoin endpoints, Jupiter
   layout: DEBUG D-2). Record what was checked and when in the module docstring.
9. **Docs ship with the change:** update the topic doc, the changelog, and BUG/DEBUG/LESSON/DECISIONS as relevant.
10. **Own names only** (D-19, D-22): every AWS name starts `solana-future-coin`, every recorded folder is `jupiter/`
    or `solana-bot/`. Never reuse a `kucoin-future-coin` (or old `futcoins-bot`) name: that is the other bot's.
11. **Ask, don't assume** when a request is unclear. When the task is clear, just do it.

Keep this file under 40,000 characters (target under 10,000; `scripts/checks.py` enforces the hard limit).
