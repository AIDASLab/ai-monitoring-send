#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f data/sender.pid ]; then
  PID="$(cat data/sender.pid)"
  if kill "$PID" 2>/dev/null; then echo "stopped pid $PID"; else echo "not running"; fi
  rm -f data/sender.pid
else
  echo "no pid file (data/sender.pid)"
fi
