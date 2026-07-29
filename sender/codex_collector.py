"""Codex (OpenAI) collector (sender side).

Reads a Codex config dir (default ~/.codex) and produces a partial CollectResult
tagged provider="codex", so it merges with Claude data on the dashboard.

On-disk format (Codex CLI):
  ~/.codex/auth.json                          -> tokens.id_token (JWT w/ email+plan)
  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl -> session transcript:
     - {"type":"session_meta","payload":{id,cwd,timestamp,originator,cli_version,...}}
     - {"type":"turn_context","payload":{model,...}}
     - {"type":"event_msg","payload":{"type":"token_count","info":{
          "last_token_usage":{input_tokens,cached_input_tokens,output_tokens,
                              reasoning_output_tokens,total_tokens}, ...}}}

Token mapping to the shared 4-component schema (so totals stay consistent):
  cache_read     = cached_input_tokens
  input          = input_tokens - cached_input_tokens   (uncached portion)
  output         = output_tokens                         (reasoning is a subset)
  cache_creation = 0
  -> total = input + output + cache_read == codex total_tokens

We record per-turn deltas (last_token_usage) with the event timestamp so the
charts work; summed they equal the session's cumulative total.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import time
from datetime import datetime, timezone

RECENT_SESSION_MS = 24 * 3600 * 1000  # re-report a session row if active within this


def _norm_codex_rate_limits(rl, event_ts_ms=None):
    """Map codex rate_limits {primary(300m), secondary(10080m)} to the shared
    {five_hour, seven_day} shape with ISO reset times (the real 5h/weekly %).

    Newer CLIs report an absolute ``resets_at`` (epoch seconds); some builds
    only report a relative ``resets_in_seconds`` from the event time, so both
    are accepted.
    """
    if not isinstance(rl, dict):
        return None
    out = {}
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        wm = w.get("window_minutes")
        key = "five_hour" if wm == 300 else ("seven_day" if wm == 10080 else None)
        if not key:
            continue
        iso = None
        epoch = w.get("resets_at")
        if epoch is None and w.get("resets_in_seconds") is not None and event_ts_ms:
            epoch = event_ts_ms / 1000 + w["resets_in_seconds"]
        if epoch:
            try:
                iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
            except (ValueError, OSError, TypeError):
                iso = None
        out[key] = {"utilization": w["used_percent"], "resets_at": iso}
    return out or None


def iso_to_ms(ts):
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _basename(cwd):
    return os.path.basename((cwd or "").rstrip("/")) or (cwd or "")


def _decode_jwt_claims(token):
    """Return the (unverified) payload claims of a JWT, or {}.

    We only read identity claims (email, plan); we never store the token itself.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


