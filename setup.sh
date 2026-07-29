#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Creating virtualenv..."
python3 -m venv venv

echo "==> Activating and installing Python packages..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing Playwright Chromium browser (Playwright's own binary)..."
playwright install chromium
# Optional system deps on Debian/Ubuntu (needed if you use Playwright's browser):
# playwright install-deps chromium

echo "==> Creating logs directory..."
mkdir -p logs

echo "==> Done. Run ./start.sh to launch with PM2."
