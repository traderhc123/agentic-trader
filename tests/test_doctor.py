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


def test_doctor_json_separates_owner_and_agent_steps(home, monkeypatch, capsys):
    monkeypatch.setattr(agent, "consent_ok", lambda: False)
    (home / "config.json").write_text(json.dumps({"source": "manual"}))
    with pytest.raises(SystemExit) as exc:
        agent.cmd_doctor(as_json=True)
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is False and out["mode"] == "DRY-RUN"
    assert any("OWNER reads and accepts" in s for s in out["owner_steps"])
    assert out["agent_steps"]  # broker + notifications are the assistant's job
    ids = {c["id"]: c for c in out["checks"]}
    assert ids["consent"]["who"] == "owner" and ids["broker"]["who"] == "agent"


def test_doctor_counts_an_active_prepaid_pass(home, monkeypatch, capsys):
    """REGRESSION (#27): a paid prepaid pass used to report 'no wallet and no
    Premium key — the feed cannot be paid for'."""
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    from sources import agenthc_day_trade_ideas as hc
    monkeypatch.setattr(hc, "pass_status",
                        lambda tok: {"status": "active", "trading_days_left": 12})
    cfg = {"source": "agenthc", "feed_pass": {"token": "L402 X:paid"}}
    checks = []
    agent._feed_access_check(cfg, {}, lambda *a, **k: checks.append((a, k)))
    (cid, ok, label, detail), _ = checks[0]
    assert ok and "12 signal day" in detail


def test_doctor_unpaid_pass_is_an_owner_step(home, monkeypatch):
    from sources import agenthc_day_trade_ideas as hc
    monkeypatch.setattr(hc, "pass_status", lambda tok: {"status": "pending"})
    got = []
    agent._feed_access_check({"feed_pass_pending": {"token": "T"}}, {},
                             lambda *a, **k: got.append((a, k)))
    (cid, ok, label, detail, fix), kw = got[0]
    assert not ok and "not paid" in detail and kw["who"] == "owner"
