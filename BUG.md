# BUG.md — open and deferred items

What is broken or deliberately unfinished **right now**. Fixed bugs go in `DEBUG.md`. Back to [CLAUDE.md](CLAUDE.md).

Fresh register since the copy-trading bot was removed (2026-11-17, D-26). The bot's rows (B-1 to B-17) closed with it;
they are in git history.

| B-n | Status | What | Why open | Closes when |
|---|---|---|---|---|
| B-18 | CLOSED 2026-11-17 | The secret `solana-future-bot` stays on purpose: the tracer reads its Telegram token and Solana URL (D-27). Was: only the secret is left. **`terraform destroy` done 2026-11-17 by the user: 21 resources gone** (box, bucket with its backups, ECR repo, role, profile, security group, policies). `prevent_destroy` was turned off and `force_destroy` / `force_delete` set in the local copy `audit/old-bot/infra` only | Nothing in this repo reads the secret (the tracer takes `--rpc-url`); it was made by hand, so Terraform could not remove it | The user deleted `solana-future-bot` in Secrets Manager |
| B-19 | WATCH | Arbitrum (after a Hyperliquid withdraw) and HyperEVM (after a send to a system address) are not followed | Out of scope for v1 (PLAN §4.3); the Arbitrum explorer blocks scripts | The report's explorer links are not enough for the user |
| B-20 | WATCH | Solana verdicts read only the newest 100 transactions (plus the 3 oldest) of a wallet | The free public RPC throttles at ~1 call a second; marked `partial` (unsure) in the report | A paid RPC or Helius key is available (`--rpc-url`) and the window is raised in `settings.py` |
| B-21 | DEFERRED | Classify and twin thresholds are fitted to one case study | 11+ Hyperliquid wallets, 2 Solana wallets (2026-11-17) | A second case with a known bot confirms or moves them |
| B-22 | WATCH | `DecreasePositionEvent` / `LiquidateFullPositionEvent` (Jupiter keeper closes and liquidations) are decoded from the IDL only | Every close in the fixtures is an `InstantDecreasePositionEvent` | A real keeper close or liquidation decodes to the right owner and PnL |
| B-23 | WATCH | Reading the secret was never run against AWS by the assistant (CLAUDE.md rule 1) | Tested on a fake Secrets Manager; the boto3 call and the secret's JSON shape match the docs and the removed bot's `hostSetup.sh` | The user's first run prints `Telegram: summary sent` and `Solana RPC: SOLANA_RPC_URL from secrets-manager:solana-future-bot` |
| B-24 | WATCH | The Chainstack pace (4 calls/s, 300 tx per wallet) comes from the 2026-11-13 limits page, not a new reading | Chainstack's plan may have changed; a 429 is retried with backoff | A Chainstack Solana trace runs without 429 lines, or the user lowers `SOL_CALLS_PER_SECOND_PAID` |

