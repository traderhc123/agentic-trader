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
    paths = [f["path"] for f in doc["write_files"]]
    assert paths == ["/home/trader/.agentic-trader/config.json"]


def test_cloud_init_shreds_userdata_and_blocks_metadata(monkeypatch, tmp_path):
    doc = _doc(monkeypatch, tmp_path)
    runcmd = "\n".join(doc["runcmd"])
    assert "shred -u /var/lib/cloud/instance/user-data.txt" in runcmd
    assert "169.254.169.254" in runcmd and "REJECT" in runcmd
    assert "chmod 700 /home/trader/.agentic-trader" in runcmd


def test_cloud_init_starts_service_after_install(monkeypatch, tmp_path):
    runcmd = _doc(monkeypatch, tmp_path)["runcmd"]
    install_idx = next(i for i, c in enumerate(runcmd) if "install.sh" in c)
    enable_idx = next(i for i, c in enumerate(runcmd)
                      if "systemctl enable --now" in c)
    assert install_idx < enable_idx


def test_create_droplet_rejects_bad_token(monkeypatch):
    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self):
            return {"message": "unauthorized"}

    monkeypatch.setattr(provision.requests, "post", lambda *a, **k: _Resp())
    try:
        provision.create_droplet("bad-token")
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "unauthorized" in str(exc).lower() or "401" in str(exc)
