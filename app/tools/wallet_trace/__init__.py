"""Wallet tracer: follow the money behind a wallet (futures AND spot, Hyperliquid AND Solana). PLAN.md is the brief.

A read-only research tool for the laptop. The bot never imports it (`test_wallet_trace_isolation.py` proves that), so
the bot's loops and call budgets cannot be touched by it. Output and the raw-answer cache live under `audit/`
(git-ignored), because they hold real addresses.
"""
