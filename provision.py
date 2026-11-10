"""Automated VPS provisioning — the agent moves itself to a cloud server.

After local setup completes, the agent can create a small DigitalOcean
droplet (with the USER'S own API token, on the user's account), ship its
finished configuration there via cloud-init, and auto-start under systemd.
The token is used for the creation calls and NOT stored.

What gets copied to the droplet (the user's own files, to the user's own
server): config.json, acceptance.json, robinhood_oauth.json, policy.md, and
the agent's memory — state.json (open positions, signals already handled,
the paid day-pass) and trades.jsonl (today's rows ARE the daily entry cap and
loss stop). Without the memory the server would start blind: it could pay for
the day again and would not know which positions are its own to exit.

Two computers must never run the same setup (every order would be placed
twice), so once the server is created the dashboard retires THIS computer the
same way "move to another computer" does (transfer.mark_moved_away).

Secret-exposure note: cloud-init user-data carries these files (base64) and is,
by default, (a) stored on disk under /var/lib/cloud and (b) served for the life
of the droplet by the link-local metadata service (169.254.169.254) to ANY
process on the box. To shrink that blast radius, the runcmd below — after the
config is restored — shreds the local user-data copies and firewalls the
metadata endpoint for every user except root (the agent, which runs as
`trader`, needs neither once booted; DigitalOcean's own root-run droplet agent
needs the endpoint for the Web Console login). The copy held in the
user's DigitalOcean control panel remains until the droplet is rebuilt, and is
in the same trust domain as the server; if the droplet is ever destroyed,
rotate the Robinhood session and any API keys it carried.
"""

import base64
import calendar
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time

import requests

DO_API = "https://api.digitalocean.com/v2"
REGIONS = ["nyc3", "sfo3", "tor1", "lon1", "fra1", "sgp1"]
# Same-price fallbacks when the chosen region has no room for the smallest
# size (DigitalOcean pulls sizes per region without notice — nyc3 2026-10).
NEARBY = {"nyc3": ["tor1", "nyc1", "sfo3"], "tor1": ["nyc3", "nyc1", "sfo3"],
          "sfo3": ["sfo2", "tor1", "nyc3"], "lon1": ["ams3", "fra1"],
          "fra1": ["ams3", "lon1"], "sgp1": ["blr1", "syd1"]}
SIZE = "s-1vcpu-1gb"
# Only if no nearby region has SIZE: the next sizes up, in the chosen region.
SIZE_FALLBACKS = ["s-1vcpu-1gb-amd", "s-1vcpu-1gb-intel", "s-1vcpu-2gb"]
IMAGE = "ubuntu-24-04-x64"
SSH_KEY_NAME = "do_ssh_key"   # private key in AGENT_HOME; .pub next to it
METADATA_IP = "169.254.169.254"

_HOME_FILES = ["config.json", "acceptance.json", "robinhood_oauth.json",
               "policy.md"]
# The agent's memory. Sent gzip-compressed: DigitalOcean refuses user-data
# over 64 KiB, and a few months of trade log is bigger than that as-is.
_STATE_FILE = "state.json"
_LEDGER_FILE = "trades.jsonl"
USER_DATA_LIMIT = 60 * 1024   # DigitalOcean's cap is 64 KiB; leave headroom
# A deploy from a RUNNING agent's dashboard first waits for the heartbeat to
# finish the cycle it is in (an order being placed, its fill check, the state
# save), so the copy sent to the server is complete. Longer than this and the
# deploy is refused — nothing is created, nothing changes here.
CYCLE_WAIT_S = 90
# After the server is created, the agent on this computer is on hold at once
# (no orders) but its process — which also serves the page — keeps running
# until the page has been given the server's address and login command, plus
# a moment to load them; or, if DigitalOcean never reports an address, until
# the cap.
LOCAL_STOP_AFTER_SHOWN_S = 60
LOCAL_STOP_CAP_S = 15 * 60
# Marks "this install's server deploy was already handled or checked", so the
# one-time warning for deploys made by OLDER versions is not repeated.
DEPLOY_CHECKED_FILE = "do_deploy_checked"
_SERVICE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "deploy", "agentic-trader.service")

