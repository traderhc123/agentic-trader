"""Automated VPS provisioning — the agent moves itself to a cloud server.

After local setup completes, the agent can create a small DigitalOcean
droplet (with the USER'S own API token, on the user's account), ship its
finished configuration there via cloud-init, and auto-start under systemd.
The token is used for the creation calls and NOT stored.

What gets copied to the droplet (the user's own files, to the user's own
server): config.json, acceptance.json, robinhood_oauth.json, policy.md.

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


def build_cloud_init(authorized_keys=()):
    """cloud-init that installs the agent, restores this machine's completed
    config, and starts the systemd service.

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
    install = ("curl -fsSL https://raw.githubusercontent.com/traderhc123/"
               "agentic-trader/main/install.sh | AGENTIC_TRADER_NO_LAUNCH=1 "
               "AGENTIC_TRADER_DIR=/home/trader/agentic-trader bash")
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
    return "#cloud-config\n" + json.dumps(doc, indent=1)


def _not_available(resp):
    return resp.status_code == 422 and "not available" in resp.text.lower()


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
    body = {"name": name, "region": region, "size": size, "image": IMAGE,
            "user_data": build_cloud_init(trader_keys),
            "ssh_keys": [k for k in key_ids if k], "tags": ["agentic-trader"],
            "monitoring": True}
    tried = [region]
    resp = _do("POST", token, "/droplets", json=body)
    while _not_available(resp):  # catalog was stale: walk the nearby regions
        alt = next((r for r in NEARBY.get(tried[0], []) + REGIONS
                    if r not in tried), None)
        if alt is None:
            break
        tried.append(alt)
        body["region"] = alt
        note = f"{tried[0]} had no room for this server, so it's in {alt} instead"
        resp = _do("POST", token, "/droplets", json=body)
    if resp.status_code == 401:
        raise RuntimeError("DigitalOcean rejected the token (401) — create a "
                           "personal access token with write scope at "
                           "cloud.digitalocean.com/account/api/tokens")
    if resp.status_code not in (201, 202):
        raise RuntimeError(f"droplet create failed HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
    droplet = resp.json().get("droplet", {})
    msg = f"droplet {droplet.get('id')} creating in {body['region']}"
    if note:
        msg += f" ({note})"
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
