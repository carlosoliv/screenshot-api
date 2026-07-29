#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f venv/bin/uvicorn ]; then
  echo "venv missing or incomplete. Run ./setup.sh first."
  exit 1
fi

mkdir -p logs

# Optional: install pm2 globally only if missing
if ! command -v pm2 >/dev/null 2>&1; then
  echo "pm2 not found. Install with: sudo npm install -g pm2"
  exit 1
fi

pm2 delete screenshot-api 2>/dev/null || true
pm2 start ecosystem.config.cjs
pm2 save
pm2 status