class CodexCollector:
    def __init__(self, codex_dir="~/.codex", host=None, file_state=None,
                 include_archived=False):
        self.codex_dir = os.path.expanduser(codex_dir)
        self.host = host or socket.gethostname()
        self.file_state = file_state if file_state is not None else {}
        self.include_archived = include_archived
        # Cached PER WINDOW so a newer event that only carries the weekly
        # window can't erase the last known 5h value; reset on account switch
        # so the previous user's percentage never shows on the new user's card.
        self._rl_cache = {}          # {window: (ts_ms, data)}
        self._rl_cache_owner = None  # (account_id, email) the cache belongs to
        # Account-boundary protection.  A rollout carries no account of its own,
        # and a changed file is re-parsed in full, so labelling every turn with
        # whatever account is logged in *now* would retroactively relabel turns
        # written under a previous login.  A turn is only attributable when its
        # own event timestamp falls after the previous poll AND the account was
        # unchanged across those two polls.
        self._last_poll_ms = 0
        self._last_account_identity = None

    # -- account --
    def read_account(self):
        path = os.path.join(self.codex_dir, "auth.json")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                auth = json.load(f)
        except (OSError, ValueError):
            return None
        tokens = auth.get("tokens") or {}
        account_id = tokens.get("account_id")
        claims = _decode_jwt_claims(tokens.get("id_token", ""))
        oauth = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}
        email = claims.get("email") or account_id
        plan = oauth.get("chatgpt_plan_type")
        orgs = oauth.get("organizations") or []
        org_name = None
        for o in orgs:
            if o.get("is_default"):
                org_name = o.get("title")
                break
        if not email:
            return None
        return {
            "provider": "codex",
            "email": email,
            "account_id": oauth.get("chatgpt_account_id") or account_id,
            "org_type": ("chatgpt_" + plan) if plan else "chatgpt",
            "rate_limit_tier": plan,
            "display_name": org_name,
            "org_name": org_name,
        }

    # -- rollout files --
    def _iter_rollouts(self):
        roots = [os.path.join(self.codex_dir, "sessions")]
        if self.include_archived:
            roots.append(os.path.join(self.codex_dir, "archived_sessions"))
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                        yield os.path.join(dirpath, fn)

    def _parse_rollout(self, path, email, attributable_after_ms=None):
        """``attributable_after_ms``: turns at or after this belong to ``email``;
        earlier ones are emitted as ``assumed`` so ingestion drops them instead
        of crediting them to the wrong login.  ``None`` = everything assumed."""
        meta = {}
        model = None
        usage = []
        seq = 0
        win_latest = {}       # {window: (event_ts_ms, {utilization, resets_at})}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t = d.get("type")
                    p = d.get("payload") or {}
                    if t == "session_meta":
                        meta = p
                    elif t == "turn_context":
                        model = p.get("model") or model
                    elif t == "event_msg" and p.get("type") == "token_count":
                        ts = iso_to_ms(d.get("timestamp"))
                        rl = p.get("rate_limits")        # real 5h/weekly % live here
                        plan = rl.get("plan_type") if isinstance(rl, dict) else None
                        norm = _norm_codex_rate_limits(rl, ts)
                        for k, v in (norm or {}).items():
                            prev = win_latest.get(k)
                            if prev is None or (ts or 0) >= prev[0]:
                                win_latest[k] = ((ts or 0), v)
                        info = p.get("info")
                        if not info:
                            continue
                        lu = info.get("last_token_usage") or {}
                        cached = lu.get("cached_input_tokens", 0) or 0
                        inp = lu.get("input_tokens", 0) or 0
                        out = lu.get("output_tokens", 0) or 0
                        if (inp + out) == 0:
                            continue
                        sid = meta.get("id") or _sid_from_name(path)
                        seq += 1
                        provable = bool(attributable_after_ms and ts
                                        and ts >= attributable_after_ms)
                        usage.append({
                            "uuid": f"codex:{sid}:{ts}:{seq}",
                            "provider": "codex",
                            "session_id": sid,
                            "project": _basename(meta.get("cwd")),
                            "cwd": meta.get("cwd"),
                            "git_branch": None,
                            "model": model,
                            "ts": ts,
                            "input_tokens": max(0, inp - cached),
                            "output_tokens": out,
                            "cache_creation_tokens": 0,
                            "cache_read_tokens": cached,
                            "service_tier": plan,
                            "request_id": None,
                            "version": meta.get("cli_version"),
                            "account_email": email if provable else None,
                            "assumed": not provable,
                        })
        except OSError:
            return {}, [], {}
        return meta, usage, win_latest

    def collect(self):
        account = self.read_account()
        email = account["email"] if account else None
        owner = ((account.get("account_id") or "", email) if account else None)
        if owner != self._rl_cache_owner:
            self._rl_cache = {}
            self._rl_cache_owner = owner
        # Only a login already in place at the previous poll can claim turns.
        account_stable = bool(owner and owner == self._last_account_identity)
        attributable_after_ms = self._last_poll_ms if account_stable else None
        now_ms = int(time.time() * 1000)
        records, updated, sessions = [], {}, []
        for path in self._iter_rollouts():
            try:
                st = os.stat(path)
            except OSError:
                continue
            mtime_ms = int(st.st_mtime * 1000)
            prev = self.file_state.get(path)
            changed = not (prev and prev[0] == st.st_size and prev[1] == st.st_mtime)
            meta, usage = (None, [])
            if changed:
                meta, usage, rl_wins = self._parse_rollout(
                    path, email, attributable_after_ms)
                records.extend(usage)
                self.file_state[path] = (st.st_size, st.st_mtime)
                updated[path] = [st.st_size, st.st_mtime]
                for k, (ts_w, v) in (rl_wins or {}).items():
                    prev = self._rl_cache.get(k)
                    if prev is None or ts_w >= prev[0]:
                        self._rl_cache[k] = (ts_w, v)  # most recent real % per window
            # only emit a session row for files that changed or were recently
            # active — avoids re-sending hundreds of old rollouts every cycle.
            if not changed and (now_ms - mtime_ms) > RECENT_SESSION_MS:
                continue
            if meta is None:
                meta = {"id": _sid_from_name(path)}
            sid = (meta or {}).get("id") or _sid_from_name(path)
            cwd = (meta or {}).get("cwd")
            sessions.append({
                "session_id": sid, "provider": "codex", "cwd": cwd,
                "project": _basename(cwd), "version": (meta or {}).get("cli_version"),
                "kind": "interactive", "entrypoint": (meta or {}).get("originator"),
                "status": "active", "pid": None, "pid_alive": None,
                "started_at": iso_to_ms((meta or {}).get("timestamp")),
                "updated_at": int(st.st_mtime * 1000),
                "account_email": email,
            })
        if account and self._rl_cache:
            rl_out = {"source": "codex_rollout"}
            newest = 0
            for k, (ts_w, v) in self._rl_cache.items():
                rl_out[k] = v
                newest = max(newest, ts_w)
            account["rate_limits"] = rl_out
            # rollout event time, not "whenever the sender happened to run"
            account["rate_limits_updated_at"] = newest or now_ms
        return {
            "account": account, "usage": records, "sessions": sessions,
            "file_state": updated,
        }


def _sid_from_name(path):
    # rollout-2026-05-21T07-49-20-019e4982-a891-70f2-87a8-729d0ca9ff79.jsonl
    base = os.path.basename(path)[:-6]  # strip .jsonl
    parts = base.split("-")
    if len(parts) >= 5:
        return "-".join(parts[-5:])
    return base
