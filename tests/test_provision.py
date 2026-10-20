"""Hermetic checks for the DigitalOcean self-provision path (provision.py).

These verify the cloud-init DOCUMENT — a real droplet run has NOT been
performed in CI (needs a paid DO token); see GETTING_STARTED for the manual
verification tier.
"""

import base64
import json

import provision


def _doc(monkeypatch, tmp_path, files=None):
    home = tmp_path / "home"
    home.mkdir()
    for name, content in (files or {}).items():
        (home / name).write_text(content)
    monkeypatch.setenv("AGENT_HOME", str(home))
    raw = provision.build_cloud_init()
    assert raw.startswith("#cloud-config\n")
    return json.loads(raw.split("\n", 1)[1])


def test_cloud_init_restores_config_0600(monkeypatch, tmp_path):
    doc = _doc(monkeypatch, tmp_path,
               {"config.json": '{"broker": "alpaca"}',
                "acceptance.json": '{"accepted": true}'})
    files = {f["path"]: f for f in doc["write_files"]}
    cfg = files["/home/trader/.agentic-trader/config.json"]
    assert cfg["permissions"] == "0600"
    assert json.loads(base64.b64decode(cfg["content"]))["broker"] == "alpaca"


def test_cloud_init_skips_missing_files(monkeypatch, tmp_path):
    doc = _doc(monkeypatch, tmp_path, {"config.json": "{}"})
    paths = [f["path"] for f in doc["write_files"] if f["path"].startswith("/home/")]
    assert paths == ["/home/trader/.agentic-trader/config.json"]


def _file(doc, path):
    f = next(f for f in doc["write_files"] if f["path"] == path)
    return base64.b64decode(f["content"]).decode()


def test_cloud_init_shreds_userdata_and_blocks_metadata(monkeypatch, tmp_path):
    doc = _doc(monkeypatch, tmp_path)
    runcmd = "\n".join(doc["runcmd"])
    assert "shred -u /var/lib/cloud/instance/user-data.txt" in runcmd
    assert "chmod 700 /home/trader/.agentic-trader" in runcmd
    guard = _file(doc, "/etc/systemd/system/agentic-trader-metadata-guard.service")
    # blocked for everyone but root, so DigitalOcean's Web Console still works
    assert "169.254.169.254" in guard and "REJECT" in guard
    assert "! --uid-owner 0" in guard
    assert "systemctl enable --now agentic-trader-metadata-guard" in runcmd


def test_cloud_init_never_uses_sudo(monkeypatch, tmp_path):
    """DigitalOcean's emailed root password starts expired; sudo then refuses
    to run at all, so nothing in first boot may go through it."""
    doc = _doc(monkeypatch, tmp_path)
    runcmd = "\n".join(doc["runcmd"])
    assert "sudo " not in runcmd and "runuser -u trader" in runcmd
    assert doc["chpasswd"] == {"expire": False}


def test_home_owned_by_trader(monkeypatch, tmp_path):
    doc = _doc(monkeypatch, tmp_path, {"config.json": "{}"})
    assert all(f.get("defer") for f in doc["write_files"])  # after useradd
    assert doc["runcmd"][0] == "chown trader:trader /home/trader"


def test_service_file_ships_in_user_data(monkeypatch, tmp_path):
    """The unit must not depend on the repo clone succeeding."""
    doc = _doc(monkeypatch, tmp_path)
    unit = _file(doc, "/etc/systemd/system/agentic-trader.service")
    assert "ExecStart=/home/trader/agentic-trader/.venv/bin/python agent.py run" in unit
    assert not any(c.startswith("cp ") for c in doc["runcmd"])


def test_login_keys_go_to_trader(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    doc = json.loads(provision.build_cloud_init(["ssh-ed25519 AAAA k1", ""])
                     .split("\n", 1)[1])
    user = doc["users"][0]
    assert user["name"] == "trader" and user["ssh_authorized_keys"] == ["ssh-ed25519 AAAA k1"]
    assert user["lock_passwd"] is True


def test_cloud_init_starts_service_after_install(monkeypatch, tmp_path):
    runcmd = _doc(monkeypatch, tmp_path)["runcmd"]
    install_idx = next(i for i, c in enumerate(runcmd) if "install.sh" in c)
    enable_idx = next(i for i, c in enumerate(runcmd)
                      if "systemctl enable --now" in c)
    assert install_idx < enable_idx


def test_create_droplet_rejects_bad_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self):
            return {"message": "unauthorized"}

    monkeypatch.setattr(provision.requests, "request", lambda *a, **k: _Resp())
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    try:
        provision.create_droplet("bad-token")
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "unauthorized" in str(exc).lower() or "401" in str(exc)


