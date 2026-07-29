#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
USER_NAME="$(whoami)"
HOME_DIR="$HOME"

echo "==> Creating virtualenv..."
python3 -m venv venv

echo "==> Installing Python packages..."
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing Playwright Chromium..."
playwright install chromium

mkdir -p logs

if ! command -v pm2 >/dev/null 2>&1; then
  echo "ERROR: pm2 not found. Install it first:"
  echo "  sudo npm install -g pm2"
  exit 1
fi

echo "==> Starting app with PM2 (needed before save)..."
pm2 delete screenshot-api 2>/dev/null || true
pm2 start ecosystem.config.cjs

echo "==> Saving process list..."
pm2 save

echo "==> Enabling PM2 to start on system boot..."
# Generate and run the official startup command (requires sudo password once)
STARTUP_CMD="$(pm2 startup systemd -u "$USER_NAME" --hp "$HOME_DIR" | grep -E '^sudo ' || true)"

if [ -z "$STARTUP_CMD" ]; then
  echo "Could not parse startup command. Run manually:"
  echo "  pm2 startup"
  echo "Then copy and run the sudo line it prints, then: pm2 save"
  exit 1
fi

echo "Running: $STARTUP_CMD"
# shellcheck disable=SC2086
eval $STARTUP_CMD

echo "==> Saving process list again..."
pm2 save

echo ""
echo "Done. PM2 should restore your apps after reboot."
echo "Verify with:  systemctl status pm2-${USER_NAME}"
echo "              pm2 status"