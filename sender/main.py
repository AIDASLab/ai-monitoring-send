"""Sender main loop.

Collects Claude + Codex usage from this server and delivers a batch to the NAS
every `interval_seconds`. Two delivery transports (see config.transport.mode):
  - "ssh"   : server has NO NAS mount -> scp the batch to the Synology over SSH
              (password auto-typed). This is the default for GPU servers.
  - "local" : server HAS the NAS mounted -> write straight into the mount.

Batches are first written to a local outbox (data/outbox/), then delivered.
A failed delivery leaves the batch queued and is retried next cycle, so a NAS
outage never loses data. Delivered batches are removed locally.

    PYTHONPATH=. python3 -m sender.main            # loop, uses ./config.json
    PYTHONPATH=. python3 -m sender.main --once     # one batch then exit (testing)
"""
from __future__ import annotations

import argparse
import copy
import getpass
import glob
import json
import os
import socket
import sys
import time
import traceback

try:
    from .claude_collector import ClaudeCollector
    from .codex_collector import CodexCollector
    from . import nas_writer, transport
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sender.claude_collector import ClaudeCollector
    from sender.codex_collector import CodexCollector
    from sender import nas_writer, transport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "data", "sender-state.json")
OUTBOX = os.path.join(ROOT, "data", "outbox")
MAX_OUTBOX = 5000  # safety cap if the NAS is unreachable for a very long time

DEFAULTS = {
    "node_id": socket.gethostname(),
    "interval_seconds": 300,
    "retain_hours": 72,
    "compress": True,
    "nas_root": "/mnt/nas/yunseok/ai-monitoring",   # used only by transport.mode=local
    "transport": {
        "mode": "ssh",                              # "ssh" (no mount) or "local"
        "ssh_host": "aidaslab.synology.me",
        "ssh_port": 2244,
        "ssh_user": "synologynas",
        "ssh_password": "",                         # set by setup.sh (gitignored)
        "ssh_key": "",                              # optional: path to a private key instead
        "remote_root": "/volume1/nas-nfs/yunseok/ai-monitoring",
    },
    "claude": {"enabled": True, "config_dirs": []},
    "codex": {"enabled": True, "dirs": ["~/.codex"], "include_archived": False},
    # real 5h/weekly utilization via Anthropic OAuth usage endpoint (per account)
    "claude_usage": {"enabled": True, "interval_seconds": 300},
}


