#!/bin/bash
#
# user_data.sh — FIRST BOOT ONLY (pattern from Pump-fun-S1).
#
# The instance has `user_data_replace_on_change = false` and `prevent_destroy`, so this runs once per box. Anything
# that must be re-appliable lives in hostSetup.sh, which this script downloads and runs and which the operator can
# re-run any time:
#   here          — packages, the region/registry helpers, ECR login, .env from Secrets Manager, the host-file
#                   sync, the bot unit, the daily status timer
#   hostSetup.sh  — swap, directories, image-update / backup / restore / spool timers, mask sshd
#
# Every heredoc uses a QUOTED delimiter (<<'EOF') so this shell expands nothing inside the written scripts.

set -uo pipefail
exec > >(tee -a /var/log/solana-future-coin-userdata.log) 2>&1
echo "=== solana-future-coin user_data starting $(date -u) ==="

# ── 0. region and registry, read from the instance, never typed ────────────────────────────────────
cat >/usr/local/bin/solana-future-coin-region <<'EOF'
#!/bin/bash
set -uo pipefail
TOKEN="$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
curl -sf -H "X-aws-ec2-metadata-token: ${TOKEN}" http://169.254.169.254/latest/meta-data/placement/region
EOF
chmod 755 /usr/local/bin/solana-future-coin-region

cat >/usr/local/bin/solana-future-coin-account <<'EOF'
#!/bin/bash
set -uo pipefail
aws sts get-caller-identity --region "$(/usr/local/bin/solana-future-coin-region)" --query Account --output text
EOF
chmod 755 /usr/local/bin/solana-future-coin-account

cat >/usr/local/bin/solana-future-coin-ecr-registry <<'EOF'
#!/bin/bash
set -uo pipefail
echo "$(/usr/local/bin/solana-future-coin-account).dkr.ecr.$(/usr/local/bin/solana-future-coin-region).amazonaws.com"
EOF
chmod 755 /usr/local/bin/solana-future-coin-ecr-registry

REGION="$(/usr/local/bin/solana-future-coin-region)"
BOT_DIR="/opt/solana-future-coin"

# ── 1. packages ────────────────────────────────────────────────────────────────────────────────────
dnf update -y
dnf install -y docker jq awscli sqlite
COMPOSE_VERSION="v2.29.7"
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64" \
  -o /usr/local/bin/docker-compose
chmod 755 /usr/local/bin/docker-compose
systemctl enable --now docker

mkdir -p "${BOT_DIR}/data" "${BOT_DIR}/logs/scripts" "${BOT_DIR}/logs/app"
chown -R 1000:1000 "${BOT_DIR}/data" "${BOT_DIR}/logs/app"

# ── 2. ECR login every 6 h (the token lives 12; a 12 h timer failed at 11.5 on the parent project) ──
cat >/usr/local/bin/ecr-login <<'EOF'
#!/bin/bash
set -uo pipefail
aws ecr get-login-password --region "$(/usr/local/bin/solana-future-coin-region)" \
  | docker login --username AWS --password-stdin "$(/usr/local/bin/solana-future-coin-ecr-registry)"
EOF
chmod 755 /usr/local/bin/ecr-login

cat >/etc/systemd/system/ecr-login.service <<'EOF'
[Unit]
Description=Refresh the ECR authorisation token
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ecr-login
EOF

cat >/etc/systemd/system/ecr-login.timer <<'EOF'
[Unit]
Description=Refresh the ECR token every 6 hours

[Timer]
OnBootSec=30s
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 3. .env from the hand-made secret Kucoin-bot, refreshed hourly; restart only on a change ───────
# `to_entries`, so a key added to the secret later (KUCOIN_API_PASSPHRASE, SOLANA_RPC_URL) needs no change here.
cat >/usr/local/bin/solana-future-coin-env-refresh <<'EOF'
#!/bin/bash
set -uo pipefail
BOT_DIR="/opt/solana-future-coin"
umask 077
aws secretsmanager get-secret-value --region "$(/usr/local/bin/solana-future-coin-region)" --secret-id "Kucoin-bot" \
  --query SecretString --output text \
  | jq -r 'to_entries | .[] | "\(.key)=\(.value|tostring)"' | sort >"${BOT_DIR}/.env.new"
