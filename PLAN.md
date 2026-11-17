# PLAN — Wallet tracer: follow the money behind a wallet (futures AND spot)

Back to the index: [CLAUDE.md](CLAUDE.md). Written 2026-11-17 from a manual case study done in the
`future-trading-kucoins` session. This file is the brief for the Claude Code session that builds the tool.
Read it top to bottom before writing code.

**Status 2026-11-17: phases 0-5 are built** (`app/tools/wallet_trace/`, D-25). What was built, how to run it and what
real data changed: [.claude/docs/09-wallet-tracer.md](.claude/docs/09-wallet-tracer.md). Changes to this plan found
while building are marked **(built: ...)** below. Phase 6 is open (the user decides).
**2026-11-17 later:** the copy-trading bot was removed from this repo (D-26). Wherever this plan says "the bot" (its
budgets, `main.py`, `jup_episodes`), that part is history; the tracer is now the whole repo.

## 1. Goal (why this exists)

The user saw "traders" on the Hyperliquid leaderboard with big PnL and **$0 volume**. Their idea: these are
**holder** wallets, and hidden **bot** wallets do the trading and send the profit to them. They want to
find those source wallets.

The tool must answer, for any wallet:

1. **What kind of wallet is it?** Holder, cash-out, pass-through, slow seller, spot trader, futures trader,
   likely bot.
2. **Where did its money come from, and where did it go?** Follow transfers in both directions, N steps.
3. **Is it linked to other wallets that have the same owner?** ("twins", shared funder, sub-accounts.)
4. **Does it trade futures, spot, or both?** And on which venue.

Two chains, futures and spot on each:

| Chain | Futures | Spot |
|---|---|---|
| Hyperliquid | perps (`Open Long`, `Close Short`, funding) | spot pairs (`@107` = HYPE/USDC, `PURR/USDC`, …) |
| Solana | Jupiter Perps (this repo already decodes it) | pump.fun bonding curve, PumpSwap, Raydium swaps |

**Not a goal:** trading, Telegram, AWS, changing the bot. This is a **read-only research tool**. It runs on
the user's laptop, not on the box.

## 2. What the manual case study proved (the tool must reproduce this)

All data came from the **public Hyperliquid `info` API** (`POST https://api.hyperliquid.xyz/info`, no key).
The raw JSON (134 files) is in `audit/wallet-trace-case-2026-11-17/` (git-ignored). Full addresses and
their roles are in `audit/wallet-trace-case-2026-11-17/wallets.txt`. **Short forms only in this file**
(rule 1: no address-like strings in git).

### 2.1 The checks, and where each one shows

