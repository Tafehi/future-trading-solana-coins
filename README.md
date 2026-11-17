# future-trading-solana-coins — wallet tracer

A **read-only research tool** that follows the money behind a crypto wallet, on **Hyperliquid** and **Solana**, for
futures and spot. For any wallet it says:

1. what kind of wallet it is (holder, cash-out, pass-through, slow seller, spot trader, perp trader, likely bot);
2. where its money came from and where it went, N steps out;
3. which other wallets probably have the same owner ("twins");
4. whether it trades futures, spot or both.

It never trades, holds no key, and runs on a laptop. Real addresses stay in `audit/` (git-ignored).

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r app/requirements-dev.txt
.venv/bin/python -m app.tools.wallet_trace <address> --chain hl --depth 2 --top 3
.venv/bin/python -m app.tools.wallet_trace <solana-address> --chain sol
./precommit.sh        # every check; prints ALL STAGES PASS
```

- Start here: [CLAUDE.md](CLAUDE.md). The tool: [.claude/docs/09-wallet-tracer.md](.claude/docs/09-wallet-tracer.md).
  The brief: [PLAN.md](PLAN.md).
- Until 2026-11-17 this repo held a Jupiter -> KuCoin copy-trading bot. It was removed (D-26); it lives on in git
  history (last commit with it: `7437aec`) and in [.claude/docs/07-changelog.md](.claude/docs/07-changelog.md).
