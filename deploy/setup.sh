#!/usr/bin/env bash
# Idempotent installer for the trumpbot daemon on a fresh Ubuntu host.
#
# Usage (as root):
#   sudo ./deploy/setup.sh
#
# Reruns are safe: missing pieces are created, existing ones are left
# alone. Files this script touches:
#   /opt/trumpbot/                    code + venv (owned by trumpbot)
#   /var/lib/trumpbot/                SQLite database directory
#   /var/log/trumpbot/                log directory (mostly journald, but
#                                     reserved for ad-hoc files)
#   /etc/trumpbot/                    config + secrets
#   /etc/systemd/system/trumpbot.service

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "setup.sh must be run as root (use sudo)" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="trumpbot"
SERVICE_GROUP="trumpbot"
INSTALL_DIR="/opt/trumpbot"
DATA_DIR="/var/lib/trumpbot"
LOG_DIR="/var/log/trumpbot"
CONFIG_DIR="/etc/trumpbot"
SERVICE_FILE="/etc/systemd/system/trumpbot.service"

echo "[setup] ensuring $SERVICE_USER user/group"
if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$INSTALL_DIR" \
          --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "[setup] creating directories"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 "$INSTALL_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$DATA_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$LOG_DIR"
install -d -o root           -g "$SERVICE_GROUP" -m 0750 "$CONFIG_DIR"

echo "[setup] syncing code to $INSTALL_DIR (rsync)"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='.mypy_cache/' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  "$REPO_DIR/" "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] installing uv (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "[setup] creating venv and installing pinned deps"
sudo -u "$SERVICE_USER" bash -lc "cd $INSTALL_DIR && uv sync --no-dev --frozen"

echo "[setup] copying example config files (only if missing — never overwrite secrets)"
[[ ! -f "$CONFIG_DIR/config.yaml" ]] && \
  install -o root -g "$SERVICE_GROUP" -m 0640 "$REPO_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
[[ ! -f "$CONFIG_DIR/subject_aliases.yaml" ]] && \
  install -o root -g "$SERVICE_GROUP" -m 0640 "$REPO_DIR/config/subject_aliases.yaml" "$CONFIG_DIR/subject_aliases.yaml"
[[ ! -f "$CONFIG_DIR/secrets.env" ]] && \
  install -o root -g "$SERVICE_GROUP" -m 0640 /dev/null "$CONFIG_DIR/secrets.env" && \
  cat <<'EOF' >> "$CONFIG_DIR/secrets.env"
# Required by trumpbot. Fill in real values, then chmod 0600.
KALSHI_API_KEY_ID=replace_me
KALSHI_PRIVATE_KEY_PASSPHRASE=replace_me
TWITTER_BEARER_TOKEN=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
LITESTREAM_BUCKET=
LITESTREAM_REGION=us-east-1
LITESTREAM_ENDPOINT=
EOF

echo "[setup] enforcing 0600 on the secrets file"
chmod 0600 "$CONFIG_DIR/secrets.env"

if [[ -f "$CONFIG_DIR/kalshi_private.pem" ]]; then
  chmod 0600 "$CONFIG_DIR/kalshi_private.pem"
  chown root:"$SERVICE_GROUP" "$CONFIG_DIR/kalshi_private.pem"
fi

echo "[setup] applying database migrations as $SERVICE_USER"
sudo -u "$SERVICE_USER" bash -lc \
  "cd $INSTALL_DIR && uv run python -c 'from trumpbot.db import Database; Database(\"$DATA_DIR/trumpbot.db\").connect()'"

echo "[setup] installing systemd unit"
install -m 0644 "$REPO_DIR/deploy/trumpbot.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable trumpbot.service

echo "[setup] done. Edit $CONFIG_DIR/config.yaml + $CONFIG_DIR/secrets.env then 'systemctl start trumpbot'."
