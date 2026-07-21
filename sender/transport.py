"""Batch delivery transports.

- LocalTransport : this server has the NAS mounted -> write into the mount.
- SshTransport   : this server has NO mount -> scp the batch to the NAS over SSH
                   (password auto-typed via sshcmd), then atomically rename it
                   into place remotely.

Both expose deliver(local_gz_path, node_id, final_name) and
prune(node_id, retain_hours). deliver() raises on failure so the caller keeps
the file queued in the local outbox and retries next cycle.
"""
from __future__ import annotations

import os
import shutil

from . import sshcmd


def _remote_join(*parts):
    """Join remote path parts, PRESERVING a leading slash on the first part.

    (A naive strip('/') on every part turns an absolute remote_root like
    /volume1/... into a relative path under the SSH user's home — files then
    land in ~/volume1/... instead of the NAS share.)
    """
    parts = [p for p in parts if p]
    if not parts:
        return ""
    head = parts[0].rstrip("/")
    tail = [p.strip("/") for p in parts[1:]]
    return "/".join([head] + [t for t in tail if t])


class LocalTransport:
    def __init__(self, nas_root):
        self.nas_root = os.path.expanduser(nas_root)

    def _dir(self, node_id):
        d = os.path.join(self.nas_root, "inbox", node_id)
        os.makedirs(d, exist_ok=True)
        return d

    def deliver(self, local_gz, node_id, final_name):
        d = self._dir(node_id)
        tmp = os.path.join(d, "up-" + final_name)
        final = os.path.join(d, final_name)
        shutil.copyfile(local_gz, tmp)      # copy into the NAS fs
        os.replace(tmp, final)              # atomic rename within the NAS fs

    def prune(self, node_id, retain_hours):
        import time
        d = os.path.join(self.nas_root, "inbox", node_id)
        if not os.path.isdir(d):
            return
        cutoff = time.time() - retain_hours * 3600
        for fn in os.listdir(d):
            if fn.startswith("batch-") or fn.startswith("up-"):
                p = os.path.join(d, fn)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass

    def describe(self):
        return f"local:{self.nas_root}"


class SshTransport:
    def __init__(self, cfg):
        self.cfg = cfg                       # transport sub-dict
        self.remote_root = cfg["remote_root"]

    def _remotedir(self, node_id):
        return _remote_join(self.remote_root, "inbox", node_id)

    def deliver(self, local_gz, node_id, final_name):
        rd = self._remotedir(node_id)
        # mkdir -p every time (idempotent + cheap): do NOT cache success, or a
        # transient failure leaves us scp-ing into a dir that never got created.
        sshcmd.ssh_exec(self.cfg, f"mkdir -p {sshcmd.shquote(rd)}")
        tmp = _remote_join(rd, "up-" + final_name)
        final = _remote_join(rd, final_name)
        sshcmd.scp_put(self.cfg, local_gz, tmp)
        # atomic rename on the NAS so the central never reads a partial file
        sshcmd.ssh_exec(self.cfg, f"mv -f {sshcmd.shquote(tmp)} {sshcmd.shquote(final)}")

    def prune(self, node_id, retain_hours):
        rd = self._remotedir(node_id)
        mins = int(retain_hours * 60)
        # delete old batches and any stray temp uploads (best-effort)
        cmd = (f"find {sshcmd.shquote(rd)} -maxdepth 1 -type f "
               f"\\( -name 'batch-*' -mmin +{mins} -o -name 'up-*' -mmin +60 \\) "
               f"-delete 2>/dev/null || true")
        sshcmd.ssh_exec(self.cfg, cmd)

    def describe(self):
        return f"ssh:{self.cfg['ssh_user']}@{self.cfg['ssh_host']}:{self.cfg['ssh_port']}{self.remote_root}"


def make_transport(cfg):
    """cfg is the full sender config; returns a transport per cfg['transport']."""
    tcfg = cfg.get("transport") or {}
    mode = tcfg.get("mode", "ssh")
    if mode == "local":
        return LocalTransport(cfg.get("nas_root", tcfg.get("remote_root")))
    if mode == "ssh":
        missing = [k for k in ("ssh_host", "ssh_user", "remote_root") if not tcfg.get(k)]
        if missing:
            raise ValueError(f"transport.mode=ssh but missing {missing}")
        if not tcfg.get("ssh_password") and not tcfg.get("ssh_key"):
            raise ValueError("transport.mode=ssh needs ssh_password or ssh_key")
        return SshTransport(tcfg)
    raise ValueError(f"unknown transport.mode {mode!r}")
