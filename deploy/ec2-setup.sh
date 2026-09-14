#!/usr/bin/env bash
# Bootstrap MPWG radar cooker on Amazon Linux 2023 ARM (t4g.small) or x86.
# Run as root: sudo bash deploy/ec2-setup.sh
set -euo pipefail

APP_USER="${APP_USER:-mpwg}"
APP_ROOT="${APP_ROOT:-/opt/mpwg-radar}"
DATA_ROOT="${DATA_ROOT:-/var/lib/mpwg-radar}"
ENV_FILE="${ENV_FILE:-/etc/mpwg-radar.env}"
REPO_URL="${REPO_URL:-https://github.com/reposinject/mpwg-radar-tiles.git}"
REPO_REF="${REPO_REF:-main}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/ec2-setup.sh" >&2
  exit 1
fi

echo "==> Installing OS packages (Amazon Linux 2023 / dnf)"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip python3-devel gcc git tar gzip shadow-utils rsync
else
  echo "dnf not found; install python3, pip, gcc, git manually and re-run." >&2
  exit 1
fi

PYTHON="$(command -v python3.11 || command -v python3)"
echo "==> Using $($PYTHON --version)"

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$DATA_ROOT" --create-home --shell /sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_ROOT" "$DATA_ROOT/data" "$DATA_ROOT/output" /var/log/mpwg-radar

echo "==> Syncing application to $APP_ROOT"
if [[ -f "$SRC_DIR/pyproject.toml" ]]; then
  # Install from the checked-out tree (typical when this repo is cloned on the instance).
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'output' --exclude 'data' --exclude 'work' \
    "$SRC_DIR"/ "$APP_ROOT"/
else
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$APP_ROOT"
fi

"$PYTHON" -m venv "$APP_ROOT/.venv"
# shellcheck disable=SC1091
source "$APP_ROOT/.venv/bin/activate"
pip install --upgrade pip wheel
pip install -e "$APP_ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$APP_ROOT/.env.example" "$ENV_FILE"
  cat >> "$ENV_FILE" <<EOF

# Paths for this host (filled by ec2-setup.sh)
MPWG_DATA_DIR=$DATA_ROOT/data
MPWG_OUTPUT_DIR=$DATA_ROOT/output
EOF
  chmod 640 "$ENV_FILE"
  echo "==> Wrote $ENV_FILE — add R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY before starting."
else
  echo "==> Keeping existing $ENV_FILE"
fi

install -m 644 "$APP_ROOT/deploy/systemd/mpwg-radar-cooker.service" /etc/systemd/system/
install -m 644 "$APP_ROOT/deploy/systemd/mpwg-radar-cooker.timer" /etc/systemd/system/

chown -R "$APP_USER:$APP_USER" "$APP_ROOT" "$DATA_ROOT" /var/log/mpwg-radar
chown root:root /etc/systemd/system/mpwg-radar-cooker.service /etc/systemd/system/mpwg-radar-cooker.timer
chown root:"$APP_USER" "$ENV_FILE"

systemctl daemon-reload
systemctl enable --now mpwg-radar-cooker.timer
systemctl start mpwg-radar-cooker.service || true

echo
echo "Installed MPWG radar cooker."
echo "  App:     $APP_ROOT"
echo "  Env:     $ENV_FILE"
echo "  Timer:   systemctl status mpwg-radar-cooker.timer"
echo "  Logs:    journalctl -u mpwg-radar-cooker.service -n 100 -f"
echo "  Smoke:   sudo -u $APP_USER $APP_ROOT/.venv/bin/mpwg-radar smoke --out $DATA_ROOT/output/smoke"
echo
echo "Edit $ENV_FILE with R2 credentials, then: systemctl start mpwg-radar-cooker.service"
