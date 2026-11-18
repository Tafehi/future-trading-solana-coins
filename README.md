# future-trading-solana-coins

A futures copy-trading bot ("solana-future-coin"). It grades public traders on Jupiter Perps (Solana) and copies their trades onto
KuCoin USDT-M perpetual futures, with its own exits. It runs in paper mode first, with paper treated exactly like real
money, and reports to Telegram every 15 minutes when it traded (stamped `[PAPER] Jupiter-KuCoin`).

Split on 2026-11-14 from `future-trading-coins`, which is now `future-trading-kucoins` (the Hyperliquid -> KuCoin
bot). Each bot has its own EC2 box and its own secret (`solana-future-bot` here).

- Start here: [CLAUDE.md](CLAUDE.md) (index, current state, rules).
- Check everything locally: `./precommit.sh` (needs `python3.12 -m venv .venv && .venv/bin/pip install -r app/requirements-dev.txt`, terraform, shellcheck).
- Deploy: push to `main` after the one-time steps in [.claude/docs/04-deploy.md](.claude/docs/04-deploy.md).
- Operate: [.claude/docs/06-runbook.md](.claude/docs/06-runbook.md).

Also here: a read-only **wallet tracer** for the laptop (`python -m app.tools.wallet_trace <addr>`), see
[.claude/docs/09-wallet-tracer.md](.claude/docs/09-wallet-tracer.md).

No result here is real money until `MODE=live`; every paper message says `[PAPER]` and "SIMULATED".
