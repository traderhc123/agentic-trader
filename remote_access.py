"""Phone remote: pair the agentic-trader phone app with THIS agent.

The phone talks to the agent the owner runs (their computer or their own
VPS); there is no server in between and broker logins never leave this
machine. How it fits together:

  * The agent serves a second, phone-only API on 127.0.0.1:REMOTE_PORT
    (webui.start_remote). Like the dashboard it is loopback-only; the owner
    publishes it to their own private network with Tailscale:
        tailscale serve --bg --https=443 http://127.0.0.1:8722
    which gives an HTTPS address only the owner's own devices can reach.
  * ``python agent.py pair`` makes a ONE-TIME pairing code (10 minutes) and
    shows it as a QR code. The app sends the code once and gets back its
    own device token. Only a SHA-256 of each token is stored here, so this
    file alone cannot be used to log in.
  * Every phone request carries ``Authorization: Bearer <device token>``.
    A device is revoked with ``python agent.py devices revoke <id>``.

Stdlib only. Never raises into the trading loop: the file helpers fail soft.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import quote

REMOTE_PORT = 8722
PAIR_TTL_S = 600            # a pairing code is good for 10 minutes, once
MAX_PAIR_FAILS = 10         # wrong codes in a row before pairing locks ...
PAIR_LOCK_S = 600           # ... for this long
MAX_DEVICES = 10
_SEEN_EVERY_S = 300         # write last_seen at most this often per device
APP_SCHEME = "agentictrader"

_lock = threading.RLock()
_fails = {"n": 0, "until": 0.0}


def _path():
    home = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
    return os.path.join(home, "devices.json")


def _sha(s):
    return hashlib.sha256(str(s).encode()).hexdigest()


def _load():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("devices", [])
            return d
    except (OSError, ValueError):
        pass
    return {"devices": []}


def _save(d):
    import platform_compat
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    platform_compat.make_private(tmp)
    os.replace(tmp, path)


def _clean_name(name):
    s = "".join(ch for ch in str(name or "") if ch.isprintable())
    return s.strip()[:60] or "Phone"


# ── pairing ──────────────────────────────────────────────────────────────────

def new_pairing_code(now=None):
    """Start pairing: a fresh one-time code (replaces any earlier one)."""
    now = time.time() if now is None else now
    code = secrets.token_urlsafe(18)
    with _lock:
        d = _load()
        d["pairing"] = {"code_sha256": _sha(code), "expires": now + PAIR_TTL_S}
        _save(d)
    return code


def pairing_link(url, code):
    """What the QR code holds: the app opens it and pairs in one step."""
    return f"{APP_SCHEME}://pair?u={quote(url, safe='')}&c={quote(code, safe='')}"


def pair(code, name="", push_token=None, now=None):
    """Swap a valid pairing code for a new device token.

    Returns ``{"device_id", "token"}`` or ``{"error"}``. The code works once;
    after MAX_PAIR_FAILS wrong codes pairing is locked for PAIR_LOCK_S."""
    now = time.time() if now is None else now
    with _lock:
        if now < _fails["until"]:
            return {"error": "too many wrong codes — wait 10 minutes, then "
                             "run `python agent.py pair` again"}
        d = _load()
        p = d.get("pairing") or {}
        ok = (bool(code) and isinstance(p.get("code_sha256"), str)
              and hmac.compare_digest(_sha(code), p["code_sha256"])
              and now <= float(p.get("expires") or 0))
        if not ok:
            _fails["n"] += 1
            if _fails["n"] >= MAX_PAIR_FAILS:
                _fails.update(n=0, until=now + PAIR_LOCK_S)
                d.pop("pairing", None)       # a locked-out code is dead too
                _save(d)
            return {"error": "pairing code is wrong or expired — run "
                             "`python agent.py pair` again"}
        _fails.update(n=0, until=0.0)
        d.pop("pairing", None)               # one use only
        if len(d["devices"]) >= MAX_DEVICES:
            _save(d)
            return {"error": f"already {MAX_DEVICES} phones paired — revoke "
                             "one with `python agent.py devices`"}
        token = secrets.token_urlsafe(32)
        dev = {"id": secrets.token_hex(4), "name": _clean_name(name),
               "token_sha256": _sha(token), "created": int(now),
               "last_seen": int(now)}
        if valid_push_token(push_token):
            dev["push_token"] = push_token
        d["devices"].append(dev)
        _save(d)
        return {"device_id": dev["id"], "token": token}


# ── devices ──────────────────────────────────────────────────────────────────

def device_for_token(token, now=None):
    """The paired device this bearer token belongs to, or None."""
    if not token or not isinstance(token, str):
        return None
    now = time.time() if now is None else now
    want = _sha(token)
    with _lock:
        d = _load()
        for dev in d["devices"]:
            if hmac.compare_digest(want, str(dev.get("token_sha256") or "")):
                if now - float(dev.get("last_seen") or 0) > _SEEN_EVERY_S:
                    dev["last_seen"] = int(now)
                    try:
                        _save(d)
                    except OSError:
                        pass
                return {k: v for k, v in dev.items() if k != "token_sha256"}
    return None


def list_devices():
    with _lock:
        return [{k: v for k, v in dev.items() if k not in ("token_sha256", "push_token")}
                | {"push": bool(dev.get("push_token"))}
                for dev in _load()["devices"]]


def revoke(device_id):
    """Remove a paired phone. True if one was removed."""
    with _lock:
        d = _load()
        keep = [x for x in d["devices"] if x.get("id") != device_id]
        if len(keep) == len(d["devices"]):
            return False
        d["devices"] = keep
        _save(d)
        return True


def valid_push_token(tok):
    """An Expo push token (ExponentPushToken[...] / ExpoPushToken[...])."""
    return (isinstance(tok, str) and len(tok) <= 200
            and tok.startswith(("ExponentPushToken[", "ExpoPushToken["))
            and tok.endswith("]"))


def set_push_token(device_id, tok):
    """Store (or clear, tok=None) where this phone receives notifications."""
    if tok is not None and not valid_push_token(tok):
        return False
    with _lock:
        d = _load()
        for dev in d["devices"]:
            if dev.get("id") == device_id:
                if tok is None:
                    dev.pop("push_token", None)
                else:
                    dev["push_token"] = tok
                _save(d)
                return True
    return False


def push_tokens():
    return [dev["push_token"] for dev in _load()["devices"]
            if valid_push_token(dev.get("push_token"))]


# ── Tailscale helper ─────────────────────────────────────────────────────────

def tailscale_url():
    """This machine's HTTPS address on the owner's tailnet, or None.

    (``tailscale serve`` answers on https://<machine>.<tailnet>.ts.net/.)"""
    import shutil
    import subprocess
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True,
                           text=True, timeout=10)
        name = (json.loads(r.stdout).get("Self") or {}).get("DNSName") or ""
    except Exception:
        return None
    name = name.rstrip(".")
    return f"https://{name}" if name else None