# Block the cloud metadata address for everyone EXCEPT root: it serves the
# user-data (secrets) to any process, but DigitalOcean's own droplet agent
# (root) needs it for the Web Console login. A unit so it survives reboots.
_GUARD_UNIT = f"""[Unit]
Description=agentic-trader: block cloud metadata for non-root users
Before=agentic-trader.service
After=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -C OUTPUT -d {METADATA_IP} -m owner ! --uid-owner 0 -j REJECT 2>/dev/null || iptables -I OUTPUT -d {METADATA_IP} -m owner ! --uid-owner 0 -j REJECT'

[Install]
WantedBy=multi-user.target
"""


def _agent_home():
    return os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))


def _b64_file(path, content, permissions, owner=None):
    f = {"path": path, "encoding": "b64",
         "content": base64.b64encode(content).decode(),
         "permissions": permissions,
         # after the users module, so /home/trader is created by useradd
         # (owned by trader), not by write_files (owned by root)
         "defer": True}
    if owner:
        f["owner"] = owner
    return f


def _gz_file(path, content, permissions):
    f = _b64_file(path, gzip.compress(content, mtime=0), permissions)
    f["encoding"] = "gz+b64"   # cloud-init: base64, then gunzip
    return f


def _ledger_lines():
    """The trade log's rows, oldest first, byte-for-byte."""
    try:
        with open(os.path.join(_agent_home(), _LEDGER_FILE), "rb") as f:
            return f.read().splitlines(keepends=True)
    except OSError:
        return []


def _is_todays_row(line, today):
    """Same test agent.py's daily entry cap and loss stop use (UTC day)."""
    if today.encode() not in line:
        return False
    try:
        t = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    return isinstance(t, dict) and (str(t.get("ts", "")).startswith(today)
                                    or str(t.get("closed_at", "")).startswith(today))


def deployed_before():
    """True if this computer ever asked DigitalOcean for a server: the login
    key is made on the first attempt and never removed."""
    return os.path.exists(os.path.join(_agent_home(), SSH_KEY_NAME))


def mark_deploy_checked():
    """No warning about an earlier deploy is needed (any more). Never raises."""
    try:
        os.makedirs(_agent_home(), exist_ok=True)
        with open(os.path.join(_agent_home(), DEPLOY_CHECKED_FILE), "w",
                  encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) + "\n")
    except OSError:
        pass


def earlier_deploy_warning():
    """One-time warning text for an owner who created a server with a version
    that did NOT retire this computer afterwards (before 2026-11): both
    copies may be trading the same account. None when there is nothing to
    say, or it was already said."""
    if not deployed_before() or os.path.exists(
            os.path.join(_agent_home(), DEPLOY_CHECKED_FILE)):
        return None
    mark_deploy_checked()
    return (
        "this computer was once used to create a cloud server for this agent "
        "(DigitalOcean). Older versions left BOTH copies running, and two "
        "copies on one broker account place every order twice. Please check "
        "now: open cloud.digitalocean.com and look under Droplets. If a "
        "server named agentic-trader is there, keep only one copy — either "
        "destroy that server, or stop the agent on this computer with: "
        "python agent.py stop   and then: python agent.py autostart off. "
        "If there is no such server, nothing is wrong and you can ignore "
        "this. This message is shown once.")


def ensure_ssh_key():
    """This computer's key for reaching the server: (public_key, private_path),
    or ("", "") when ssh-keygen isn't available. Made once, kept in AGENT_HOME."""
    priv = os.path.join(_agent_home(), SSH_KEY_NAME)
    pub = priv + ".pub"
    if not os.path.exists(pub):
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            return "", ""
        os.makedirs(_agent_home(), exist_ok=True)
        try:
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C",
                            "agentic-trader", "-f", priv],
                           check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return "", ""
    try:
        with open(pub, encoding="utf-8") as f:
            return f.read().strip(), priv
    except OSError:
        return "", ""


def _fingerprint(public_key):
    """DigitalOcean's key id form: MD5 of the key blob, colon-separated."""
    blob = base64.b64decode(public_key.split()[1])
    h = hashlib.md5(blob).hexdigest()  # noqa: S324 — DO's id format, not security
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def _do(method, token, path, **kw):
    return requests.request(method, f"{DO_API}{path}",
                            headers={"Authorization": f"Bearer {token}",
                                     "Content-Type": "application/json"},
                            timeout=30, **kw)


def _account_keys(token):
    """The user's SSH keys already on their DigitalOcean account. Never raises."""
    try:
        resp = _do("GET", token, "/account/keys?per_page=50")
        return resp.json().get("ssh_keys", []) if resp.status_code == 200 else []
    except Exception:
        return []


