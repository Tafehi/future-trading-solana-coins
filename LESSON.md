# LESSON.md — lessons (root cause -> fix -> takeaway)

Back to [CLAUDE.md](CLAUDE.md).

L-1 to L-10 are inherited from `future-trading-coins` (the parent repo, before the 2026-11-14 split).

- **L-1 — An IDL is not the account.** Field sizes from an IDL can differ from what lives on-chain (padding, added
  fields). Before filtering by `dataSize`, read a few real accounts. (DEBUG D-2)
- **L-2 — Test the boundary value with real arithmetic.** A threshold compared with a float sum fails AT the
  boundary; round before comparing, and test exactly at the line. (DEBUG D-3)
- **L-3 — Two guards on one path hide each other from a mutant.** The Jupiter "fresh open" needed a scenario where
  only `old is None` protects (an account opened just before a poll), or the mutant survived through the other
  guard. A surviving mutant can mean a missing scenario, not a missing guard.
- **L-4 — A mutant harness must not trust the bytecode cache.** (DEBUG D-1)
- **L-5 — Shared IAM policies have a size budget.** Each project adds its ARNs to many aws-foundation policies (twice
  while Stockholm is open). Run aws-foundation's `./scripts/check-offline.sh` BEFORE planning a new repo; the 9th
  repo needed `robot-pump.fun` removed (aws-foundation B-32).
- **L-6 — Copy-trading needs public positions.** KuCoin, Binance and Bitget hide other traders' positions; Hyperliquid
  and Solana perps do not. Trade where you want, grade where you can see (inherited from ai-kucoin-future E-24/E-25).
- **L-7 — A local test run does not prove the repo is complete.** Files that exist on disk but are git-ignored pass
  every local check and fail in CI. Test a fresh clone before pushing (`scripts/verify_clone.sh`), and never write an
  unanchored ignore pattern (`data/`) in a repo that has a source folder of the same name. (DEBUG D-4; Pump-fun-S3
  had already learned this and has a "source not git-ignored" stage.)
- **L-8 — A hash prefix is not the bytes on the wire.** Anchor writes its event tag as a little-endian integer; the
  hand check printed the right bytes and the code still compared the wrong order. Test the decoder on a real recorded
  payload, not on bytes built by the same assumption. (DEBUG D-5)
- **L-9 — Measure the shortage before fixing it.** The live scan showed most wallets fail for reasons that protect
  money (idle, unlisted coins, hidden losses), so loosening thresholds would have added risk, not edge; widening the
  candidate pool and shadow-proving wallets adds wallets without loosening any protection.
- **L-10 — Public is not the same as safe to commit.** A secret scanner cannot tell a public Solana address or a trade
  hash from an API key or a private key, and every false alarm costs the owner time. Commit only the bytes a test reads;
  replace addresses, signatures and hashes with placeholders. (DEBUG D-6)
- **L-11 — A copied bot needs its own name in every place two bots share.** After copying a project, `grep -ri` for the
  old prefix is not enough: shared places keep the old meaning silently. Here three were shared: the Telegram chat (fix:
  `BOT_NAME` in every message, D-21), the recordings prefix `futures/` (fix: own `solana-bot/` folder, D-20) and the
  KuCoin account and key (not fixable in code: BUG B-3 blocks live). Each fix ships a test and a mutant.