class _R:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._b, self.text = code, body or {}, text or json.dumps(body or {})

    def json(self):
        return self._b


def _fake_do(monkeypatch, regions, create=None):
    calls = []

    def req(method, url, **kw):
        calls.append((method, url, kw.get("json")))
        if url.endswith("/regions?per_page=200"):
            return _R(200, {"regions": regions})
        if "/account/keys" in url and method == "GET":
            return _R(200, {"ssh_keys": [{"id": 7, "public_key": "ssh-rsa AAAAB acct"}]})
        if url.endswith("/account/keys") and method == "POST":
            return _R(201, {"ssh_key": {"id": 9}})
        if url.endswith("/droplets"):
            return (create or (lambda body: _R(202, {"droplet": {"id": 1}})))(kw["json"])
        raise AssertionError(url)
    monkeypatch.setattr(provision.requests, "request", req)
    return calls


def _no_keygen(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)


def test_picks_nearby_region_when_size_missing(monkeypatch, tmp_path):
    _no_keygen(monkeypatch, tmp_path)
    calls = _fake_do(monkeypatch, [
        {"slug": "nyc3", "available": True, "sizes": ["s-2vcpu-4gb"]},
        {"slug": "tor1", "available": True, "sizes": ["s-1vcpu-1gb"]}])
    _id, msg = provision.create_droplet("t", "nyc3")
    body = next(c[2] for c in calls if c[1].endswith("/droplets"))
    assert body["region"] == "tor1" and body["size"] == "s-1vcpu-1gb"
    assert "tor1" in msg and "nyc3" in msg


def test_retries_next_region_on_not_available(monkeypatch, tmp_path):
    _no_keygen(monkeypatch, tmp_path)
    seen = []

    def create(body):
        seen.append(body["region"])
        if len(seen) == 1:
            return _R(422, text='{"message":"Size is not available in this region."}')
        return _R(202, {"droplet": {"id": 2}})
    _fake_do(monkeypatch, [], create)  # catalog unreadable → request as asked
    _id, msg = provision.create_droplet("t", "nyc3")
    assert seen == ["nyc3", "tor1"] and "tor1" in msg


def test_attaches_account_keys_and_this_computers_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    (tmp_path / "do_ssh_key").write_text("PRIVATE")
    (tmp_path / "do_ssh_key.pub").write_text("ssh-ed25519 AAAAC3Nz agentic-trader\n")
    calls = _fake_do(monkeypatch, [{"slug": "nyc3", "available": True,
                                    "sizes": ["s-1vcpu-1gb"]}])
    provision.create_droplet("t", "nyc3")
    body = next(c[2] for c in calls if c[1].endswith("/droplets"))
    assert body["ssh_keys"] == [7, 9]
    doc = json.loads(body["user_data"].split("\n", 1)[1])
    assert doc["users"][0]["ssh_authorized_keys"] == [
        "ssh-ed25519 AAAAC3Nz agentic-trader", "ssh-rsa AAAAB acct"]
    assert provision.ssh_command("1.2.3.4") == (
        f'ssh -i "{tmp_path / "do_ssh_key"}" -L 8721:127.0.0.1:8721 trader@1.2.3.4')


def test_fingerprint_matches_do_format():
    # DigitalOcean shows MD5 of the key blob as aa:bb:...
    fp = provision._fingerprint("ssh-ed25519 " + base64.b64encode(b"abc").decode())
    assert fp == "90:01:50:98:3c:d2:4f:b0:d6:96:3f:7d:28:e1:7f:72"


def test_ensure_ssh_key_makes_one_private_key(monkeypatch, tmp_path):
    import shutil
    import pytest
    if not shutil.which("ssh-keygen"):
        pytest.skip("no ssh-keygen here")
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    pub, priv = provision.ensure_ssh_key()
    assert pub.startswith("ssh-ed25519 ") and priv == str(tmp_path / "do_ssh_key")
    assert provision.ensure_ssh_key() == (pub, priv)  # made once, reused
