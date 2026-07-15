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
metadata endpoint (the agent needs neither once booted). The copy held in the
user's DigitalOcean control panel remains until the droplet is rebuilt, and is
in the same trust domain as the server; if the droplet is ever destroyed,
rotate the Robinhood session and any API keys it carried.
"""

import base64
import json
import os
import time

import requests

DO_API = "https://api.digitalocean.com/v2"
REGIONS = ["nyc3", "sfo3", "tor1", "lon1", "fra1", "sgp1"]
SIZE = "s-1vcpu-1gb"
IMAGE = "ubuntu-24-04-x64"

_HOME_FILES = ["config.json", "acceptance.json", "robinhood_oauth.json",
               "policy.md"]


def _agent_home():
    return os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))


def build_cloud_init():
    """cloud-init that installs the agent, restores this machine's completed
    config, and starts the systemd service."""
    write_files = []
    for name in _HOME_FILES:
        path = os.path.join(_agent_home(), name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        write_files.append({
            "path": f"/home/trader/.agentic-trader/{name}",
            "encoding": "b64",
            "content": content,
            "permissions": "0600",
        })
    doc = {
        "users": [{"name": "trader", "shell": "/bin/bash", "groups": "sudo",
                   "sudo": ["ALL=(ALL) NOPASSWD:ALL"]}],
        "package_update": True,
        "packages": ["git", "python3", "python3-venv", "python3-pip", "tmux"],
        "write_files": write_files,
        "runcmd": [
            "chown -R trader:trader /home/trader/.agentic-trader",
            "chmod 700 /home/trader/.agentic-trader",
            "sudo -u trader bash -c 'curl -fsSL https://raw.githubusercontent.com/"
            "traderhc123/agentic-trader/main/install.sh | "
            "AGENTIC_TRADER_DIR=/home/trader/agentic-trader bash'",
            "cp /home/trader/agentic-trader/deploy/agentic-trader.service "
            "/etc/systemd/system/",
            "systemctl daemon-reload",
            "systemctl enable --now agentic-trader",
            # Secrets travelled in user-data; the config is now on disk (0600)
            # so neither the local user-data copy nor the metadata service is
            # needed again. Shred the copies and firewall the metadata endpoint
            # so a later low-priv foothold cannot re-read them. Best-effort.
            "shred -u /var/lib/cloud/instance/user-data.txt* 2>/dev/null || "
            "rm -f /var/lib/cloud/instance/user-data.txt* || true",
            "find /var/lib/cloud/instances -name 'user-data.txt*' -delete "
            "2>/dev/null || true",
            "iptables -A OUTPUT -d 169.254.169.254 -j REJECT || true",
        ],
    }
    return "#cloud-config\n" + json.dumps(doc, indent=1)


def create_droplet(token, region="nyc3", name="agentic-trader"):
    """Create the droplet. Returns (droplet_id, message). Raises RuntimeError."""
    if region not in REGIONS:
        region = "nyc3"
    resp = requests.post(
        f"{DO_API}/droplets",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"name": name, "region": region, "size": SIZE, "image": IMAGE,
              "user_data": build_cloud_init(), "tags": ["agentic-trader"],
              "monitoring": True},
        timeout=30)
    if resp.status_code == 401:
        raise RuntimeError("DigitalOcean rejected the token (401) — create a "
                           "personal access token with write scope at "
                           "cloud.digitalocean.com/account/api/tokens")
    if resp.status_code not in (201, 202):
        raise RuntimeError(f"droplet create failed HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
    droplet = resp.json().get("droplet", {})
    return droplet.get("id"), f"droplet {droplet.get('id')} creating in {region}"


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
