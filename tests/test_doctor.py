"""agent.py doctor — every failure must come with an actionable fix line."""

import json

import pytest

import agent


def test_doctor_unconfigured_exits_1_with_fixes(home, capsys):
    with pytest.raises(SystemExit) as exc:
        agent.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Consent accepted" in out
    assert "fix: python agent.py setup" in out
    assert "problem(s)" in out


def test_doctor_all_green_exits_0(home, monkeypatch, capsys):
    (home / "acceptance.json").write_text("{}")
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    cfg = {"source": "manual", "broker": "alpaca",
           "alpaca_key_id": "k", "alpaca_secret": "s",
           "discord_webhook_url": "https://discord/x", "dry_run": True}
    (home / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(agent.BROKERS["alpaca"], "verify",
                        lambda c: (True, "connected ✓ (PAPER)"))
    agent.cmd_doctor()  # must not SystemExit
    out = capsys.readouterr().out
    assert "All checks passed" in out
    assert "DRY-RUN" in out


def test_doctor_broker_failure_names_the_fix(home, monkeypatch, capsys):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    cfg = {"source": "manual", "broker": "moomoo", "moomoo_paper": True}
    (home / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(agent.BROKERS["moomoo"], "verify",
                        lambda c: (False, "could not reach OpenD"))
    with pytest.raises(SystemExit):
        agent.cmd_doctor()
    out = capsys.readouterr().out
    assert "could not reach OpenD" in out
    assert "broker step" in out
