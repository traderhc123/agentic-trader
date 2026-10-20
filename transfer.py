"""Move a setup to another computer.

Everything an install knows lives in one folder (AGENT_HOME, default
~/.agentic-trader): the agreement, config, broker login, prepaid pass,
wallet keys, positions, trade history and rules. `export_bundle` packs it
into ONE file locked with a passphrase the owner picks; `import_bundle`
unpacks it on the new computer.

The file is encrypted with Python's standard library only (no new
dependency for existing installs to pip-install):
    key   = scrypt(passphrase, salt, n=2**15, r=8, p=1) -> 64 bytes
            (32 for encryption, 32 for the MAC)
    data  = zip XOR keystream, keystream block i = HMAC-SHA256(enc, nonce||i)
    tag   = HMAC-SHA256(mac, header || ciphertext)   (checked before decrypting)
Wrong passphrase or a damaged file fails the tag check; nothing is written.

Two computers must never run the same setup (every order would be placed
twice in the same broker account), so after an export the old computer is
stopped, start-at-login is turned off, and a MOVED_AWAY marker makes
`agent.py run` refuse to start there until the owner runs
`agent.py resume-here`. An imported setup starts in dry-run.
"""
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import time
import zipfile

MAGIC = b"AGENTIC-TRADER-MOVE-1\n"
EXTENSION = ".atmove"
MIN_PASSPHRASE = 8
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 64}

# Machine-specific or transient files that must not travel.
_SKIP_NAMES = {
    "agent.lock", "agent.pid", "stop.request", "ui_token", ".acl-locked",
    "launchd.log", "MOVED_AWAY",
}
_SKIP_PREFIXES = ("agent-run.log",)
_SKIP_SUFFIXES = (".tmp", ".lock")
MAX_BUNDLE_BYTES = 200 * 1024 * 1024


class TransferError(Exception):
    pass


def home():
    return os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))


def _moved_path():
    return os.path.join(home(), "MOVED_AWAY")


# ── crypto ───────────────────────────────────────────────────────────────────

def _keys(passphrase, salt):
    k = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, **_SCRYPT)
    return k[:32], k[32:]


def _xor_stream(key, nonce, data):
    out = bytearray(len(data))
    mv = memoryview(data)
    for i in range(0, len(data), 32):
        block = hmac.new(key, nonce + struct.pack(">Q", i // 32),
                         hashlib.sha256).digest()
        chunk = mv[i:i + 32]
        n = len(chunk)
        out[i:i + n] = (int.from_bytes(chunk, "big")
                        ^ int.from_bytes(block[:n], "big")).to_bytes(n, "big")
    return bytes(out)


def encrypt(payload, passphrase):
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    enc, mac = _keys(passphrase, salt)
    header = MAGIC + salt + nonce
    body = _xor_stream(enc, nonce, payload)
    tag = hmac.new(mac, header + body, hashlib.sha256).digest()
    return header + body + tag


def decrypt(blob, passphrase):
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 64:
        raise TransferError("that isn't an agentic-trader move file")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    nonce = blob[len(MAGIC) + 16:len(MAGIC) + 32]
    body, tag = blob[len(MAGIC) + 32:-32], blob[-32:]
    enc, mac = _keys(passphrase, salt)
    want = hmac.new(mac, blob[:len(MAGIC) + 32] + body, hashlib.sha256).digest()
    if not hmac.compare_digest(want, tag):
        raise TransferError("wrong passphrase, or the file is damaged")
    return _xor_stream(enc, nonce, body)


# ── export ───────────────────────────────────────────────────────────────────

def _travels(rel):
    name = os.path.basename(rel)
    return not (name in _SKIP_NAMES or name.startswith(_SKIP_PREFIXES)
                or name.endswith(_SKIP_SUFFIXES))


def _files():
    root = home()
    out = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for n in names:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if _travels(rel) and os.path.isfile(full):
                out.append((rel, full))
    return sorted(out)


def export_bundle(passphrase):
    """Encrypted bytes of the whole setup. Raises TransferError."""
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise TransferError(f"pick a passphrase of at least {MIN_PASSPHRASE} characters")
    files = _files()
    if not any(rel == "config.json" for rel, _ in files):
        raise TransferError("there's no setup on this computer to move yet")
    buf = io.BytesIO()
    import platform
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({
            "format": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from_os": platform.system(), "files": [rel for rel, _ in files]}))
        for rel, full in files:
            z.write(full, "home/" + rel)
    return encrypt(buf.getvalue(), passphrase)


def export_filename():
    return f"agentic-trader-move-{time.strftime('%Y-%m-%d')}{EXTENSION}"


# ── after export: retire this computer ───────────────────────────────────────

def mark_moved_away():
    """Turn off start-at-login and leave a marker so this computer won't run
    the setup again by accident. Returns a short note."""
    import keep_running
    keep_running.disable_autostart()
    os.makedirs(home(), exist_ok=True)
    with open(_moved_path(), "w", encoding="utf-8") as f:
        json.dump({"moved_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}, f)
    return "start-at-login turned off; this computer won't run the agent again"


def moved_away():
    """The marker's contents if this setup was moved away, else None."""
    try:
        with open(_moved_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def resume_here():
    try:
        os.remove(_moved_path())
        return True
    except OSError:
        return False


# ── import ───────────────────────────────────────────────────────────────────

def existing_setup():
    """True if this computer already has a configured setup."""
    try:
        with open(os.path.join(home(), "config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("source"))
    except (OSError, ValueError):
        return False


def import_bundle(blob, passphrase, replace=False):
    """Decrypt and install a move file into AGENT_HOME. Returns a summary
    dict. Raises TransferError (nothing written) on any problem."""
    if len(blob) > MAX_BUNDLE_BYTES:
        raise TransferError("that file is too large to be a move file")
    payload = decrypt(blob, passphrase)
    try:
        z = zipfile.ZipFile(io.BytesIO(payload))
        manifest = json.loads(z.read("manifest.json"))
    except Exception:
        raise TransferError("the move file is damaged")
    if manifest.get("format") != 1:
        raise TransferError("this move file is from a newer version — update "
                            "agentic-trader on this computer first")
    members = [m for m in z.namelist() if m.startswith("home/")]
    for m in members:
        rel = m[len("home/"):]
        parts = rel.split("/")
        if (not rel or rel.startswith("/") or ".." in parts or ":" in rel
                or "\\" in rel):
            raise TransferError("the move file contains an unsafe path")
    if not any(m == "home/config.json" for m in members):
        raise TransferError("the move file has no setup in it")
    if existing_setup() and not replace:
        raise TransferError("NEEDS_REPLACE")

    root = home()
    os.makedirs(root, exist_ok=True)
    import platform_compat
    platform_compat.make_private(root, is_dir=True)
    for m in members:
        rel = m[len("home/"):]
        if not _travels(rel):
            continue
        dest = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".import-tmp"
        with open(tmp, "wb") as f:
            f.write(z.read(m))
        if not platform_compat.IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, dest)

    # Safety: the old computer may still be running. Start in dry-run until
    # the owner confirms it's off and says `dry off`.
    cfg_path = os.path.join(root, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    was_live = not cfg.get("dry_run")
    cfg["dry_run"] = True
    tmp = cfg_path + ".import-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, cfg_path)
    resume_here()  # a fresh import is meant to run here
    return {"files": len(members), "created_at": manifest.get("created_at"),
            "from_os": manifest.get("from_os"), "was_live": was_live,
            "source": cfg.get("source"), "broker": cfg.get("broker")}