def _primary_ip():
    """Best-effort outbound-facing local IP.

    node_id is often just a hostname (sometimes a generic default like
    "servername"), which is not enough to SSH back to the box. A UDP socket
    connect() sends no packets — it only asks the kernel which local address
    would be used for that route.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _os_user():
    """OS account the sender runs as (it reads THAT user's ~/.claude*/~/.codex).

    getpass.getuser() consults the environment first, so fall back to the real
    uid's passwd entry — a stale SUDO_USER/LOGNAME must not mislabel the node.
    """
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001  (non-POSIX, or no passwd entry)
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001
            return None


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        # New Claude cursors are dicts (offset + account provenance).  Keep
        # legacy [size, mtime] records readable during a rolling upgrade.
        state = {}
        for path, value in raw.items():
            if isinstance(value, dict):
                state[path] = dict(value)
            elif isinstance(value, (list, tuple)) and len(value) >= 2:
                state[path] = tuple(value)
        return state
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # json natively serializes legacy tuples as arrays and preserves
            # structured cursor records without flattening their provenance.
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def detect_claude_dirs():
    found = []
    for path in sorted(glob.glob(os.path.expanduser("~/.claude*"))):
        if os.path.isdir(path) and (
            os.path.isdir(os.path.join(path, "projects"))
            or os.path.isdir(os.path.join(path, "sessions"))
        ):
            found.append(path)
    return found or [os.path.expanduser("~/.claude")]


def build_collectors(cfg, state):
    host = cfg["node_id"]
    cu = cfg.get("claude_usage") or {}
    collectors = []
    if cfg["claude"]["enabled"]:
        for d in (cfg["claude"]["config_dirs"] or detect_claude_dirs()):
            collectors.append(ClaudeCollector(
                d, host=host, file_state=copy.deepcopy(state),
                usage_enabled=cu.get("enabled", True),
                usage_interval=cu.get("interval_seconds", 300)))
    if cfg["codex"]["enabled"]:
        for d in cfg["codex"].get("dirs", ["~/.codex"]):
            collectors.append(CodexCollector(
                d, host=host, file_state=copy.deepcopy(state),
                include_archived=cfg["codex"].get("include_archived", False)))
    return collectors


def _reset_collector_state(collectors, state):
    """Make the durable state the only cursor source for the next poll."""
    for collector in collectors:
        collector.file_state = copy.deepcopy(state)


def collect_all(cfg, collectors, state):
    """Collect a batch without acknowledging its cursors yet.

    Cursor advancement must be committed only after the matching batch exists
    durably in the local outbox.  Otherwise a process crash or disk error can
    skip bytes that were never delivered.  A separate candidate state also
    lets a failed collector roll back any partial in-memory mutation.
    """
    accounts, usage, sessions = [], [], []
    pending_state = copy.deepcopy(state)
    for c in collectors:
        c.file_state = pending_state
        before_collector = copy.deepcopy(pending_state)
        try:
            part = c.collect()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            pending_state = before_collector
            continue
        if part.get("account"):
            accounts.append(part["account"])
        usage.extend(part.get("usage", []))
        sessions.extend(part.get("sessions", []))
        updated = part.get("file_state") or {}
        if isinstance(updated, dict):
            pending_state.update(updated)
    return {
        "schema": 1, "host": cfg["node_id"],
        "generated_at": int(time.time() * 1000),
        # Self-identification: which install on which account is reporting.
        # Without this a node is only known by node_id, and finding the sender
        # again (to redeploy or debug) means hunting the filesystem by hand.
        "sender_root": ROOT,
        "os_user": _os_user(),
        "fqdn": socket.getfqdn(),
        "ip": _primary_ip(),
        "accounts": accounts, "usage": usage, "sessions": sessions,
    }, pending_state


def _outbox_batches():
    os.makedirs(OUTBOX, exist_ok=True)
    return sorted(f for f in os.listdir(OUTBOX) if f.startswith("batch-"))


def _cap_outbox():
    files = _outbox_batches()
    if len(files) > MAX_OUTBOX:
        for fn in files[:len(files) - MAX_OUTBOX]:
            try:
                os.remove(os.path.join(OUTBOX, fn))
            except OSError:
                pass
        print(f"[sender] WARNING: outbox over {MAX_OUTBOX}; dropped oldest",
              file=sys.stderr)


def run_once(cfg, collectors, state, tport):
    result, pending_state = collect_all(cfg, collectors, state)
    ms = int(time.time() * 1000)
    name = f"batch-{ms:013d}.json" + (".gz" if cfg["compress"] else "")
    local_path = os.path.join(OUTBOX, name)
    try:
        nbytes = nas_writer.build_gz(local_path, result, cfg["compress"])
        # The outbox file is durable before its byte cursors are acknowledged.
        # A delivery failure is fine: the durable batch is retried below.
        save_state(pending_state)
    except Exception:
        # Do not let a failed local transaction advance in-memory cursors.  If
        # state persistence failed, discard this unsafely-unacknowledged batch
        # so the next poll recollects it from the last durable cursor.
        try:
            os.remove(local_path)
        except OSError:
            pass
        _reset_collector_state(collectors, state)
        raise
    state.clear()
    state.update(pending_state)
    _reset_collector_state(collectors, state)
    _cap_outbox()

    # flush the outbox oldest-first; stop on first failure (preserve order, retry)
    pending = _outbox_batches()
    sent, failed = 0, None
    for fn in pending:
        path = os.path.join(OUTBOX, fn)
        try:
            tport.deliver(path, cfg["node_id"], fn)
            os.remove(path)
            sent += 1
        except Exception as e:  # noqa: BLE001
            failed = str(e)
            break
    remaining = len(_outbox_batches())
    if sent:
        try:
            tport.prune(cfg["node_id"], cfg["retain_hours"])
        except Exception:  # noqa: BLE001
            pass

    accts = ", ".join(sorted({a.get("email", "?") for a in result["accounts"]})) or "none"
    msg = (f"[sender] batch {name} ({nbytes} B) usage={len(result['usage'])} "
           f"sessions={len(result['sessions'])} accounts=[{accts}] "
           f"delivered={sent} queued={remaining} via {tport.describe()}")
    if failed:
        msg += f"  DELIVERY FAILED: {failed}"
    print(msg, file=sys.stderr, flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIDAS monitoring sender")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.interval:
        cfg["interval_seconds"] = args.interval
    state = load_state()
    collectors = build_collectors(cfg, state)
    tport = transport.make_transport(cfg)

    print(f"[sender] node={cfg['node_id']} transport={tport.describe()} "
          f"interval={cfg['interval_seconds']}s", file=sys.stderr)

    if args.once:
        run_once(cfg, collectors, state, tport)
        return
    while True:
        try:
            run_once(cfg, collectors, state, tport)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(cfg["interval_seconds"])


if __name__ == "__main__":
    main()