| Question | Where it shows (Hyperliquid) | Case result (11 wallets) |
|---|---|---|
| Has sub-accounts? | `subAccounts` query; `subAccountTransfer` in the ledger | None |
| Money in a vault (someone else trades it)? | `vaultDeposit` / `vaultWithdraw` in the ledger; `userVaultEquities` | Only HLP (Hyperliquid's own vault, `0xdfc2…`), 2024 |
| Traded futures? | `portfolio` → `perpAllTime.vlm`; fills with `Open/Close Long/Short`; `userFunding` | $0, except one 4.1 RUNE test long ($35) |
| Moved money to futures to trade? | `accountClassTransfer toPerp:true`, then perp fills | Always followed by `withdraw` within ~5 min = **exit door, not trading** |
| Staking? | `cStakingTransfer`; `delegatorSummary` | Holders stake right after receiving |
| Airdrops? | `spotGenesis` in the ledger | HYPE airdrop 2024-11-29 07:25 is where the big money came from |
| Left Hyperliquid? | `withdraw` (to Arbitrum); `send` to `0x2000…` / `0x2222…` (to HyperEVM) | The trail stops here |

**Key fact:** on Hyperliquid you can only withdraw from the perp balance. So "move to perp → withdraw a few
minutes later" is how money LEAVES. Never count that as futures trading.

### 2.2 The verdicts the tool must reproduce (golden test)

| Wallet (short) | Expected verdict | Why |
|---|---|---|
| `0x0000…acb8`, `0x0028…4092`, `0x0064…dec5`, `0x006c…7f8b`, `0x0009…c610` | HOLDER | 0 fills ever |
| `0x8747…` | SPOT TRADER (airdrop seller), not a bot | 1,077 fills, all spot; 157 orders on 58 days; perp volume $0; no funding |
| `0x8f4f…` | twin of `0x8747…` | same-second airdrops ×3, same tiny sender within 1 min ×2, both in HLP |
| `0x921e…` | CASH-OUT | lives 1 day: receive → sell on spot → withdraw |
| `0xc8aa…` | PASS-THROUGH | receives 10,098 HYPE, sends 10,022 on 3 minutes later |
| `0xb9b9…` | HOLDER | receives, stakes in 2 min, never trades |
| `0xf0bd…` | SLOW SELLER | spot sells in round lots (1,000 / 500 / 300 HYPE) every 1–3 days, then withdraws |
| `0xf580…` | CASH-OUT | receives JEFF, only sells (149), withdraws |
| `0x02bb…`, `0x029b…`, `0x8738…` | ONE-SHOT BUYER | deposit USDC → buy XMR1 in minutes → send all to one holder |
| No wallet | LIKELY BOT | none of the 11+ wallets trades like a bot |

**Completeness check that worked:** for `0x8747…`, summing every HYPE movement gave the live balance to 4
decimals:
`airdrop 59,072.41 + bought 1,827.01 − sold 19,161.19 − fees 0.509 − sent 5,321 − staked 36,400 + unstaked 36,443.19 − sent 36,459 = 0.9156` (API: `0.91567`).
If the sum does NOT match the live balance, history is missing (fills cap, paging). The tool must flag
that wallet `INCOMPLETE` instead of guessing.

### 2.3 What we learned about the user's idea

For this case the idea did not hold. The holders were filled by an **airdrop**, staked, sold slowly and
spread over fresh wallets. No bot. The tool must still be able to find a bot when there is one, so the
classifier has a bot rule (section 5) that the case does NOT trigger.

## 3. Where the code goes

```
app/tools/wallet_trace/
  __init__.py
  __main__.py        CLI: python -m app.tools.wallet_trace <addr> [<addr>...] --chain hl|sol --depth 2 --top 5
  model.py           dataclasses: Wallet, Event, Edge, Profile, Verdict (chain-neutral)
  cache.py           on-disk cache of raw API answers (so a re-run costs 0 calls); resumable
  hl_source.py       Hyperliquid info API: ledger, fills (paged), funding, portfolio, snapshots, spotMeta
  sol_source.py      Solana: signatures + transactions via SolanaRpc; decode transfers and swaps
  profile.py         raw events -> Profile (the numbers in sections 4 and 5)
  classify.py        Profile -> Verdict (rules in section 5)
  links.py           transfer graph + twin scoring (section 6)
  walk.py            breadth-first walk with depth, top-N, stop list, call budget
  report.py          Markdown report + edges CSV + ASCII tree
app/tests/test_wallet_trace_*.py
app/tests/fixtures/wallet_trace_*.json      ANONYMISED only (section 8)
scripts/make_trace_fixtures.py              builds the anonymised fixtures from audit/
```

- **Output goes to `audit/wallet_trace/<UTC-time>/`** (git-ignored). Reports hold real addresses, so they
  must never be committed.
- **Keep it out of the bot's runtime.** `main.py` must not import it. Add a test that proves this (grep or
  import-graph check), so the bot's loops and budgets cannot be touched by mistake.
- **Hyperliquid client:** this repo removed its Hyperliquid client in the split (D-19, B-17). Write a small,
  separate, read-only client in `hl_source.py`. Do NOT bring back the old bot source. Record this as a new
  DECISIONS row when you build it.
- **Solana RPC budget:** `SOLANA_RPC_URL` is the bot's Chainstack key with a daily budget (B-15). The tool
  must take its own `--rpc-url` (default: the free public `https://api.mainnet-beta.solana.com`). Only use
  the bot's URL if the user passes it on purpose. Never log any URL (it can carry a key; see
  `data/solana_rpc.py`).

## 4. Data sources — verify each one first (rule 8)

Phase 0 checks every item below against the current docs AND one real answer. Write what was checked, and
when, in the module docstring. Do not rely on what this plan says; it is a starting point.

