#!/usr/bin/env bash
# ONE-SHOT setup for a sender server.
#
#   git clone <repo> ai-monitoring-send && cd ai-monitoring-send
#   SSH_PASSWORD='Qwer!234' ./setup.sh
#
# Default transport is SSH (for servers WITHOUT the NAS mounted): the batch is
# scp'd to the Synology NAS and the password is typed automatically.
# Creates config.json (chmod 600) and starts the sender in the background.
# Safe to re-run. Override via env or flags:
#
#   SSH_PASSWORD=...  HOST_ID=gpu7  INTERVAL=300 ./setup.sh
#   ./setup.sh --password 'Qwer!234' --host gpu7
#   ./setup.sh --ssh-host aidaslab.synology.me --ssh-port 2244 --ssh-user synologynas \
#              --remote-root /volume1/nas-nfs/yunseok/ai-monitoring --password '...'
#   ./setup.sh --host gpu7 --claude-dir /data/work/.claude     # explicit .claude dir(s)
#   ./setup.sh --host gpu7 --claude-dir ~/.claude --claude-dir ~/.claude2   # repeatable
#   ./setup.sh --local --nas /mnt/nas/yunseok/ai-monitoring   # server HAS the mount
#   ./setup.sh --key ~/.ssh/id_ed25519                        # use an SSH key, no password
#   ./setup.sh --systemd                                      # print a systemd unit
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

# clones/copies sometimes drop the executable bit — restore it (best effort) so
# that `./setup.sh`, `./start.sh`, ... work afterwards even if it was lost.
chmod +x ./*.sh 2>/dev/null || true

MODE="ssh"
NODE_ID="${HOST_ID:-${NODE_ID:-$(hostname)}}"
INTERVAL="${INTERVAL:-300}"
SSH_HOST="${SSH_HOST:-aidaslab.synology.me}"
SSH_PORT="${SSH_PORT:-2244}"
SSH_USER="${SSH_USER:-synologynas}"
SSH_PASSWORD="${SSH_PASSWORD:-}"
SSH_KEY="${SSH_KEY:-}"
REMOTE_ROOT="${REMOTE_ROOT:-/volume1/nas-nfs/yunseok/ai-monitoring}"
NAS_ROOT="${NAS_ROOT:-/mnt/nas/yunseok/ai-monitoring}"
USE_SYSTEMD=0
CLAUDE_DIRS=()    # explicit Claude config dir(s); empty => auto-detect ~/.claude*
CODEX_DIRS=()     # explicit Codex dir(s); empty => default ~/.codex

while [ $# -gt 0 ]; do
  case "$1" in
    --local) MODE="local"; shift;;
    --nas) NAS_ROOT="$2"; shift 2;;
    --host) NODE_ID="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --ssh-host) SSH_HOST="$2"; shift 2;;
    --ssh-port) SSH_PORT="$2"; shift 2;;
    --ssh-user) SSH_USER="$2"; shift 2;;
    --password) SSH_PASSWORD="$2"; shift 2;;
    --key) SSH_KEY="$2"; shift 2;;
    --remote-root) REMOTE_ROOT="$2"; shift 2;;
    --claude-dir) CLAUDE_DIRS+=("$2"); shift 2;;   # repeatable: explicit .claude dir
    --codex-dir) CODEX_DIRS+=("$2"); shift 2;;     # repeatable: explicit .codex dir
    --systemd) USE_SYSTEMD=1; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

command -v "$PY" >/dev/null || { echo "python3 not found (set PYTHON=...)"; exit 1; }

# ask for the password if SSH mode and neither password nor key was given
if [ "$MODE" = "ssh" ] && [ -z "$SSH_PASSWORD" ] && [ -z "$SSH_KEY" ]; then
  printf "SSH password for %s@%s: " "$SSH_USER" "$SSH_HOST" >&2
  read -rs SSH_PASSWORD; echo >&2
fi

# 1) create config.json (chmod 600 — it can hold the password)
if [ ! -f config.json ]; then
  echo "creating config.json (mode=$MODE, node=$NODE_ID)"
  CLAUDE_DIRS_ENV=""; CODEX_DIRS_ENV=""
  if [ ${#CLAUDE_DIRS[@]} -gt 0 ]; then CLAUDE_DIRS_ENV=$(printf '%s\n' "${CLAUDE_DIRS[@]}"); fi
  if [ ${#CODEX_DIRS[@]} -gt 0 ]; then CODEX_DIRS_ENV=$(printf '%s\n' "${CODEX_DIRS[@]}"); fi
  CLAUDE_DIRS_ENV="$CLAUDE_DIRS_ENV" CODEX_DIRS_ENV="$CODEX_DIRS_ENV" \
  "$PY" - "$MODE" "$NODE_ID" "$INTERVAL" "$SSH_HOST" "$SSH_PORT" "$SSH_USER" \
        "$SSH_PASSWORD" "$SSH_KEY" "$REMOTE_ROOT" "$NAS_ROOT" <<'PYEOF'
import json, sys, os
mode,node,interval,host,port,user,pw,key,remote,nas = sys.argv[1:11]
cfg = json.load(open("config.example.json"))
cfg["node_id"] = node
cfg["interval_seconds"] = int(interval)
cfg["nas_root"] = nas
cfg["transport"] = {
    "mode": mode, "ssh_host": host, "ssh_port": int(port), "ssh_user": user,
    "ssh_password": pw, "ssh_key": key, "remote_root": remote,
}
cd = [x for x in os.environ.get("CLAUDE_DIRS_ENV", "").splitlines() if x.strip()]
xd = [x for x in os.environ.get("CODEX_DIRS_ENV", "").splitlines() if x.strip()]
if cd:
    cfg["claude"]["config_dirs"] = cd
if xd:
    cfg["codex"]["dirs"] = xd
json.dump(cfg, open("config.json", "w"), indent=2)
print("  wrote config.json" + (f"  claude_dirs={cd}" if cd else "")
      + (f"  codex_dirs={xd}" if xd else ""))
PYEOF
  chmod 600 config.json
else
  echo "config.json already exists — leaving it as-is (delete it to reconfigure)"
fi

# 2) one-shot test (actually collects + delivers one batch -> validates SSH/creds)
echo "running a one-shot collection + delivery test…"
PYTHONPATH=. "$PY" -m sender.main --once || {
  echo "one-shot failed — check the SSH host/port/user/password above"; exit 1; }

if [ "$USE_SYSTEMD" = "1" ]; then
  cat <<UNIT

# --- copy to /etc/systemd/system/aidas-sender.service then: systemctl enable --now ---
[Unit]
Description=AIDAS monitoring sender
After=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
ExecStart=$(command -v "$PY") -m sender.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
  exit 0
fi

# 3) start in the background (use `bash` so it works even before chmod takes hold)
bash start.sh
echo "Done. The central dashboard will show host '$NODE_ID' within ~1 minute."
