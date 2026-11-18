# 07 — Changelog

Back to the index: [CLAUDE.md](../../CLAUDE.md). One entry per day, newest last.

**This repo starts at the 2026-11-14 split (the last entry).** Every entry before it is the history of the parent repo
`future-trading-coins` (now `future-trading-kucoins`), kept because the code here came from it.

## 2026-11-13 — First version

- New repo. Python 3.12, reusing ai-kucoin-future's data clients, grading, paper math, ledger and Telegram client
  (D-1, D-2). One $10k paper account, 3x isolated, 10 positions (D-5).
- Signals: Hyperliquid GOLD wallets + Jupiter Perps wallets read from the Solana chain (D-4, D-6, D-7). KuCoin is the
  only venue traded. Live executor written (signed REST, exchange stop orders, reconcile) but locked behind
  `MODE=live` + passphrase + pre-flight.
- Telegram: 15-minute digest of real fills + alarms, Pump-fun style (D-8). Recorder: futures data to the shared bucket.
- Infra: t4g.micro in Frankfurt, pull-based deploy, two workflows. aws-foundation B-32 (repo added, `robot-pump.fun`
  removed for policy size) and Pump-fun-S3 D-127 (`futures/` write) prepared, not applied.
- Found on the way: Jupiter accounts are 216 bytes, not the IDL's 210 (DEBUG D-2); a cent of drift halted at the
  tolerance (D-3); a stale `.pyc` let a mutant survive (D-1).
- On that day: 121 tests and 23 mutants, all killed, `./precommit.sh` ALL STAGES PASS. Not run: the Docker build (no local daemon).
- Later: the user added `KUCOIN_API_PASSPHRASE` and `SOLANA_RPC_URL` to `Kucoin-bot` (B-2, B-4 now WATCH).

## 2026-11-13 — First CI run failed: app/data/ was never committed (DEBUG D-4)

- `.gitignore` `data/` hid the source package. Fixed (`/data/`, `/logs/`), added `scripts/checks.py`
  `source-not-ignored` (in precommit and CI) and `scripts/verify_clone.sh` (fresh-clone test before every push).

## 2026-11-13 — Wallet list checked every 5 minutes (user)

