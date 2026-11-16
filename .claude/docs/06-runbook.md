# 06 — Runbook (on the box, over SSM)

Back to the index: [CLAUDE.md](../../CLAUDE.md).

| Want | Command |
|---|---|
| Status (also daily in `logs/status-daily.log`) | `sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh` |
| Trades (ids = Telegram `#id`) | `sudo docker exec solana-future-coin python -m app.tools.trade_log` |
| Go-live check | `sudo docker exec solana-future-coin python -m app.tools.golive_check` |
| Logs | `sudo docker logs --since 1h solana-future-coin` |
| Kill switch (close all, stop) | `sudo touch /opt/solana-future-coin/data/STOP`; undo: `sudo rm` it, `sudo systemctl restart solana-future-coin` |
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

## Alarms (Telegram 🚨, at most once an hour per title)

Every alarm is stamped `[PAPER] Jupiter-KuCoin` / `[LIVE] Jupiter-KuCoin` (same chat as the KuCoin bot).

Kill switch · Account halted (ledger drift: read DEBUG first) · Reconcile mismatch, entries paused · KuCoin auth
failed · Market data stale (> 5 min).
