"""Automated VPS provisioning — the agent moves itself to a cloud server.

After local setup completes, the agent can create a small DigitalOcean
droplet (with the USER'S own API token, on the user's account), ship its
finished configuration there via cloud-init, and auto-start under systemd.
The token is used for the creation calls and NOT stored.

What gets copied to the droplet (the user's own files, to the user's own
server): config.json, acceptance.json, robinhood_oauth.json, policy.md.
Note: cloud-init user-data is readable from inside the droplet and by
anyone with access to the user's cloud account — same trust domain as the
server itself.
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