def _upload_key(token, public_key):
    """Add this computer's key to the account (once). Returns its id or None."""
    try:
        resp = _do("POST", token, "/account/keys",
                   json={"name": "agentic-trader", "public_key": public_key})
        if resp.status_code in (200, 201):
            return resp.json().get("ssh_key", {}).get("id")
        # already on the account (e.g. a second deploy): look it up
        resp = _do("GET", token, f"/account/keys/{_fingerprint(public_key)}")
        if resp.status_code == 200:
            return resp.json().get("ssh_key", {}).get("id")
    except Exception:
        pass
    return None


def pick_region_and_size(token, region):
    """(region, size, note). The chosen region at the $6 size if DigitalOcean
    offers it there; else the same size in a nearby region; else a bigger
    size in the chosen region. If the catalog can't be read, the request
    goes out as asked (create_droplet still retries on 'not available')."""
    try:
        resp = _do("GET", token, "/regions?per_page=200")
        regions = {r["slug"]: r for r in resp.json().get("regions", [])
                   if r.get("available")} if resp.status_code == 200 else {}
    except Exception:
        regions = {}
    if not regions:
        return region, SIZE, ""

    def has(slug, size):
        return slug in regions and size in (regions[slug].get("sizes") or [])
    if has(region, SIZE):
        return region, SIZE, ""
    for alt in NEARBY.get(region, []) + REGIONS:
        if has(alt, SIZE):
            return alt, SIZE, (f"{region} has no room for the $6 server right "
                               f"now, so it's in {alt} instead")
    for size in SIZE_FALLBACKS:
        if has(region, size):
            return region, size, (f"the $6 server isn't offered anywhere close "
                                  f"right now, so it's a {size} in {region} "
                                  "(costs a little more — check DigitalOcean's "
                                  "pricing page)")
    return region, SIZE, ""


