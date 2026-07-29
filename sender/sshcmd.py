"""Non-interactive ssh/scp helpers (stdlib only).

Password auth is automated with a pseudo-terminal (the `pty` module) — we spawn
ssh/scp, watch its output, and type the password when it prompts. This is what
`sshpass` does, but without needing sshpass/paramiko installed (GPU servers
usually have neither, only the openssh client).

Key auth (transport.ssh_key set, no password) skips the pty and runs in
BatchMode. Host-key prompts are suppressed (StrictHostKeyChecking=no with a
repo-local known_hosts) so nothing ever blocks on a tty.
"""
from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
KNOWN_HOSTS = os.path.join(DATA_DIR, "known_hosts")


class SshError(RuntimeError):
    pass


def _common_opts(cfg, for_scp=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    port = str(cfg.get("ssh_port", 22))
    opts = [
        ("-P" if for_scp else "-p"), port,
        "-o", "StrictHostKeyChecking=no",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=10",
        "-o", "LogLevel=ERROR",
    ]
    key = cfg.get("ssh_key")
    if key:
        opts += ["-i", os.path.expanduser(key), "-o", "BatchMode=yes",
                 "-o", "PreferredAuthentications=publickey"]
    else:
        opts += ["-o", "NumberOfPasswordPrompts=1",
                 "-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=password,keyboard-interactive"]
    return opts


def _run_with_password(argv, password, timeout=120):
    """Run argv in a pty, typing `password` at the first password prompt.

    Returns (exit_code, combined_output_str).
    """
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    output = bytearray()
    recent = bytearray()
    pw_sent = 0
    status = None
    deadline = time.time() + timeout
    try:
        while True:
            left = deadline - time.time()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return 124, bytes(output).decode("utf-8", "replace")
            r, _, _ = select.select([fd], [], [], min(1.0, left))
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                output += data
                recent += data
                low = bytes(recent).lower()
                if pw_sent < 1 and (b"password:" in low or b"passphrase" in low):
                    os.write(fd, password.encode() + b"\n")
                    pw_sent += 1
                    recent.clear()
                if len(recent) > 256:
                    del recent[:-128]
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                status = st
                break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if status is None:
        _, status = os.waitpid(pid, 0)
    if hasattr(os, "waitstatus_to_exitcode"):
        code = os.waitstatus_to_exitcode(status)
    else:  # pragma: no cover
        code = (status >> 8) if os.WIFEXITED(status) else 128
    return code, bytes(output).decode("utf-8", "replace")


def _run_batch(argv, timeout=120):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        raise SshError(f"{argv[0]} not found ({e})")


# transient SSH/network failures worth retrying within a cycle
_TRANSIENT = ("connection reset", "kex_exchange_identification", "connection refused",
              "connection closed", "connection timed out", "timed out",
              "broken pipe", "temporary failure", "no route to host", "reset by peer")
_BACKOFF = (2, 5, 10)


def _exec_once(cfg, argv):
    password = cfg.get("ssh_password")
    if password and not cfg.get("ssh_key"):
        return _run_with_password(argv, password)
    return _run_batch(argv)


def _exec(cfg, argv, retries=2):
    """Run an ssh/scp command, retrying transient connection errors with backoff."""
    code, out = 255, ""
    for attempt in range(retries + 1):
        code, out = _exec_once(cfg, argv)
        if code == 0:
            return code, out
        low = (out or "").lower()
        transient = code == 255 or any(t in low for t in _TRANSIENT)
        if attempt < retries and transient:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
            continue
        break
    return code, out


def ssh_exec(cfg, remote_cmd):
    """Run a shell command on the remote host."""
    target = f"{cfg['ssh_user']}@{cfg['ssh_host']}"
    argv = ["ssh"] + _common_opts(cfg) + [target, remote_cmd]
    code, out = _exec(cfg, argv)
    if code != 0:
        raise SshError(f"ssh '{remote_cmd}' failed (code {code}): {out.strip()[:300]}")
    return out


def scp_put(cfg, local_path, remote_path):
    """Copy a local file to remote_path.

    Uses `scp -O` to force the legacy (shell-based) SCP protocol. Newer OpenSSH
    (9.0+) defaults to SFTP, and if the server's SFTP subsystem is chrooted to
    the user's home (common on Synology), an absolute dest path like
    /volume1/... resolves INSIDE the chroot and fails with
    "dest open ...: No such file or directory". The shell-based protocol respects
    real absolute paths (same context as our mkdir/mv). Falls back to plain scp
    on very old clients that lack -O.
    """
    target = f"{cfg['ssh_user']}@{cfg['ssh_host']}:{remote_path}"
    base = _common_opts(cfg, for_scp=True)
    code, out = _exec(cfg, ["scp", "-O"] + base + [local_path, target])
    if code != 0 and _opt_unsupported(out):
        code, out = _exec(cfg, ["scp"] + base + [local_path, target])
    if code != 0:
        raise SshError(f"scp failed (code {code}): {out.strip()[:300]}")
    return out


def _opt_unsupported(out):
    """True if scp rejected an option (so we should retry without -O).
    Transfer errors like 'No such file or directory' don't match → they surface.
    """
    low = (out or "").lower()
    return ("unknown option" in low or "illegal option" in low
            or "invalid option" in low or "usage:" in low)


def shquote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"
