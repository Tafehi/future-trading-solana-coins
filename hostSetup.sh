#!/bin/bash
#
# hostSetup.sh — the RE-RUNNABLE part of the box setup. user_data.sh runs it at first boot; the operator can run it
# again any time over SSM: sudo bash /opt/solana-future-coin/logs/scripts/hostSetup.sh
#
#   1. 1 GB swap (t4g.micro has 1 GB of RAM)
#   2. directories and ownership (uid 1000 = the container user)
#   3. solana-future-coin-restore   : before the bot starts, fetch the newest backup if there is no local database
#   4. solana-future-coin-backup    : hourly SQLite backup to s3://solana-future-coin-data-<acct>/live/ and daily/
#   5. solana-future-coin-spool     : every 15 min, move finished Parquet files to s3://bigdata-shared-history-<acct>/futures/
#   6. solana-future-coin-update    : every 5 min, pull :latest and restart only if the image changed
#   7. mask sshd, but only after proving the SSM agent runs (SSM is the only way in)
#
# Heredocs use quoted delimiters (<<'EOF'): this shell expands nothing inside the written scripts.

set -uo pipefail
BOT_DIR="/opt/solana-future-coin"
echo "=== hostSetup.sh $(date -u) ==="

# ── 1. swap ────────────────────────────────────────────────────────────────────────────────────────
if ! swapon --show | grep -q /swapfile; then
  if [ ! -f /swapfile ]; then
    dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none && chmod 600 /swapfile && mkswap /swapfile >/dev/null
  fi
  swapon /swapfile && echo "swap on"
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

# ── 2. directories ─────────────────────────────────────────────────────────────────────────────────
mkdir -p "${BOT_DIR}/data/spool" "${BOT_DIR}/logs/scripts" "${BOT_DIR}/logs/app" "${BOT_DIR}/backup"
chown -R 1000:1000 "${BOT_DIR}/data" "${BOT_DIR}/logs/app"

# ── 3. restore ─────────────────────────────────────────────────────────────────────────────────────
cat >/usr/local/bin/solana-future-coin-restore <<'EOF'
#!/bin/bash
# Before the bot starts: no local database -> fetch live/solana-future-coin.db. "Not found" is a fresh start (exit 0):
# this bot is new (2026-11-14 split) and has no paper history to protect, so its first boot must start empty. (The
# KuCoin bot WAITS instead, because it carries the old futcoins ledger; that repo's B-20.) Any other failure exits 1
# and fails the unit on purpose: a bot that starts empty would back up an empty ledger over the good one an hour later.
set -uo pipefail
DB="/opt/solana-future-coin/data/solana-future-coin.db"
[ -s "${DB}" ] && exit 0
REGION="$(/usr/local/bin/solana-future-coin-region)"
BUCKET="solana-future-coin-data-$(/usr/local/bin/solana-future-coin-account)" || exit 1
OUT="$(aws s3 cp "s3://${BUCKET}/live/solana-future-coin.db" "${DB}.restore" --region "${REGION}" 2>&1)"
if [ $? -eq 0 ]; then
  mv "${DB}.restore" "${DB}" && chown 1000:1000 "${DB}" && echo "restored ${DB} from s3://${BUCKET}/live/"
  exit 0
fi
rm -f "${DB}.restore"
if echo "${OUT}" | grep -qE '404|Not Found|does not exist'; then
  echo "no backup yet: fresh start"
  exit 0
fi
echo "ERROR: restore failed and it is not a missing backup: ${OUT}"
exit 1
EOF
chmod 755 /usr/local/bin/solana-future-coin-restore

# ── 4. backup ──────────────────────────────────────────────────────────────────────────────────────
cat >/usr/local/bin/solana-future-coin-backup <<'EOF'
#!/bin/bash
# The SQLite online backup API (sqlite3 .backup), so a backup taken while the bot writes is still consistent.
set -uo pipefail
DB="/opt/solana-future-coin/data/solana-future-coin.db"
[ -s "${DB}" ] || { echo "no database yet"; exit 0; }
REGION="$(/usr/local/bin/solana-future-coin-region)"
BUCKET="solana-future-coin-data-$(/usr/local/bin/solana-future-coin-account)" || exit 1
OUT="/opt/solana-future-coin/backup/solana-future-coin.db"
rm -f "${OUT}"
sqlite3 "${DB}" ".backup '${OUT}'" || exit 1
aws s3 cp "${OUT}" "s3://${BUCKET}/live/solana-future-coin.db" --region "${REGION}" --only-show-errors || exit 1
aws s3 cp "${OUT}" "s3://${BUCKET}/daily/$(date -u +%F).db" --region "${REGION}" --only-show-errors || exit 1
date -u +%s >/opt/solana-future-coin/logs/last-backup
echo "backup done $(date -u +%FT%TZ)"
EOF
chmod 755 /usr/local/bin/solana-future-coin-backup

