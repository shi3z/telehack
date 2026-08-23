#!/usr/bin/env bash
# Telehack 起動スクリプト: LiveKit スタック(Docker) + アプリサーバー
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r server/requirements.txt
fi

docker compose up -d

echo "LiveKit スタック起動。アプリサーバーを http://0.0.0.0:8800 で開始します"
cd server
exec ../.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8800