if [ ! -s "${BOT_DIR}/.env.new" ]; then
  rm -f "${BOT_DIR}/.env.new"
  echo "secret fetch empty; keeping the existing .env"
  exit 1
fi
if [ -f "${BOT_DIR}/.env" ] && diff -q "${BOT_DIR}/.env" "${BOT_DIR}/.env.new" >/dev/null 2>&1; then
  rm -f "${BOT_DIR}/.env.new"
  exit 0
fi
mv "${BOT_DIR}/.env.new" "${BOT_DIR}/.env"
chmod 600 "${BOT_DIR}/.env"
echo "the secret changed ($(grep -c '=' "${BOT_DIR}/.env") keys); restarting the bot if it runs"
if systemctl is-active --quiet solana-future-coin; then systemctl restart solana-future-coin; fi
EOF
chmod 755 /usr/local/bin/solana-future-coin-env-refresh

cat >/etc/systemd/system/solana-future-coin-env.service <<'EOF'
[Unit]
Description=Refresh /opt/solana-future-coin/.env from Secrets Manager (Kucoin-bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/solana-future-coin-env-refresh
EOF

cat >/etc/systemd/system/solana-future-coin-env.timer <<'EOF'
[Unit]
Description=Hourly .env refresh

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 4. host-file sync (statusCheck.sh, hostSetup.sh, docker-compose.yml from s3://<bucket>/host/) ──
cat >/usr/local/bin/solana-future-coin-sync-scripts <<'EOF'
#!/bin/bash
set -uo pipefail
exec 9>/run/solana-future-coin-sync.lock
flock -n 9 || exit 0
REGION="$(/usr/local/bin/solana-future-coin-region)"
BOT_DIR="/opt/solana-future-coin"
BUCKET="solana-future-coin-data-$(/usr/local/bin/solana-future-coin-account)"
RERUN=0
for f in statusCheck.sh hostSetup.sh; do
  if aws s3 cp "s3://${BUCKET}/host/${f}" "${BOT_DIR}/logs/scripts/${f}.new" --region "${REGION}" >/dev/null 2>&1; then
    if ! diff -q "${BOT_DIR}/logs/scripts/${f}" "${BOT_DIR}/logs/scripts/${f}.new" >/dev/null 2>&1; then
      mv "${BOT_DIR}/logs/scripts/${f}.new" "${BOT_DIR}/logs/scripts/${f}" && chmod 755 "${BOT_DIR}/logs/scripts/${f}"
      echo "updated ${f}"
      [ "${f}" = hostSetup.sh ] && RERUN=1
    else
      rm -f "${BOT_DIR}/logs/scripts/${f}.new"
    fi
  fi
done
if aws s3 cp "s3://${BUCKET}/host/docker-compose.yml" "${BOT_DIR}/docker-compose.yml.new" --region "${REGION}" >/dev/null 2>&1; then
  if ! diff -q "${BOT_DIR}/docker-compose.yml" "${BOT_DIR}/docker-compose.yml.new" >/dev/null 2>&1; then
    mv "${BOT_DIR}/docker-compose.yml.new" "${BOT_DIR}/docker-compose.yml"
    echo "compose file changed"
    if systemctl is-active --quiet solana-future-coin; then systemctl restart solana-future-coin; fi
  else
    rm -f "${BOT_DIR}/docker-compose.yml.new"
  fi
fi
# hostSetup.sh is re-run when it changed, and while sshd is not masked yet: at first boot the SSM agent may not be
# active when hostSetup.sh runs, so it leaves sshd alone, and nothing ran it again (DEBUG D-7, 2026-11-16).
systemctl is-enabled sshd 2>/dev/null | grep -q masked || RERUN=1
if [ "${RERUN}" = 1 ] && [ -f "${BOT_DIR}/logs/scripts/hostSetup.sh" ]; then
  bash "${BOT_DIR}/logs/scripts/hostSetup.sh" >>"${BOT_DIR}/logs/hostsetup.log" 2>&1 || echo "WARN: hostSetup.sh failed"
fi
exit 0
EOF
chmod 755 /usr/local/bin/solana-future-coin-sync-scripts

cat >/etc/systemd/system/solana-future-coin-sync.service <<'EOF'
[Unit]
Description=Sync the host files Terraform uploaded to S3
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/solana-future-coin-sync-scripts
EOF

cat >/etc/systemd/system/solana-future-coin-sync.timer <<'EOF'
[Unit]
Description=Pull the host files from S3 every 5 minutes

[Timer]
OnBootSec=45s
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 5. the bot unit (restore before start: an empty database must never overwrite the good backup) ─
cat >/etc/systemd/system/solana-future-coin.service <<'EOF'
[Unit]
Description=solana-future-coin futures copy bot
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=/opt/solana-future-coin
ExecStartPre=/usr/local/bin/ecr-login
ExecStartPre=/usr/local/bin/solana-future-coin-restore
ExecStart=/bin/bash -c 'ECR_REGISTRY="$(/usr/local/bin/solana-future-coin-ecr-registry)" /usr/local/bin/docker-compose up -d --remove-orphans'
ExecStop=/bin/bash -c 'ECR_REGISTRY="$(/usr/local/bin/solana-future-coin-ecr-registry)" /usr/local/bin/docker-compose down'
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

# ── 6. the daily status check ──────────────────────────────────────────────────────────────────────
cat >/etc/systemd/system/solana-future-coin-status.service <<'EOF'
[Unit]
Description=Daily status check, appended to logs/status-daily.log

[Service]
Type=oneshot
ExecStart=/bin/bash /opt/solana-future-coin/logs/scripts/statusCheck.sh
StandardOutput=append:/opt/solana-future-coin/logs/status-daily.log
StandardError=append:/opt/solana-future-coin/logs/status-daily.log
SuccessExitStatus=0 1 2 3 4 5 6 7 8 9 10
EOF

cat >/etc/systemd/system/solana-future-coin-status.timer <<'EOF'
[Unit]
Description=Run the status check once a day

[Timer]
OnCalendar=*-*-* 06:23:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 7. start; hostSetup.sh hardens last ────────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable --now ecr-login.timer solana-future-coin-env.timer solana-future-coin-sync.timer solana-future-coin-status.timer
/usr/local/bin/ecr-login || echo "WARN: first ECR login failed; the timer retries"
/usr/local/bin/solana-future-coin-env-refresh || echo "WARN: .env not written; check the role's secret-read policy"

for TRY in $(seq 1 30); do
  /usr/local/bin/solana-future-coin-sync-scripts || true
  [ -f "${BOT_DIR}/logs/scripts/hostSetup.sh" ] && break
  echo "WARN: host files not in the bucket yet (try ${TRY}/30); waiting 20s"
  sleep 20
done

if [ -f "${BOT_DIR}/logs/scripts/hostSetup.sh" ]; then
  bash "${BOT_DIR}/logs/scripts/hostSetup.sh" || echo "WARN: hostSetup.sh reported problems; read the log"
else
  echo "ERROR: hostSetup.sh did not sync, so SSH IS NOT MASKED. Connect over SSM and run it by hand."
fi

systemctl enable --now solana-future-coin.service || echo "WARN: the bot did not start; check ECR and .env"
echo "=== solana-future-coin user_data finished $(date -u) region=${REGION} ==="
