"""Move a setup to another computer: one passphrase-locked file, the old
computer retired, the new one starting in dry-run."""

import io
import json
import os
import zipfile

import pytest

import agent
import keep_running
import transfer

PW = "correct horse battery"


def _seed(h, live=True):
    (h / "config.json").write_text(json.dumps(
        {"source": "agenthc", "broker": "robinhood", "dry_run": not live,
         "rule_tickers": ["SPY"], "feed_pass": {"token": "L402 abc:paid"}}))
    (h / "acceptance.json").write_text('{"accepted": true}')
    (h / "state.json").write_text('{"positions": {"SPY|x": {"qty": 1}}}')
    (h / "robinhood_oauth.json").write_text('{"access_token": "rh"}')
    (h / "trades.jsonl").write_text('{"action": "entry"}\n')
    # machine-specific — must not travel
    (h / "ui_token").write_text("x" * 40)
    (h / "agent.pid").write_text("123")
    (h / "agent-run.log").write_text("log")
    (h / "sub").mkdir()
    (h / "sub" / "keep.json").write_text("{}")


def _move(old, new, monkeypatch, blob_pw=PW, import_pw=PW, replace=False):
    monkeypatch.setenv("AGENT_HOME", str(old))
    blob = transfer.export_bundle(blob_pw)
    monkeypatch.setenv("AGENT_HOME", str(new))
    return blob, transfer.import_bundle(blob, import_pw, replace=replace)


def test_round_trip_moves_everything_but_machine_files(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    _seed(old)
    blob, info = _move(old, new, monkeypatch)
    assert info["was_live"] and info["source"] == "agenthc"
    assert json.loads((new / "state.json").read_text())["positions"]
    assert (new / "robinhood_oauth.json").exists() and (new / "sub" / "keep.json").exists()
    assert not (new / "ui_token").exists() and not (new / "agent.pid").exists()
    assert not (new / "agent-run.log").exists()
    cfg = json.loads((new / "config.json").read_text())
    assert cfg["dry_run"] is True                 # safety: starts in dry-run
    assert cfg["feed_pass"]["token"] == "L402 abc:paid" and cfg["rule_tickers"] == ["SPY"]
    assert b"robinhood" not in blob and b"L402" not in blob   # encrypted


def test_wrong_passphrase_writes_nothing(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    _seed(old)
    with pytest.raises(transfer.TransferError, match="wrong passphrase"):
        _move(old, new, monkeypatch, import_pw="not the one")
    assert not new.exists() or not any(new.iterdir())


def test_tampered_file_is_rejected(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    _seed(old)
    monkeypatch.setenv("AGENT_HOME", str(old))
    blob = bytearray(transfer.export_bundle(PW))
    blob[len(transfer.MAGIC) + 40] ^= 1
    with pytest.raises(transfer.TransferError):
        transfer.decrypt(bytes(blob), PW)
    with pytest.raises(transfer.TransferError, match="isn't an agentic-trader"):
        transfer.decrypt(b"hello", PW)


def test_short_passphrase_and_empty_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    with pytest.raises(transfer.TransferError, match="at least"):
        transfer.export_bundle("short")
    with pytest.raises(transfer.TransferError, match="no setup"):
        transfer.export_bundle(PW)


def test_existing_setup_needs_replace(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _seed(old)
    (new / "config.json").write_text('{"source": "manual"}')
    with pytest.raises(transfer.TransferError, match="NEEDS_REPLACE"):
        _move(old, new, monkeypatch)
    assert json.loads((new / "config.json").read_text())["source"] == "manual"
    _move(old, new, monkeypatch, replace=True)
    assert json.loads((new / "config.json").read_text())["source"] == "agenthc"


@pytest.mark.parametrize("evil", ["home/../escape.txt", "home//etc/x", "home/C:/x"])
def test_unsafe_paths_rejected(tmp_path, monkeypatch, evil):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "new"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", '{"format": 1}')
        z.writestr("home/config.json", '{"source": "manual"}')
        z.writestr(evil, "x")
    with pytest.raises(transfer.TransferError, match="unsafe"):
        transfer.import_bundle(transfer.encrypt(buf.getvalue(), PW), PW)
    assert not (tmp_path / "escape.txt").exists()


def test_moved_away_computer_refuses_to_run(home, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", False)
    monkeypatch.setattr(keep_running, "IS_MAC", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    keep_running.enable_autostart()
    transfer.mark_moved_away()
    assert not keep_running.autostart_enabled()
    with pytest.raises(SystemExit) as e:
        agent.cmd_run()
    assert e.value.code == 0 and "moved to another computer" in capsys.readouterr().out
    assert transfer.resume_here() and transfer.moved_away() is None


def test_import_clears_a_stale_moved_marker(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _seed(old)
    (new / "MOVED_AWAY").write_text("{}")
    _move(old, new, monkeypatch)
    assert not (new / "MOVED_AWAY").exists()


def test_download_route_returns_a_file():
    import webui
    r = webui.RawResponse(b"abc", headers={"Content-Disposition": "attachment"})
    assert r.body == b"abc" and r.ctype == "application/octet-stream"
