#!/usr/bin/env bash
# Show sender status: process, last log lines, and latest batch on the NAS.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ -f data/sender.pid ] && kill -0 "$(cat data/sender.pid)" 2>/dev/null; then
  echo "running (pid $(cat data/sender.pid))"
else
  echo "NOT running"
fi
NAS_ROOT="$("$PY" -c "import json;print(json.load(open('config.json')).get('nas_root','?'))" 2>/dev/null || echo '?')"
NODE_ID="$("$PY" -c "import json;print(json.load(open('config.json')).get('node_id','?'))" 2>/dev/null || echo '?')"
echo "node=$NODE_ID  nas=$NAS_ROOT"
DIR="$NAS_ROOT/inbox/$NODE_ID"
echo "latest batches in $DIR:"
ls -1t "$DIR" 2>/dev/null | head -3 | sed 's/^/  /' || echo "  (none yet / NAS not mounted)"
echo "--- last log lines ---"
tail -n 8 data/sender.log 2>/dev/null || echo "  (no log yet)"
