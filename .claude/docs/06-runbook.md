# 06 — Runbook (on the box, over SSM)

Back to the index: [CLAUDE.md](../../CLAUDE.md).

Two containers since D-30: `solana-future-coin-scout` (finds, grades, picks) and `solana-future-coin-trader` (feed,
copies, exits, Telegram). The systemd unit `solana-future-coin` starts and stops both.

| Want | Command |
|---|---|
| Status (also daily in `logs/status-daily.log`) | `sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh` |
| Trades (ids = Telegram `#id`) | `sudo docker exec solana-future-coin-trader python -m app.tools.trade_log` |
| Go-live check | `sudo docker exec solana-future-coin-trader python -m app.tools.golive_check` |
| Wallet pool (both files, read-only) | `sudo docker exec solana-future-coin-scout python -m app.tools.wallets_report` |
| Health | `curl -s 127.0.0.1:8080/health \| jq` (scout) · `curl -s 127.0.0.1:8081/health \| jq` (trader: copy list, feed) |
| Logs | `sudo docker logs --since 1h solana-future-coin-trader` (or `-scout`); files in `logs/app/{scout,trader}/app.log` |
| Copy speed | `sudo sqlite3 -readonly /opt/solana-future-coin/data/solana-future-coin.db "SELECT via, COUNT(*), AVG(ts - open_time) FROM signals WHERE kind='OPEN' GROUP BY via"` |
| Kill switch (the trader closes all, stops) | `sudo touch /opt/solana-future-coin/data/STOP`; undo: `sudo rm` it, `sudo systemctl restart solana-future-coin` |
| Backup now | `sudo /usr/local/bin/solana-future-coin-backup` |
| Spool upload now | `sudo /usr/local/bin/solana-future-coin-spool` |
| Re-read the secret | `sudo /usr/local/bin/solana-future-coin-env-refresh` |

## Going live (only after the go-live check passes, BUG B-3 is closed, and the user decides)

0. B-3 first: this bot needs its own KuCoin sub-account and key (locked to an Elastic IP of this box) in its own
   secret. Today's key (the same in both bots' secrets) is IP-locked to the KuCoin box, and two bots on one account break the
   pre-flight and the reconcile. Then set `KUCOIN_KEY_DEDICATED = true` in `app/settings.toml`: until then `MODE=live`
   refuses to boot and `golive_check` shows the "own KuCoin key" gate failing (the reminder, user 2026-11-16).
1. Add `KUCOIN_API_PASSPHRASE` to `solana-future-bot`; the key needs Futures trade permission.
2. Put the real USDT in the KuCoin futures account; close any manual position there.
3. Set `MODE = "live"` and `LIVE_START_BALANCE = <that amount>` in `app/settings.toml`, run `./precommit.sh`, push.
4. Watch the first `[LIVE]` digest and `statusCheck.sh`; the reconcile loop pauses entries and alarms on any mismatch.

## Evidence for an analysis (user, 2026-11-16)

1. On the box: `sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh --evidence` (logs -> `s3://<bucket>/diag/`).
2. Here: `./scripts/collect_evidence.sh` (your AWS login; read-only). `LEGACY=1` = the old futcoins database with the
   Jupiter history from before the split; `DAYS=7` = a week of `solana-bot/` Parquet.
3. Hand `audit/<date>/` to the assistant; start at `INDEX.md` and `analysis/summary.md`.

## The first start after D-30 (or on a new box)

The scout starts without `data/scout.db` (or from its backup), copies the owner list once from the trader's file (log
line `seed: N owners from the trader's file`), and reads every owner's past again: about 20 minutes
until the copy list is full. Until the scout's first pick the trader refuses new opens (LIST_STALE) and its /health
says `stale: copy list`; that is expected. Exits of open positions never wait for the scout.

## Alarms (Telegram 🚨, at most once an hour per title; only the trader sends Telegram)

Every alarm is stamped `[PAPER] Jupiter-KuCoin` / `[LIVE] Jupiter-KuCoin` (same chat as the KuCoin bot).

Kill switch · Account halted (ledger drift: read DEBUG first) · Reconcile mismatch, entries paused · KuCoin auth
failed · Market data stale (> 5 min) · Copy list stale (the scout's pick > 600 s old: new opens refused; not in the
first 10 minutes after a start) · Solana feed down (websocket down > 300 s; the poll still runs) · Scout feed stale
(the scout's last Jupiter feed poll > 900 s old).
