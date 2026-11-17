# CLAUDE.md — future-trading-solana-coins (the wallet tracer)

A **read-only research tool for the laptop**: it follows the money behind a wallet on Hyperliquid and Solana (futures
and spot), classifies the wallet, links same-owner wallets, and writes a report. It never trades. Python 3.12. The
Jupiter -> KuCoin copy-trading bot that lived here until 2026-11-17 was removed (D-26; git history, commit `7437aec`).

## Docs — read the one you need

| File | Read it when |
|---|---|
| [.claude/docs/09-wallet-tracer.md](.claude/docs/09-wallet-tracer.md) | You run or change the tracer: modules, rules learned on real data, tests |
| [PLAN.md](PLAN.md) | You need the brief: goals, the case study, classify and twin rules, phases (0-5 built; 6 is the user's call) |
| [.claude/docs/01-architecture.md](.claude/docs/01-architecture.md) | You need the module map and the data flow |
| [.claude/docs/05-testing.md](.claude/docs/05-testing.md) | You add or change a test or fixture, or a mutant survives |
| [.claude/docs/07-changelog.md](.claude/docs/07-changelog.md) | You want what changed and when (including the removed bot's history) |
| `BUG.md` / `DEBUG.md` / `LESSON.md` / `DECISIONS.md` | Open items / symptom->cause->guard / lessons / decisions with "revisit when" |

## Current state (2026-11-17)

- **Tracer built** (PLAN phases 0-5): `python -m app.tools.wallet_trace <addr> --chain hl|sol --depth 2 --top 3`.
  Reproduces the PLAN §2.2 case (no bot found there). Hyperliquid public API and the free Solana RPC; no key.
- **Old bot removed** (user, 2026-11-17): code, Terraform, workflows and host scripts are gone; a push changes nothing
  in AWS. Its AWS resources were destroyed by the user on 2026-11-17 (21 resources); only the hand-made secret
  `solana-future-bot` is left to delete (BUG B-18). Nothing here reads it.
- **Telegram + secrets (D-27):** every run sends one summary to Telegram. Token, chat id and the Chainstack
  `SOLANA_RPC_URL` come from the secret `solana-future-bot` (eu-central-1), read with the user's AWS login; env vars
  win; `--no-telegram` / `--no-aws` turn them off. The secret must stay (BUG B-18 closed: kept on purpose).
- Output and cache: `audit/wallet_trace/` (git-ignored). Labels: `audit/wallet_trace/labels.toml`.
- Checks: `./precommit.sh` prints its own counts (tests, mutants); that output is the source of truth, not this file.

## Rules that must be obeyed every session

1. **Never use local AWS (or any cloud) credentials yourself.** No `aws`, no remote `terraform`, and never run the
   tracer without `--no-aws` (it reads the secret with the user's login). Ask the user to run it. **Never put a token,
   key, credential or URL with a key in git.** The secret is read only (`get_secret_value`); a test enforces it.
2. **No real address, signature or transaction hash in git**, not even in docs: short forms only (`0x8747…`). Real
   data lives in `audit/`. `scripts/checks.py` enforces it (`fixture-keys`, `hex64`, `case-addresses`; DEBUG D-6).
   Fixtures are built only by `scripts/make_trace_fixtures.py` (anonymised).
3. **Read-only.** The tool never signs, never trades, never writes to any chain or exchange; its only outward write
   is the Telegram summary. `test_no_trading_or_bot_code_comes_back` fails if trading code comes back.
4. **Git only when the user asks.** Commit/push as `Tafehi` / `ehsan.tafehi@gmail.com` (set locally; check before
   every push); never the `@dnb.no` address, no Claude co-author line.
5. **`./precommit.sh` must print ALL STAGES PASS before any commit, and `scripts/verify_clone.sh` before any push.**
6. **Bug loop:** grep DEBUG.md / LESSON.md for the symptom first. Every fix ships a test that fails on the old code
   (add a mutant to `scripts/mutants.sh`). Test boundaries at the boundary value.
7. **Verify external APIs against current docs AND one real answer** before relying on them. Record what was checked
   and when in the module docstring (`hl_source.py`, `sol_source.py`).
8. **Docs ship with the change:** doc 09, the changelog, PLAN.md status, and BUG/DEBUG/LESSON/DECISIONS as relevant.
9. **Never log an RPC URL or the Telegram token** (both carry keys in the URL). Errors keep the HTTP status or the
   exception type only. Telegram gets short addresses only; full addresses stay in `audit/`.
10. **Before using any traced wallet for real-money copying**, remind the user the copy bot is gone and that this
    needs its own decision (PLAN phase 6).
11. **Ask, don't assume** when a request is unclear. When the task is clear, just do it.

Keep this file under 40,000 characters (target under 10,000; `scripts/checks.py` enforces the hard limit).