### 4.1 Hyperliquid (no key)

| Query (`type`) | Gives | Note |
|---|---|---|
| `userNonFundingLedgerUpdates` (`startTime`) | deposits, withdrawals, transfers, staking, vaults, sub-account moves, airdrops | **(built: 500 per answer, paged by time. It MISSES some staking moves: use `delegatorHistory`, DEBUG D-8)** |
| `userFillsByTime` (`startTime`, `endTime`) | every trade | Capped (~2,000 per answer). Page by time until empty. **(built: only the newest 10,000 exist; TWAP slices are NOT here, read `userTwapSliceFillsByTime` too, DEBUG D-9)** |
| `userFunding` (`startTime`) | funding payments = futures positions held | Empty = never held a futures position overnight |
| `portfolio` | `allTime.vlm`, `perpAllTime.vlm` | Fast spot-vs-perp split |
| `clearinghouseState` | perp account now, open positions | |
| `spotClearinghouseState` | spot balances now | Needed for the completeness check |
| `delegatorSummary` | staked HYPE now | |
| `subAccounts` | sub-accounts of a master | `null` = none |
| `userVaultEquities`, `vaultDetails` | vaults joined; vault name + leader | Mark HLP as protocol, not a person |
| `userRole` | user / vault / subAccount / agent | An agent (API wallet) points at its master: follow it |
| `spotMeta` | `@N` → pair name, token index → name | Without it `@107` means nothing |

Ledger fields seen in the case (2026-11-17): `deposit`, `withdraw`, `spotTransfer`, `send` (has
`sourceDex`/`destinationDex`), `internalTransfer`, `accountClassTransfer` (`toPerp`), `cStakingTransfer`
(`isDeposit`), `spotGenesis`, `vaultDeposit`, `vaultWithdraw`, `subAccountTransfer`, `borrowLend`,
`accountActivationGas`. Unknown types must be kept and counted in the report, never dropped.

System addresses (not people; stop the walk there): `0x2222…2222` (HYPE ↔ HyperEVM), `0x2000…0000` and
`0x2000…00NN` (token ↔ HyperEVM; the last bytes are the token index). Verify this rule in the docs.

