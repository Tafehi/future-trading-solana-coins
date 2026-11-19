# 02 — Strategy: who is copied, how much, and how it exits

Back to the index: [CLAUDE.md](../../CLAUDE.md). Every number below is a setting; `app/config.py` and
`app/settings.toml` are the source of truth, not this page.

Jupiter Perps is this bot's only source since the split (2026-11-14, D-19). The Hyperliquid rules (leaderboard
prefilter, trade stream, 90-day fills, `live_check`) stayed in `future-trading-kucoins`.

## Jupiter Perps wallets — the SCOUT (`jobs/jupiter.py`, `jobs/jupiter_history.py`, D-6, D-7, D-14, D-30)

- Recorded from the change feed: every open and close >= $1,000; a close becomes an episode valued at the KuCoin MARK
  of the same coin at that moment (BUG B-7).
- **History for every owner (item 1; since 2026-11-18 from Jupiter's trade API, D-29):** every recorded owner, biggest
  first, 80 a minute, gets its last 90 days read from `perps-api.jup.ag/v2/trades` (one free call per 1,000 trades;
  exact PnL and every fee, open and liquidation fees included). Those episodes replace the feed's estimates (and an
  older chain read) and count as history, so the owner is graded at once. Only when the API fails is the chain read
  (close events in the Position accounts' transactions), capped at `JUPITER_HISTORY_CALLS_PER_DAY` (B-15).
- Graded once the history covers 1 day and holds **8** closed episodes (was 14 days and 15, D-29). Below 15 episodes
  the proof must be stronger: the needed PF is multiplied by sqrt(15 / episodes) (8: GOLD 2.46, SILVER 1.92; 10: 2.20,
  1.71; 12: 2.01, 1.57), a record with no loss counts PF = its episode count (not the 99 cap), and the copy weight is
  capped at 3. Re-graded after every new close and at least every 24 h. Measured on 317 live owners (2026-11-18):
  5 GOLD/SILVER under the old rules with the old history, 26 with history for all, 30 with the 8-episode rule. No public account value, so drawdown = largest fall
  of cumulative episode PnL / the owner's largest position. GOLD = PF >= 1.8, DD <= 35%, total > 0, no single episode
  above `MAX_BIGGEST_WIN_SHARE`, not a scalper. SILVER (PF >= 1.4, DD <= 50%) copied at weight 3 at most (item 5).
  Grade score and copy weight use the shared rules in `core/wallet_grader.py`. Best 20 tracked, re-graded and re-picked every
  `JUPITER_GRADE_MINUTES` (5 min since 2026-11-13, was hourly). Coins: SOL, ETH, BTC only.

## Selection: tracked vs watched (`core/selection.py`, items 4 and 5)

1. Eligible: GOLD and SILVER. 2. A wallet with 5+ shadow copies in 14 days and a mean <= 0% is NOT tracked (copying
it loses), only watched. 3. Order: proven wallets (5+ copies, mean > 0) by mean, then GOLD by grade score, then SILVER.
4. Tracked = the first `JUPITER_MAX_TRACKED` (20), at most `MAX_TRACKED_SILVER` (10) SILVER. The rest are watched
(the trader's websocket follows them too, for fast shadow entries). Re-picked after each grading, every 5 min. The
pick writes `jup:picked_at` with the flags; when the trader's shadow record cannot be read, the pick FAILS CLOSED (the
old flags and time stay, retried in 60 s), so a broken read never re-tracks a wallet whose copies lose.

## The trader's feed: from an open on Jupiter to a signal (`trading/watch.py`, `data/solana_ws.py`, D-30)

- The trader reads the copy list (tracked + watched) from the scout's file once a minute and subscribes one Solana
  `programSubscribe` per owner (filters `dataSize 216` + `memcmp offset 8 = owner`, commitment `confirmed`), tracked
  first by grade, up to `SOLANA_WS_MAX_SUBSCRIPTIONS` (100). A change reaches it 1.3 s p50 / 1.9 s max after the fill
  (measured 2026-11-18). A poll every `FEED_POLL_SECONDS` (30) re-reads the listed owners' known accounts with
  `getMultipleAccounts` (never `getProgramAccounts`, which Chainstack caps at 3 per 20 s): the safety net.
- `decide()`: OPEN when the account was flat, unknown or dust and is now open >= $1,000 AND the open is fresh
  (now - openTime <= `SIGNAL_MAX_AGE_S`, 60 s). Subscribing sends no snapshot, so freshness, not a baseline, decides:
  an old open first seen now is state only, and an open made during a restart is still copied when young. An account
  whose openTime changed between two looks = CLOSE of the old open + the OPEN rule for the new one. Open -> 0 = CLOSE
  (record only). Adds / reduces: state only. The state moves only forward by (updateTime, slot); a repeat is ignored.
- A signal is written only for an owner on the CURRENT list; a unique index on (account, openTime, kind) makes a
  second signal for one open impossible (socket + poll + restart). The signal keeps `via` (ws / poll) and `open_time`,
  so the copy delay is measured: `ts - open_time` (statusCheck 4c). A written signal wakes the copier at once.

## Shadow copies (`trading/shadow.py`, item 4)

Every OPEN of a tracked or watched wallet that passes the market and probe gates gets a shadow copy: entry at the
best ask/bid, our exits on the market cache's mark prices, return = price % in our favour minus two taker fees
(liquidation = -33%). No money, no Telegram. `python -m app.tools.wallets_report` shows the record.

## From signal to position (`trading/copier.py`)

OPEN only (flat -> open); ADD / REDUCE ignored; CLOSE recorded only. Refusals, in order: STALE (> 60 s), LIST_STALE
(the scout's last pick is older than `COPYLIST_MAX_AGE_S`, 600 s, or never happened: an old list proves nothing;
exits of open copies continue), NOT_TRACKED (not on the CURRENT copy list as GOLD/SILVER: a wallet the scout dropped
gets no new copy, its open copies keep their exits), NOT_LISTED, (shadow copy here; a watched wallet stops with SHADOW_ONLY), market gate (LOW_TURNOVER < $500k, NO_BOOK, SPREAD > 15 bps, ENTRY_DRIFT > 1.5%, FUNDING_AGAINST > 0.1%),
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