- `WALLET_CHECK_MINUTES` (Hyperliquid live check) and `JUPITER_GRADE_MINUTES` (Jupiter re-grade + re-pick) = 5, were
  a hard-coded hour. The daily Hyperliquid grading round is unchanged (its speed is set by Hyperliquid's rate limit).
  Tests: `test_settings_file_loads_and_is_paper`, `test_main_uses_the_wallet_list_intervals`, two new mutants.
- The box is up (`i-0f88352629b80bcca`, eu-central-1a, public IP 3.76.226.144). The user locked the KuCoin key to
  that IP; the IP is not fixed (no Elastic IP) — BUG B-13.

## 2026-11-13 — More wallets: items 1-5 (user: "apply 1 - 5")

- Live scan of today's leaderboard with the bot's own grader (91 wallets): 0 GOLD, 3 SILVER on the old 30-day rules;
  rejections mostly protect money (inactive 24, KuCoin does not list the coins 22, hidden losses 14).
- **1 Jupiter history from the chain** (`data/jupiter_events.py`, `jobs/jupiter_history.py`): close events decoded and
  checked on real mainnet transactions; a real whale backfilled in 65 calls (14 episodes, PnL matching prices). The
  event tag is little-endian (DEBUG D-5).
- **2 Hyperliquid trade stream** (`data/hl_stream.py`): 75 wallets in 20 s live; prescreen with one portfolio call.
- **3 90-day window**, 6 fill pages. **4 shadow copies** (`trading/shadow.py`) and **5 SILVER at weight <= 3**, both
  through one selection rule (`core/selection.py`). Watched wallets polled every 30 s.
- Migration 2. New tool `wallets_report` (statusCheck 4b). 148 tests, 37 mutants, all killed.

## 2026-11-13 — GitGuardian alert: fixtures trimmed (DEBUG D-6)

- The flagged string was Jupiter's public event-authority address (no credential; nothing to rotate). The full git
  history was scanned for real credential shapes (Telegram token, Chainstack URL, AWS key, private key, KuCoin key,
  heartbeat URL): none ever committed.
- Fixtures cut to what tests read; 392 trade hashes and 3 position addresses replaced by placeholders. New checks
  `fixture-keys` and `hex64` in `scripts/checks.py` (precommit + CI).

## 2026-11-13 — First green deploy with items 1-5; KuCoin IP lock kept

- The deploy run passed; the box kept 3.76.226.144. The user keeps the KuCoin key locked to that IP without an Elastic
  IP (B-13: a stop/start would change it).

## 2026-11-14 — Split from future-trading-coins: this repo is the Solana bot (D-19)

Why: the user wants one bot per source, each on its own box, so one can change, stop or go live alone. The parent
repo became `future-trading-kucoins` (Hyperliquid -> KuCoin); this one is `future-trading-solana-coins`
(Jupiter -> KuCoin, paper). Trading on Jupiter itself may come later (roadmap, D-19 "revisit when").

- **Names:** `futcoins-bot` -> `futsol-bot` in Terraform, workflows, host scripts, compose, Dockerfile, statusCheck
  (`/opt/futsol-bot`, `futsol-*` timers, ECR `futsol-bot`, bucket `futsol-bot-data-<acct>`). State key
  `future-trading-solana-coins/terraform.tfstate`; tag `Repo = future-trading-solana-coins`. DB `data/futsol.db`.
- **Hyperliquid removed:** `jobs/discovery.py`, `jobs/watch.py`, `data/hl_stream.py`, their loops and settings, the
  Hyperliquid weight check, the HL-only tests, fixtures and mutants. `data/hyperliquid.py` trimmed to the shared types
  and parsers (B-17). `MarketCache.refresh(kf)` maps `JUPITER_COINS` only. `ijson` and `websockets` dropped.
- **Telegram:** `BOT_NAME` (`Jupiter-KuCoin`) after the `[PAPER]` stamp in every message, because both bots post to
  one chat (D-21). Test + mutant.
- **Recorder:** no KuCoin market data any more (the KuCoin bot records it); `grades` / `bot/*` -> `solana-bot/*`
  (D-20). Test + mutant.
- **statusCheck:** a missing `SOLANA_RPC_URL` is a FAIL here (it is the only source).
- **Same secret `Kucoin-bot`** (user). Live is blocked until this bot has its own KuCoin key/sub-account (B-3).
- **aws-foundation B-33** (not applied yet): adds this repo and project `futsol-bot`, drops Pump-fun-S1/S3.
- Docs: fresh BUG register (B-1 not deployed, B-3 live blocker, B-4 shared bucket policy, B-17 split leftovers),
  D-19..D-21, L-11.

## 2026-11-16 — Renamed `solana-future-coin`; go-live key reminder; evidence tooling; sshd fix

- **Names (user, D-22):** every `futsol-bot` / `futsol` name is now `solana-future-coin` (Terraform, workflows, host
  scripts and units, compose, Dockerfile, DB `data/solana-future-coin.db`). State key unchanged. Still `t4g.micro`
  (user: "the solana must run also on t4g.micro"). aws-foundation B-34 grants the prefix (not applied yet).
- **Go-live reminder (user: "as soon as we have decided for the real money remind me"):** `KUCOIN_KEY_DEDICATED`
  (default false). `MODE=live` refuses to boot until it is true, and `golive_check` shows a failing "own KuCoin key"
  gate with the reminder. Tests + 2 mutants (B-3).
- **Evidence:** `scripts/collect_evidence.sh` (user runs it, read-only AWS: `evidence-read-only` check),
  `statusCheck.sh --evidence` (box logs to `s3://<bucket>/diag/`, kept 30 days), `app/tools/evidence_report.py` (every
  table as CSV + `summary.md` by wallet / source / reason / tier / day; read-only open). Tests + 2 mutants.
- **Host:** the sync re-runs `hostSetup.sh` while sshd is not masked or when it changed (DEBUG D-7, seen on the KuCoin
  box); the update timer retries a FAILED bot start. `scripts/checks.py host-guards` keeps both. The restore still
  starts fresh when there is no backup (this bot has no history; the KuCoin bot waits instead).
- **Shared bucket:** Pump-fun-S3 D-129 (in code there) lists `solana-future-coin-role` for `futures/` (B-4).

## 2026-11-16 (later) — Own secret `solana-future-bot`

- This bot reads `solana-future-bot` (user; the old shared secret is deleted after B-9). Terraform names it exactly
  (`-??????`); `hostSetup.sh` section 0 rewrites the `.env` script so the running box switches; an unreadable secret
  keeps the last `.env` and `statusCheck.sh` warns. Check `old-secret-name`; `audit/` skipped by `scripts/checks.py`.
- Incident on 2026-11-16: copying the old futcoins bucket into this bucket also copied the OLD `host/` files; fixed by
  copying this repo's three host files back (the box syncs `host/` every 5 min). `host/` belongs to Terraform.
- **Wallet history as Parquet (user):** new `jup_episodes` rows (the Jupiter trades grading uses) are copied at each
  flush to `futures/solana-bot/episodes`; the last exported id lives in `meta` (no repeats, no gaps after a restart).
  `record/episodes_export.py`, `test_episodes_export.py` (2), 2 mutants; `collect_evidence.sh` downloads it.

## 2026-11-16 — Telegram report every 45 min, always (D-24)

- `DIGEST_MINUTES = 45`, new `DIGEST_ALWAYS = true`: every window sends a report; one with no trade says "no trades in
  this window" and lists the open positions and the account. After a gap only the newest empty window reports. Every
  report has an "Open positions" block. `DIGEST_MINUTES` must divide 1440. 3 new tests + 3 mutants (as the KuCoin bot).

## 2026-11-16 — Shared history bucket: this bot opts in by its own role tags (Pump-fun-S3 D-131)

- `infra/iam.tf`: the role carries `bigdata-history-read = yes` and `bigdata-history-write = futures`; new
  `shared_history_read` grants `s3:GetObject` on the bucket and `s3:ListBucket`. The bucket policy lists no project
  (user: each project owns its access). Test `app/tests/test_shared_bucket_access.py`, red on the old file.
- Works after Pump-fun-S3's manual deploy (the live policy still lists only `futcoins-bot-role`), then this repo's.

## 2026-11-17 — PLAN.md: wallet tracer (plan only, no code)

- **Why (user):** holder wallets on the Hyperliquid leaderboard show big PnL with $0 volume; the user suspects hidden
  bot wallets trade and send them the profit, and wants a tool that finds them, for futures AND spot, on Hyperliquid
  and Solana. A manual case study (11+ wallets, from the `future-trading-kucoins` session) found no bot: an airdrop,
  staked, sold slowly and spread over fresh wallets.
- **What:** `PLAN.md` is the brief for the session that builds `app/tools/wallet_trace/` (read-only, laptop only, never
  imported by `main.py`): checks and where each shows, golden verdicts from the case, classification and twin rules,
  phases 0-6, anonymised fixtures (no address or 64-hex hash in git).
- Case data (raw API answers, full addresses, the prototype script) is in `audit/wallet-trace-case-2026-11-17/`
  (git-ignored).

## 2026-11-17 — Wallet tracer built (PLAN phases 0-5, D-25)

- **Why (user):** "change the whole project using PLAN.md": add the tracer, keep the bot as it is; 2 calls/s as PLAN.md
  says; free public Solana RPC only; labels in `audit/`.
- **What:** `app/tools/wallet_trace/` (doc 09): Hyperliquid info client with time paging, weight budget and 429 wait;
  balance replay per token (`INCOMPLETE` when history and live balance disagree); classify rules 1-9 with reasons;
  twin scoring; breadth-first walk with stop list and call budget; Markdown/CSV/tree report; Solana transfers, swaps
  (pump.fun, PumpSwap, Raydium, Jupiter), funder, Jupiter Perps episodes via the bot's own decoder.
- **Checked against docs + real answers (2026-11-17):** Hyperliquid paging (500 / 2000 / 10,000), weights, system
  addresses, `userRole`, `vaultDetails`; Solana `jsonParsed` shape, transaction version 1, public-RPC throttle; every
  program id and mint executable / a mint on mainnet.
- **Found on real data:** the ledger misses staking moves (DEBUG D-8), fills miss TWAP slices (D-9), a liquidation
  takes the margin with no row, airdrop dust vanishes. Each is handled, tested and mutated. CASH-OUT now measures
  money-in to money-out; shared airdrops count as one clue (max 3 points) and never make a twin alone.
- **Bug fixed in the bot's code:** `SolanaRpc` errors could carry the RPC URL with its key (DEBUG D-10).
- **Result:** the golden test reproduces every PLAN §2.2 verdict, the twins, and the funder's HYPE sum (0.91567001);
  no LIKELY BOT. A live depth-2 run from the staking holder: 11 wallets, 190 calls, no bot. A live Jupiter trader is
  `PERP TRADER`; a live pump.fun trader gets `SPOT TRADER` with its swaps.
- Tests: `test_wallet_trace_{golden,rules,tool,sol}.py`; fixtures `wallet_trace_hl_case.json` and
  `wallet_trace_sol_case.json` from `scripts/make_trace_fixtures.py` (anonymised). `ALLOWED_PUBLIC_KEYS` lists the
  public Solana program ids and mints. Lint: S105 off for `tools/wallet_trace/*` (it reads `token == "SOL"` as a
  password).

## 2026-11-17 — The copy-trading bot is removed; the repo is the wallet tracer (D-26)

- **Why (user):** "make sure all codes related to the old bot is removed and only the newly added ones base on the new
  plan.md", then push. The user destroys the AWS infrastructure (`terraform destroy`, BUG B-18).
- **Removed:** the bot's packages and tools, 15 bot test files and the KuCoin/Hyperliquid fixtures, `infra/`,
  `.github/workflows/` (no CI deploy any more: a push touches no AWS), `hostSetup.sh`, `statusCheck.sh`,
  `docker-compose.yml`, `app/Dockerfile`, `app/settings.toml`, `scripts/collect_evidence.sh`, docs 02/03/04/06/08.
  Dependencies cut to `httpx` + `tenacity`.
- **Kept and trimmed:** `data/solana_rpc.py` (no bot settings; the caller passes the URL), `data/http.py` (no hot-path
  reserve), `data/jupiter_perps.py` / `jupiter_events.py` and their real-data tests, `utils/logging.py` (Chainstack
  and `api-key=` redaction only).
- **New guards:** `test_no_trading_or_bot_code_comes_back` (red on the first run: two leftover bot words), the tool
  imports with no env; `scripts/checks.py` rewritten for this repo with a new `case-addresses` check (no real case
  wallet outside `audit/`). `precommit.sh` runs the checks, lint, types, tests, the CLI and the mutants.
- **For the destroy:** `audit/old-bot/` holds a copy of `infra/` plus the three host files it reads (git-ignored).
- **Explained:** how the removed bot worked, with diagrams, in `audit/bot-explained/` (local only).
- **Destroyed (user, 2026-11-17):** `terraform destroy` from `audit/old-bot/infra` removed all 21 resources (box
  `i-05ad4a52b74d57842`, bucket `solana-future-coin-data-…` with its backups, ECR repo, role, profile, SG, policies).
  It first stopped on `prevent_destroy`; the local copy was changed to allow it (`force_destroy` on the bucket,
  `force_delete` on the ECR repo). The shared bucket's `futures/` recordings were not touched. Left: the secret
  `solana-future-bot` (hand-made; BUG B-18).

## 2026-11-17 — Telegram summary per run; keys from AWS Secrets Manager (D-27)

- **Why (user):** "i need report on the telegram and the api is keep in the secret manager". Choices: a summary each
  run; read the tokens from Secrets Manager; use Chainstack for Solana; keep the secret `solana-future-bot`.
- **What:** `wallet_trace/secrets.py` (env first, then `get_secret_value` on `solana-future-bot`; never fatal, never
  prints a value), `wallet_trace/telegram.py` (summary text with short addresses only; `sendMessage`, HTML, 4,096
  limit; failures report status or type only), CLI flags `--no-telegram`, `--no-aws`, `--secret`. Solana uses the
  secret's `SOLANA_RPC_URL` at 4 calls/s and 300 tx per wallet; `--rpc-url` still wins. `boto3` added to requirements.
- **Tests:** `test_wallet_trace_notify.py` (fake Secrets Manager and Telegram; a run sends exactly one summary;
  `--no-telegram` sends none; the token never appears in an error), RPC choice in `test_wallet_trace_sol.py`; the
  read-only guard now allows boto3 only in `secrets.py` and only `get_secret_value`. 9 new mutants, all killed.
- BUG B-18 closed (the secret stays on purpose). New: B-23 (first real secret read is the user's), B-24 (Chainstack pace).

## 2026-11-18 — Bot restored with the tracer kept (D-26 reversed, D-28)

- **Why (user):** "i have asked you to keep the setup and change the way that the bot finds the wallets". The
  2026-11-17 removal misread the request (LESSON L-13).
- **Restored from `7437aec`:** all bot code and tests, `infra/`, the workflows, host scripts, docs 02/03/04/06/08,
  the bot's requirements. **Kept:** the tracer, its Telegram + Secrets Manager (D-27), DEBUG D-8..D-10, and the
  `SolanaRpc` URL-leak fix (D-10) plus `maxSupportedTransactionVersion: 1` for the bot's history reader too.
- `boto3` moved to `requirements-dev.txt` (laptop tracer only), so the bot image is unchanged.
- Guards: the bot does not import the tracer (subprocess test) and no bot module names it; the tracer only reads.
  `scripts/checks.py` = the bot-era checks + `case-addresses`; `scripts/mutants.sh` = 108 mutants (bot + tracer).
- **Deploy:** merged to main and pushed (user: "Rebuild on push"): CI rebuilds the destroyed box, bucket and ECR repo.
  New box, empty database, fresh $10,000 paper account (BUG B-25).
- **Next:** D-28, the new wallet finding (Hyperliquid holders -> hidden bot wallets -> copy them). Not built yet.

