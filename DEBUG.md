# DEBUG.md — symptom -> root cause -> guard

So the same issue is never diagnosed twice. Back to [CLAUDE.md](CLAUDE.md).

D-1 to D-7 come from the copy-trading bot (removed 2026-11-17, D-26): D-3 and D-7 describe code that is gone and stay
as history; D-1, D-2, D-4, D-5, D-6 still guard code here (mutants, the Jupiter decoders, .gitignore, fixtures).
D-8 onward are the wallet tracer's.

| D-n | Symptom | Root cause | Guard |
|---|---|---|---|
| D-1 | A mutant "survived" once, then was killed on a re-run | Two mutants of the same file had the same byte size and the same mtime second; Python reused the stale `.pyc` of the first | `scripts/mutants.sh` sets `PYTHONDONTWRITEBYTECODE=1` and deletes `__pycache__` before each mutant |
| D-2 | The Jupiter feed returned 0 positions on mainnet | The IDL's fields add up to 210 bytes, but real Position accounts are **216** bytes; the `dataSize: 210` filter matched nothing | `ACCOUNT_SIZE = 216`; tests decode three real recorded accounts (`fixtures/jupiter_positions.json`); mutant `ACCOUNT_SIZE = 210` must fail |
| D-6 | GitGuardian incident #40040487 "Generic High Entropy Secret" on `jupiter_more_txs.json` (commit 27b756a): `37hJBDnn…iqBN` | Not a credential: Jupiter's PUBLIC event-authority address, inside the full account lists of real transactions saved as fixtures. Our `no-secrets` check only knew credential shapes, not "key-looking" strings | Fixtures trimmed to the bytes tests use (event payloads, program id); trade hashes and position addresses replaced by placeholders; `scripts/checks.py` `fixture-keys` + `hex64` (fail on the pushed files: 59 keys, a signature, 64-hex hashes) |
| D-7 | `statusCheck.sh`: "WARN sshd is not masked (re-run hostSetup.sh)" on the KuCoin box (2026-11-16), two days after its first boot | At first boot `user_data.sh` runs `hostSetup.sh` once; its last step masks sshd only if the SSM agent is ALREADY active (so a failed SSM never locks the box out). The agent was not active yet, so sshd was left alone, and nothing ever ran `hostSetup.sh` again: the sync timer copies a new `hostSetup.sh` but did not run it | The host sync (`infra/user_data.sh`) re-runs `hostSetup.sh` when it changed and on every pass while sshd is not masked (`RERUN`); `scripts/checks.py host-guards` fails the build if that line goes. Same fix in both futures repos |
| D-5 | Jupiter close events were never found, even in a transaction decoded correctly by hand | Anchor's event tag is the u64 `0x1d9acb512ea545e4` written little-endian: the bytes are `sha256("anchor:event")[:8]` REVERSED (`e445a52e51cb9a1d`); the code compared the unreversed hash | `EVENT_IX = ...[::-1]`; `test_event_tag_is_the_little_endian_anchor_constant` and real-transaction tests; mutant restores the old order |
| D-4 | First CI run: `ModuleNotFoundError: No module named 'app.data'` in every test; all local tests passed | `.gitignore` had `data/` (meant for the runtime folder), which also matched the source package `app/data/`, so its 9 files were never committed | `.gitignore` anchors runtime folders (`/data/`, `/logs/`); `scripts/checks.py` `source-not-ignored` (fails on the old file: 9 failures); `scripts/verify_clone.sh` tests a fresh clone before every push |
| D-8 | Wallet tracer: a holder's HYPE history did not add up to its live balance (the ledger said 8,547 HYPE too few) | `userNonFundingLedgerUpdates` MISSED three `cStakingTransfer`s (10,060 HYPE staked, 1,500 unstaked) that `delegatorHistory` has | Staking comes from `delegatorHistory` (finalized withdrawals); the ledger's rows only fill in what it lacks, counted once. `test_delegator_history_fills_what_the_ledger_misses` + 2 mutants |
| D-9 | Wallet tracer: two wallets INCOMPLETE (one with 0 fills but USDC spent, one with HYPE it never bought) | TWAP orders fill in slices that are NOT in `userFillsByTime` (a real wallet: 516 fills + 1,347 TWAP slices) | `hl_source` also reads `userTwapSliceFillsByTime`; `merged_fills` dedups by trade id. `test_twap_slices_count_as_fills` + mutant |
| D-10 | `SolanaRpc` error messages could still carry the RPC URL (with its key) | `str(exc).split(':')[-1]` kept the text after the LAST colon; a transport error text "cannot reach https://host/<key>" ends in "//host/<key>" | The message keeps only "HTTP <status>" or the exception type, never its text. `test_an_rpc_error_never_carries_the_url` (red on the old code) + mutant |
| D-3 | A ledger drift of exactly $0.01 halted the account although the rule says "above $0.01" | `0.01` computed from ledger sums is `0.010000000000218` in floats | `risk.snapshot` rounds the drift to 6 decimals before comparing; `test_drift_at_exactly_the_tolerance_does_not_halt` + mutant |

## D-2 detail

Found by the first real call (public RPC, 2026-11-13). The discriminator filter alone found 885,878 accounts; a sample of
five via `getMultipleAccounts` showed 216 bytes each. Offsets were then confirmed on real BTC positions (entry prices
~$80k, sizes up to $1.3M, dates 2025-10 to 2026-11). A full scan is ~280 MB, so the bot reads only accounts updated
in the current 65,536-second block (memcmp on the high 6 bytes of `updateTime`, offset 146): 4,014 accounts, 1.8 MB,
~1 s.
