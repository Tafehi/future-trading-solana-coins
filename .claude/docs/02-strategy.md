# 02 — Strategy: who is copied, how much, and how it exits

Back to the index: [CLAUDE.md](../../CLAUDE.md). Every number below is a setting; `app/config.py` and
`app/settings.toml` are the source of truth, not this page.

Jupiter Perps is this bot's only source since the split (2026-11-14, D-19). The Hyperliquid rules (leaderboard
prefilter, trade stream, 90-day fills, `live_check`) stayed in `future-trading-kucoins`.

## Jupiter Perps wallets (`jobs/jupiter.py`, `jobs/jupiter_history.py`, D-6, D-7, D-14)

- Recorded from the change feed: every open and close >= $1,000; a close becomes an episode valued at the KuCoin MARK
  of the same coin at that moment (BUG B-7).
- **History from the chain (item 1):** owners with >= $10k open, biggest first, get their last 90 days read from their
  Position accounts' transactions; the close events carry the real PnL and fees. Those episodes replace the feed's
  estimates and count as history, so the owner is graded at once. Capped at `JUPITER_HISTORY_CALLS_PER_DAY` (B-15).
- Graded once the history (recorded or read from the chain) covers 14 days and holds 15 closed episodes. No public account value, so drawdown = largest fall
  of cumulative episode PnL / the owner's largest position. GOLD = PF >= 1.8, DD <= 35%, total > 0, no single episode
  above `MAX_BIGGEST_WIN_SHARE`, not a scalper. SILVER (PF >= 1.4, DD <= 50%) copied at weight 3 at most (item 5).
  Grade score and copy weight use the shared rules in `core/wallet_grader.py`. Best 20 tracked, re-graded and re-picked every
  `JUPITER_GRADE_MINUTES` (5 min since 2026-11-13, was hourly). Coins: SOL, ETH, BTC only.

## Selection: tracked vs watched (`core/selection.py`, items 4 and 5)

1. Eligible: GOLD and SILVER. 2. A wallet with 5+ shadow copies in 14 days and a mean <= 0% is NOT tracked (copying
it loses), only watched. 3. Order: proven wallets (5+ copies, mean > 0) by mean, then GOLD by grade score, then SILVER.
4. Tracked = the first `JUPITER_MAX_TRACKED` (20), at most `MAX_TRACKED_SILVER` (10) SILVER. The rest are watched
(free: the change feed sees every owner). Re-picked after each grading, every 5 min.

## Shadow copies (`trading/shadow.py`, item 4)

Every OPEN of a tracked or watched wallet that passes the market and probe gates gets a shadow copy: entry at the
best ask/bid, our exits on the market cache's mark prices, return = price % in our favour minus two taker fees
(liquidation = -33%). No money, no Telegram. `python -m app.tools.wallets_report` shows the record.

## From signal to position (`trading/copier.py`)

OPEN only (flat -> open); ADD / REDUCE ignored; CLOSE recorded only. Refusals, in order: STALE (> 60 s), NOT_TRACKED,
NOT_LISTED, (shadow copy here; a watched wallet stops with SHADOW_ONLY), market gate (LOW_TURNOVER < $500k, NO_BOOK, SPREAD > 15 bps, ENTRY_DRIFT > 1.5%, FUNDING_AGAINST > 0.1%),
SMALL_PROBE, account (HALTED, PAUSED, SYMBOL_HELD, MAX_POSITIONS 10 — a wallet scoring >= 1.0 higher REPLACES the
lowest-score copy), TOO_SMALL, INSUFFICIENT_MARGIN, then the executor's own refusals.

## Sizing

`margin = equity x 6% x size_share(weight)`, `notional = margin x 3` (isolated), rounded DOWN to whole lots.

## Paper treated exactly as real (`trading/paper_executor.py`, `executor.check_rules`)

KuCoin's rules refuse the same orders (lot, max order qty, max leverage, first risk-limit level). The fill waits
`PAPER_FILL_DELAY_S`, reads a FRESH depth20 book and walks it (refused above 30 bps slippage or on a thin book) with
the contract's taker fee. Exits walk a fresh book too and never refuse. Funding = KuCoin's settled rates.
Liquidation by MARK price at the contract's maintenance margin.

## Exits (`futures_paper.exit_reason`, every 5 s, by MARK price)

1. liquidation 2. stop at -6% price 3. trailing: arms at +6%, closes after a 3% giveback 4. max hold 72 h.
Live: the same stop also sits on KuCoin as a reduce-only stop-market (mark price), so it works if the bot is down.
These exits are ai-kucoin-future's best of 7 settings on one window (BUG B-6).

## No account-level stop rules

No daily-loss or drawdown stop (user, 2026-11-13: review after 2-3 weeks of paper). BUG B-3.

## Go-live (`core/golive.py`, `python -m app.tools.golive_check`)

30+ closed trades on 7+ days from 3+ wallets; banked > 0 after fees and funding; banked minus the best trade > 0;
PF >= 1.3; no liquidation, never halted. The bot never goes live by itself.
