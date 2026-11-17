# BUG.md — open and deferred items

What is broken or deliberately unfinished **right now**. Fixed bugs go in `DEBUG.md`. Back to [CLAUDE.md](CLAUDE.md).

Fresh register since the copy-trading bot was removed (2026-11-17, D-26). The bot's rows (B-1 to B-17) closed with it;
they are in git history.

| B-n | Status | What | Why open | Closes when |
|---|---|---|---|---|
| B-18 | OPEN (user) | The old bot's AWS resources still exist (box `solana-future-coin`, its bucket, ECR repo, role, security group; secret `solana-future-bot`) | The code that managed them is removed; a push no longer touches AWS. A copy of the Terraform and its host files is in `audit/old-bot/` (git-ignored) for the destroy | The user ran `terraform destroy` from `audit/old-bot/infra` and deleted the secret by hand |
| B-19 | WATCH | Arbitrum (after a Hyperliquid withdraw) and HyperEVM (after a send to a system address) are not followed | Out of scope for v1 (PLAN §4.3); the Arbitrum explorer blocks scripts | The report's explorer links are not enough for the user |
| B-20 | WATCH | Solana verdicts read only the newest 100 transactions (plus the 3 oldest) of a wallet | The free public RPC throttles at ~1 call a second; marked `partial` (unsure) in the report | A paid RPC or Helius key is available (`--rpc-url`) and the window is raised in `settings.py` |
| B-21 | DEFERRED | Classify and twin thresholds are fitted to one case study | 11+ Hyperliquid wallets, 2 Solana wallets (2026-11-17) | A second case with a known bot confirms or moves them |
| B-22 | WATCH | `DecreasePositionEvent` / `LiquidateFullPositionEvent` (Jupiter keeper closes and liquidations) are decoded from the IDL only | Every close in the fixtures is an `InstantDecreasePositionEvent` | A real keeper close or liquidation decodes to the right owner and PnL |
