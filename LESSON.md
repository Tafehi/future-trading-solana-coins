# LESSON.md — lessons (root cause -> fix -> takeaway)

Back to [CLAUDE.md](CLAUDE.md).

L-1 to L-10 come from the copy-trading bot (removed 2026-11-17, D-26); they stay because the lessons still hold.

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
- **L-11 — A balance that adds up is the only proof a history is complete.** The wallet tracer replays every movement
  and compares the sum with the live balance. That one check found three gaps no documentation mentions: staking moves
  missing from the ledger (DEBUG D-8), TWAP fills missing from the fills list (D-9), and a liquidation that takes the
  margin with no row at all. A count of rows proves nothing; a sum that matches to 8 decimals does.
- **L-12 — "Strip the URL" must be tested with the URL inside the error text.** `split(':')[-1]` looked safe on an
  "HTTP 429" message and leaked the key on a connection error (DEBUG D-10). Keep the type of the failure, never its
  text.
- **L-13 — When an answer does not pick an option, ask again before anything destructive.** On 2026-11-17 the user
  asked to remove "all codes related to the old bot"; asked how much to remove, they answered "i can run tf destroy
  ... if needed", which picked none of the options. The removal went ahead on the widest reading, and the user ran
  the destroy. They had meant the old wallet-selection method. The bot was restored the next day, but the box's
  database and backups were gone for good. A deletion that is hard to undo needs an answer that names it.
- **L-14 — A compose `command:` is APPENDED to the image's ENTRYPOINT.** The image had
  `ENTRYPOINT ["python", "-m", "app.main"]`. Two services with `command: ["python", "-m", "app.scout"]` and
  `["python", "-m", "app.trader"]` would both have run `python -m app.main python -m app.scout`: the whole old bot,
  twice, on one paper account. Found in the plan review for D-30, before it shipped. Pick the program with
  `entrypoint:` in compose and keep only `CMD` in the image; `scripts/checks.py compose` enforces both.
- **L-15 — A surviving mutant can mean the TEST is weak, not the code.** Two D-30 mutants survived. (1) The
  same-file check compares normalised paths, but the test built its "different-looking" path with `pathlib`, which
  drops the `/./` by itself, so normalisation was never exercised: build such inputs as plain strings. (2) The
  websocket cleared its subscription state in two places, so removing one changed nothing: keep one owner of each
  invariant, so that losing it shows.
- **L-16 — A copied Terraform folder is never "just old": it points at the live state.** `audit/old-bot/infra` (the
  copy made for the 2026-11-17 destroy) kept the backend key `future-trading-solana-coins/terraform.tfstate`. After CI
  rebuilt the box on 2026-11-18 with the same key, a second `terraform destroy` in that folder would have destroyed
  the LIVE box, bucket and image repo. Found during the 2026-11-19 cleanup and deleted. Delete such a copy as soon as
  its one destroy is done.