Rate limits: read the current "rate limits" page. Default to at most 2 calls/second, back off on 429.
**(built: the limit is 1200 WEIGHT a minute per IP; most info calls weigh 20, so Hyperliquid is paced by weight at
about one call a second, and a lasting 429 is waited out. Solana's public RPC throttles at ~2/s: 1/s.)**

### 4.2 Solana (free public RPC by default)

- `getSignaturesForAddress` (page with `before`) and `getTransaction` (`maxSupportedTransactionVersion: 0`,
  `encoding: jsonParsed`). Pre/post token balances give spot buys and sells without decoding each program.
- **Futures:** Jupiter Perps — reuse `data/jupiter_events.py` and `data/jupiter_perps.py` (already checked
  against mainnet, DEBUG D-2 and D-5). Do not copy them; import them.
- **Spot:** pump.fun bonding curve program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` (seen in a live
  RPC answer 2026-11-17). PumpSwap and Raydium program ids: **look up and verify**, do not guess.
- **Funder:** the oldest incoming SOL transfer (the wallet that paid for this wallet's first fee). This is the
  strongest "same owner" clue on Solana.
- **Trading-bot apps** (Photon, BullX, Axiom, Trojan, …) take a fee to their own wallet inside the swap.
  Their fee wallets: **look up and verify**, keep them in a local label file, not in git if unsure.
- pump.fun's own API answered 403 to scripts and Solscan's API needs a key (both checked 2026-11-17). Helius
  has a free tier with a key and decoded transactions; optional, only if the user gives a key.

### 4.3 Out of scope for v1 (say so in the report)

- Arbitrum (after a Hyperliquid `withdraw`) and HyperEVM (after `0x2000…`/`0x2222…`). The Arbitrum
  explorer blocked a script with a bot check (HTTP 403, 2026-11-17). The report prints the address and the
  explorer link for the user to open in a browser.
- Exchange hot wallets: stop the walk and label them if they are in the local label file.

## 5. Profile and classification rules

`profile.py` computes, per wallet, chain-neutral numbers:

| Field | Meaning |
|---|---|
| `fills`, `orders`, `active_days`, `first`, `last` | trading activity (orders = distinct order ids) |
| `perp_fills`, `spot_fills`, `perp_volume`, `spot_volume` | futures vs spot split |
| `funding_events` | futures positions held (Hyperliquid); open/close events (Jupiter) |
| `buy_share` | share of spot volume that is buys |
| `round_lot_share` | share of sell orders with a round size (100, 250, 500, 1,000, …) |
| `exit_door_pairs` | `toPerp` moves followed by `withdraw` within `EXIT_DOOR_MINUTES` |
| `trading_perp_moves` | `toPerp` moves followed by perp fills before any withdraw |
| `in_usd`, `out_usd` per counterparty | transfer edges |
| `staked_now`, `stake_lag_minutes` | how fast incoming tokens get staked |
| `lifetime_days` | first to last ledger event |
| `passthrough_lag_minutes` | time from a big incoming transfer to a similar outgoing one |
| `airdrops` | `spotGenesis` events (token, amount, time) |
| `complete` | the balance sum matches the live balance (section 2.2) |

`classify.py` — first rule that matches wins. **Every number is a setting** (in one constants block, or
`settings.toml` if it ever runs on the box). They are starting points fitted to the case, not truths:

1. `INCOMPLETE` — `complete` is false. Still print the rest, marked as unsure.
2. `HOLDER` — 0 fills.
3. `PASS-THROUGH` — ≥ 90% of a big incoming amount leaves within `PASSTHROUGH_MINUTES` (start: 30) and
   fills < 10.
4. `CASH-OUT` — `lifetime_days` ≤ 3 or sells ≥ 90% of spot volume, AND withdraws ≥ 50% of what it sold.
   **(built: also money-in to biggest withdraw ≤ 3 days; `0x921e…` had a 389-day ledger life from a late airdrop)**
5. `ONE-SHOT BUYER` — deposit → buys → one transfer out of ≥ 90%, all within one day.
6. `LIKELY BOT` — orders ≥ 300, active_days ≥ 30, fills/active day ≥ 20, AND (perp share ≥ 50% or swap
   count on Solana ≥ 300 across ≥ 20 tokens). The case must NOT trigger this.
7. `PERP TRADER` — perp share ≥ 50% and `funding_events` > 0 (Hyperliquid) or Jupiter episodes ≥ 5.
8. `SLOW SELLER` — sells ≥ 80% of spot volume, `round_lot_share` ≥ 50%, sells spread over ≥ 5 days.
9. `SPOT TRADER` — anything else that trades.

Each verdict prints the reasons ("0 perp fills; 0 funding; 157 orders on 58 days"), so the user can check it,
as in the case study.

**Boundaries (rule 7):** test each rule AT its boundary value, for example a withdraw exactly
`EXIT_DOOR_MINUTES` after `toPerp`, and exactly 300 orders. Add a mutant for each in `scripts/mutants.sh`.

## 6. Linking wallets (same owner)

`links.py` builds a graph: nodes = wallets, edges = transfers (USD, count, first/last time, token).

Twin score between two wallets: add points per clue, each clue printed with its evidence.

| Clue | Points (start) | Case example |
|---|---|---|
| Same airdrop token at the same second | 3 each **(built: 3 in total, and never a twin alone; early wallets share up to 49 airdrop seconds with strangers)** | HYPE, HFUN, JEFF: `0x8747…` and `0x8f4f…` |
| Same small sender within 2 minutes | 2 each | 15 LQNA from `0x8f66…`; 0.0001 HYPE from `0xcf83…` |
| Same funder (first incoming money / first SOL) | 3 | — |
| Same vault joined within the same week | 1 | both in HLP, 2024 |
| Direct transfers both ways | 1 | — |
| Sub-account or agent of the same master | 5 (certain) | — |

Score ≥ 6 = "same owner, very likely". Show it as a group in the report.

Watch out: an airdrop at the same second is shared by EVERY eligible wallet. It is only a clue together with
something else. The case passed because there were five clues.

## 7. The walk (`walk.py`)

- Breadth-first from the start wallets. `--depth` (default 2) and `--top` (default 5): follow only the top-N
  counterparties by USD in each direction.
- **Stop list:** system addresses, HLP and other protocol vaults, labelled exchanges, and any wallet already
  seen.
- **Call budget:** `--max-calls` (default 500). When it is reached, stop and say which wallets were not
  visited.
- **Cache:** every raw answer is saved under `audit/wallet_trace/cache/<chain>/<address>/<query>.json` with
  its fetch time. `--refresh` ignores the cache. A re-run of the case costs 0 calls.
- A depth-2 run of 3 Hyperliquid wallets with top-3 took about 1 minute by hand (~30 wallets × ~6 calls).

## 8. Tests and fixtures (rules 1, 6, 7)

- **Golden test:** the case in section 2.2, from anonymised fixtures. Every verdict, both twins, the HYPE
  balance sum (0.9156), and "no LIKELY BOT".
- **Anonymise before anything enters git.** `scripts/make_trace_fixtures.py` reads
  `audit/wallet-trace-case-2026-11-17/`, then:
  - replaces every 40-hex address with a placeholder like `0x` + `ab` × 19 + a 2-digit index (same input →
    same placeholder, so links still work);
  - **deletes every `hash` field** — those are 64-hex strings, and `scripts/checks.py` `hex64` fails on any
    64-hex string anywhere in the repo;
  - keeps amounts, times, coins and types.
  For Solana fixtures, `fixture-keys` rejects any base58 string of 32–44 characters that is not in
  `ALLOWED_PUBLIC_KEYS`, and any signature. Add only public program ids to that list.
- Unit tests per rule, at the boundary value. A mutant per rule in `scripts/mutants.sh`, seen to go red.
- A test that `app/main.py` does not import `app.tools.wallet_trace`.
- No network in tests: sources take an `httpx` transport, as `SolanaRpc` already does.

## 9. Phases (one PR each; each ends with `./precommit.sh` printing ALL STAGES PASS)

| Phase | Work | Done when |
|---|---|---|
| 0 | Verify every API in section 4 (docs + one real answer). Write the docstrings. Fixture builder + anonymised case fixtures | Docstrings say what was checked and when; `checks.py` passes on the fixtures |
| 1 | `model`, `cache`, `hl_source` (with paging), `profile` for Hyperliquid | The `0x8747…` balance sum is exact from fixtures; unknown ledger types are counted |
| 2 | `classify` + golden test | All section 2.2 verdicts pass; mutants red |
| 3 | `links`, `walk`, `report`, CLI | `python -m app.tools.wallet_trace <holder> --depth 2` on the live API prints the same story as section 2 |
| 4 | Solana futures: Jupiter Perps profile via the existing decoders | A known Jupiter trader from the bot's `jup_episodes` is `PERP TRADER` |
| 5 | Solana spot: transfers, funder, pump.fun / PumpSwap / Raydium swaps, bot-app fee label | A wallet from a pump.fun token's top holders gets a verdict with reasons |
| 6 (later, ask the user) | Use the tracer in wallet selection: "follow a trader to a new wallet" (roadmap step 3 in `future-trading-kucoins`, doc 09) | User decides; needs its own DECISIONS row |

## 10. Rules that apply here (from CLAUDE.md, repeated because they bite)

- No AWS, no cloud credentials, no Telegram. The tool runs on the laptop.
- **No full address, signature, transaction hash or key in git**, not even in docs. Short forms only. Real
  data lives in `audit/` (git-ignored).
- Git only when the user asks. Docs ship with each phase: this file, the changelog, DECISIONS (new tool +
  separate Hyperliquid client), and `.claude/docs/01-architecture.md` (module map).
- When the user asks about using a found wallet for real-money copying, remind them of B-3 first.

## 11. Open questions for the user (ask before the phase that needs them)

1. Phase 5: is a Helius (or other) API key available, or free public RPC only? **(answered 2026-11-17: free public RPC only)**
2. Phase 3: is a local label list of exchange and bot-app wallets OK to keep in `audit/` (outside git)? **(answered: yes, `audit/wallet_trace/labels.toml`)**
3. Phase 6: should the tracer ever feed the bots, or stay a research tool?
