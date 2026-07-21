#!/usr/bin/env bash
# Start the sender loop in the background (nohup). Logs: data/sender.log
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
mkdir -p data
if [ -f data/sender.pid ] && kill -0 "$(cat data/sender.pid)" 2>/dev/null; then
  echo "already running (pid $(cat data/sender.pid))"; exit 0
fi
nohup env PYTHONPATH=. "$PY" -m sender.main >> data/sender.log 2>&1 &
echo $! > data/sender.pid
echo "started pid $(cat data/sender.pid) — logs: data/sender.log"