cat >/etc/systemd/system/solana-future-coin-backup.service <<'EOF'
[Unit]
Description=Hourly SQLite backup to S3

[Service]
Type=oneshot
ExecStart=/usr/local/bin/solana-future-coin-backup
EOF

cat >/etc/systemd/system/solana-future-coin-backup.timer <<'EOF'
[Unit]
Description=Hourly backup at :17

[Timer]
OnCalendar=*-*-* *:17:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 5. spool -> shared bucket ──────────────────────────────────────────────────────────────────────
cat >/usr/local/bin/solana-future-coin-spool <<'EOF'
#!/bin/bash
# The recorder writes spool/futures/<dataset>/day=D/part-*.parquet (renamed from .tmp only when complete).
# `aws s3 mv` uploads then deletes each local file, so a failed upload is retried on the next run.
set -uo pipefail
SPOOL="/opt/solana-future-coin/data/spool"
[ -d "${SPOOL}/futures" ] || exit 0
REGION="$(/usr/local/bin/solana-future-coin-region)"
SHARED="bigdata-shared-history-$(/usr/local/bin/solana-future-coin-account)" || exit 1
aws s3 mv "${SPOOL}/" "s3://${SHARED}/" --recursive --exclude "*" --include "futures/*.parquet" \
  --region "${REGION}" --only-show-errors || exit 1
date -u +%s >/opt/solana-future-coin/logs/last-spool
EOF
chmod 755 /usr/local/bin/solana-future-coin-spool

cat >/etc/systemd/system/solana-future-coin-spool.service <<'EOF'
[Unit]
Description=Move recorded futures Parquet files to the shared bucket

[Service]
Type=oneshot
ExecStart=/usr/local/bin/solana-future-coin-spool
EOF

cat >/etc/systemd/system/solana-future-coin-spool.timer <<'EOF'
[Unit]
Description=Spool upload every 15 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 6. image update ────────────────────────────────────────────────────────────────────────────────
cat >/usr/local/bin/solana-future-coin-update <<'EOF'
#!/bin/bash
# Pull :latest; restart the bot only when the image id changed. Rollback = retag an older sha- image as latest in ECR.
set -uo pipefail
REG="$(/usr/local/bin/solana-future-coin-ecr-registry)" || exit 1
IMG="${REG}/solana-future-coin:latest"
BEFORE="$(docker image inspect --format '{{.Id}}' "${IMG}" 2>/dev/null || true)"
docker pull -q "${IMG}" >/dev/null || { /usr/local/bin/ecr-login >/dev/null 2>&1; docker pull -q "${IMG}" >/dev/null || exit 1; }
AFTER="$(docker image inspect --format '{{.Id}}' "${IMG}" 2>/dev/null || true)"
if [ -n "${AFTER}" ] && [ "${BEFORE}" != "${AFTER}" ]; then
  echo "new image ${AFTER:7:12}; restarting"
  systemctl restart solana-future-coin
  docker image prune -f >/dev/null 2>&1 || true
elif systemctl is-failed --quiet solana-future-coin; then
  # A FAILED start (ECR or the restore was not ready) is retried here. A bot the operator stopped on purpose is
  # "inactive", not "failed", and stays stopped.
  echo "bot unit failed; starting it again"
  systemctl start solana-future-coin || true
fi
EOF
chmod 755 /usr/local/bin/solana-future-coin-update

cat >/etc/systemd/system/solana-future-coin-update.service <<'EOF'
[Unit]
Description=Pull the latest bot image and restart on a change
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/solana-future-coin-update
EOF

cat >/etc/systemd/system/solana-future-coin-update.timer <<'EOF'
[Unit]
Description=Image update every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now solana-future-coin-backup.timer solana-future-coin-spool.timer solana-future-coin-update.timer

# ── 7. mask sshd, only once SSM is proven ──────────────────────────────────────────────────────────
if systemctl is-active --quiet amazon-ssm-agent; then
  systemctl disable --now sshd >/dev/null 2>&1 || true
  systemctl mask sshd >/dev/null 2>&1 && echo "sshd masked (SSM agent is active)"
else
  echo "WARN: the SSM agent is not active; sshd left alone so the box is not locked out"
fi
echo "=== hostSetup.sh done ==="