def build_cloud_init(authorized_keys=(), notes=None):
    """cloud-init that installs the agent, restores this machine's completed
    config and memory (positions, handled signals, day-pass, trade log), and
    starts the systemd service.

    If the trade log is too long for DigitalOcean's user-data limit, the
    OLDEST rows stay behind (they only feed the P&L history pages) and a
    plain-language line saying so is appended to `notes`. Today's rows always
    travel; if even those can't fit, raises RuntimeError — nothing is sent.

    Commands run as root via runuser, never sudo: with no SSH key DigitalOcean
    sets an expired root password, and sudo then refuses to run at all."""
    write_files = []
    for name in _HOME_FILES:
        path = os.path.join(_agent_home(), name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            write_files.append(_b64_file(f"/home/trader/.agentic-trader/{name}",
                                         f.read(), "0600"))
    state_path = os.path.join(_agent_home(), _STATE_FILE)
    if os.path.exists(state_path):
        with open(state_path, "rb") as f:
            write_files.append(_gz_file(
                f"/home/trader/.agentic-trader/{_STATE_FILE}", f.read(), "0600"))
    home_files = len(write_files)   # the trade log slots in right after these
    with open(_SERVICE_PATH, "rb") as f:
        write_files.append(_b64_file("/etc/systemd/system/agentic-trader.service",
                                     f.read(), "0644"))
    write_files.append(_b64_file(
        "/etc/systemd/system/agentic-trader-metadata-guard.service",
        _GUARD_UNIT.encode(), "0644"))
    user = {"name": "trader", "shell": "/bin/bash", "groups": "sudo",
            "sudo": ["ALL=(ALL) NOPASSWD:ALL"], "lock_passwd": True}
    keys = [k for k in authorized_keys if k]
    if keys:
        user["ssh_authorized_keys"] = keys
    # Download to a file, THEN run it. `curl … | bash` reports bash's status,
    # so a failed download looked like a successful install (nothing to run =
    # exit 0) and the retry loop below never retried it.
    install = ("curl -fsSL https://raw.githubusercontent.com/traderhc123/"
               "agentic-trader/main/install.sh "
               "-o /home/trader/agentic-trader-install.sh && "
               "AGENTIC_TRADER_NO_LAUNCH=1 "
               "AGENTIC_TRADER_DIR=/home/trader/agentic-trader "
               "bash /home/trader/agentic-trader-install.sh")
    doc = {
        "users": [user],
        # don't force a root password change at first login (it also breaks sudo)
        "chpasswd": {"expire": False},
        "package_update": True,
        "packages": ["git", "python3", "python3-venv", "python3-pip", "tmux",
                     "iptables"],
        "write_files": write_files,
        "runcmd": [
            # write_files may have created /home/trader before the user existed
            "chown trader:trader /home/trader",
            "chown -R trader:trader /home/trader/.agentic-trader",
            "chmod 700 /home/trader/.agentic-trader",
            # retried: a network blip on first boot shouldn't strand the server
            # (and a failed download must count as a failure — see `install`)
            "for i in 1 2 3; do runuser -u trader -- env HOME=/home/trader "
            f"USER=trader bash -c '{install}' && break; sleep 20; done",
            "systemctl daemon-reload",
            "systemctl enable --now agentic-trader-metadata-guard",
            "systemctl enable --now agentic-trader",
            # Secrets travelled in user-data; the config is now on disk (0600)
            # so the local user-data copies aren't needed again. Best-effort.
            "shred -u /var/lib/cloud/instance/user-data.txt* 2>/dev/null || "
            "rm -f /var/lib/cloud/instance/user-data.txt* || true",
            "find /var/lib/cloud/instances -name 'user-data.txt*' -delete "
            "2>/dev/null || true",
        ],
    }
    lines = _ledger_lines()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    first_today = next((i for i, row in enumerate(lines)
                        if _is_todays_row(row, today)), len(lines))

    def render(older):
        """The document with today's rows plus the newest `older` rows."""
        rows = lines[first_today - older:]
        doc["write_files"] = list(write_files)
        if rows:
            doc["write_files"].insert(home_files, _gz_file(
                f"/home/trader/.agentic-trader/{_LEDGER_FILE}",
                b"".join(rows), "0600"))
        raw = "#cloud-config\n" + json.dumps(doc, indent=1)
        return raw if len(raw.encode("utf-8")) <= USER_DATA_LIMIT else None

    raw = render(first_today)
    if raw is not None:
        return raw
    best, lo, hi = render(0), 0, first_today
    if best is None:
        raise RuntimeError(
            "this setup's saved state and today's activity log are too large "
            "to send to a new server, so no server was created and nothing "
            "changed on this computer. Try again tomorrow before the market "
            "opens, when today's log is empty.")
    while hi - lo > 1:   # most older rows that still fit
        mid = (lo + hi) // 2
        got = render(mid)
        if got is None:
            hi = mid
        else:
            best, lo = got, mid
    if notes is not None:
        notes.append(
            "your trade history was too long to send in full, so the server "
            f"starts with the most recent {len(lines) - first_today + lo} "
            "entries (all of today's are there). The full history stays on "
            f"this computer in {os.path.join(_agent_home(), _LEDGER_FILE)}")
    return best


def _not_available(resp):
    return resp.status_code == 422 and "not available" in resp.text.lower()


class OutcomeUnknown(RuntimeError):
    """DigitalOcean did not answer and the account could not be read either:
    a server may or may not exist. Nothing was changed on this computer."""


def _recent_server(token, name):
    """(known, droplet): a server by this name that DigitalOcean is creating
    right now or made in the last few minutes. known=False when the account
    could not be read (so "none found" would be a guess). Never raises."""
    try:
        resp = _do("GET", token, "/droplets?tag_name=agentic-trader&per_page=200")
        if resp.status_code != 200:
            return False, None
        droplets = resp.json().get("droplets") or []
    except Exception:
        return False, None
    now, found = time.time(), None
    for d in droplets:
        if not isinstance(d, dict) or d.get("name") != name:
            continue
        recent = d.get("status") == "new"
        try:
            made = calendar.timegm(time.strptime(
                str(d.get("created_at", ""))[:19], "%Y-%m-%dT%H:%M:%S"))
            recent = recent or abs(now - made) < 600
        except ValueError:
            pass
        if recent and (found is None or str(d.get("created_at", ""))
                       > str(found.get("created_at", ""))):
            found = d
    return True, found


def create_droplet(token, region="nyc3", name="agentic-trader"):
    """Create the droplet. Returns (droplet_id, message). Raises RuntimeError.

    Also returns how to reach it: the message names the SSH key this computer
    uses (made once, also added to the trader account for the dashboard)."""
    if region not in REGIONS:
        region = "nyc3"
    region, size, note = pick_region_and_size(token, region)
    public_key, _priv = ensure_ssh_key()
    key_ids, trader_keys = [], [public_key]
    for k in _account_keys(token):
        key_ids.append(k.get("id"))
        if k.get("public_key") and k["public_key"] not in trader_keys:
            trader_keys.append(k["public_key"])
    if public_key:
        kid = _upload_key(token, public_key)
        if kid and kid not in key_ids:
            key_ids.append(kid)
    handoff_notes = []
    body = {"name": name, "region": region, "size": size, "image": IMAGE,
            "user_data": build_cloud_init(trader_keys, notes=handoff_notes),
            "ssh_keys": [k for k in key_ids if k], "tags": ["agentic-trader"],
            "monitoring": True}
    tried = [region]

    def ask():
        # No answer (timeout, dropped connection) is NOT "not created": the
        # request may have arrived. Handled below — never as a plain failure.
        try:
            return _do("POST", token, "/droplets", json=body)
        except requests.RequestException as exc:
            return exc
    resp = ask()
    while not isinstance(resp, Exception) and _not_available(resp):
        # catalog was stale: walk the nearby regions
        alt = next((r for r in NEARBY.get(tried[0], []) + REGIONS
                    if r not in tried), None)
        if alt is None:
            break
        tried.append(alt)
        body["region"] = alt
        note = f"{tried[0]} had no room for this server, so it's in {alt} instead"
        resp = ask()
    if isinstance(resp, Exception):
        known, droplet = _recent_server(token, name)
        if droplet is None and known:
            raise RuntimeError(
                "DigitalOcean did not answer. Your account was checked and "
                "no new server is there, so none was created. Nothing was "
                "changed on this computer. Try again in a minute. (If a "
                "server named agentic-trader shows up under Droplets at "
                "cloud.digitalocean.com later anyway, destroy it.)")
        if droplet is None:
            raise OutcomeUnknown(
                "DigitalOcean did not answer, so it is not known whether the "
                "server was created. Nothing was changed on this computer. "
                "Before you try again, open cloud.digitalocean.com and look "
                "under Droplets: if a server named agentic-trader is there, "
                "destroy it. Left running, it would place every order a "
                "second time.")
        if (droplet.get("region") or {}).get("slug"):
            body["region"] = droplet["region"]["slug"]
    else:
        if resp.status_code == 401:
            raise RuntimeError("DigitalOcean rejected the token (401) — create a "
                               "personal access token with write scope at "
                               "cloud.digitalocean.com/account/api/tokens")
        if resp.status_code not in (201, 202):
            raise RuntimeError(f"droplet create failed HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        # From here the server EXISTS: nothing below may raise.
        try:
            droplet = resp.json().get("droplet") or {}
        except (ValueError, AttributeError):
            droplet = {}
        if not droplet.get("id"):
            droplet = _recent_server(token, name)[1] or {}
    if droplet.get("id"):
        msg = f"droplet {droplet.get('id')} creating in {body['region']}"
    else:
        msg = (f"the server was created in {body['region']}, but "
               "DigitalOcean's reply could not be read — find its address "
               "under Droplets at cloud.digitalocean.com")
    if note:
        msg += f" ({note})"
    for extra in handoff_notes:
        msg += f" — {extra}"
    if not public_key:
        msg += (" — no ssh-keygen on this computer, so no login key was made: "
                "add your own SSH key in DigitalOcean to reach the dashboard")
    return droplet.get("id"), msg


def ssh_command(ip):
    """The tunnel command for the dashboard, with this computer's key."""
    priv = os.path.join(_agent_home(), SSH_KEY_NAME)
    key = f'-i "{priv}" ' if os.path.exists(priv) else ""
    return f"ssh {key}-L 8721:127.0.0.1:8721 trader@{ip}"


def droplet_status(token, droplet_id):
    """Returns {"status": ..., "ip": ...}. Never raises."""
    try:
        resp = requests.get(f"{DO_API}/droplets/{droplet_id}",
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=15)
        d = resp.json().get("droplet", {})
        ip = ""
        for net in (d.get("networks", {}) or {}).get("v4", []):
            if net.get("type") == "public":
                ip = net.get("ip_address", "")
        return {"status": d.get("status", "unknown"), "ip": ip}
    except Exception as exc:
        return {"status": f"error: {str(exc)[:100]}", "ip": ""}


def wait_active(token, droplet_id, timeout_s=300):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = droplet_status(token, droplet_id)
        if st["status"] == "active" and st["ip"]:
            return st
        time.sleep(5)
    return droplet_status(token, droplet_id)
