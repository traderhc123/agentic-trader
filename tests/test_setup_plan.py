"""Assistant-written setup plans: all-or-nothing, never past the owner's gates."""

import json

import pytest

import agent
import setup_plan


def _apply(plan, cfg=None, consent=True, home="/tmp/unused"):
    return setup_plan.apply(plan, cfg or {}, consent, home)


def test_refuses_before_the_owner_accepts():
    new, rep = _apply({"source": {"type": "manual"}}, consent=False)
    assert new is None and "accept the agreement" in rep["errors"][0]
    assert "never accept it for them" in rep["errors"][0]


@pytest.mark.parametrize("plan,needle", [
    ({"safety": {"dry_run": False}}, "dry off"),
    ({"dry_run": False}, "dry off"),
    ({"self_edit_enabled": True}, "self-edit"),
    ({"source": {"type": "manual", "accepted": True}}, "accepts DISCLAIMER"),
])
def test_owner_only_settings_are_rejected(plan, needle):
    new, rep = _apply(plan)
    assert new is None and any(needle in e for e in rep["errors"])


def test_unknown_sections_rejected():
    new, rep = _apply({"source": {"type": "manual"}, "wallet": {}})
    assert new is None and "unknown plan sections: wallet" in rep["errors"][0]


def test_all_or_nothing(monkeypatch, tmp_path):
    """One bad section means nothing is applied — not even the good ones."""
    plan = {"source": {"type": "manual"},
            "notifications": {"discord_webhook_url": "http://not-https"}}
    new, rep = _apply(plan, home=str(tmp_path))
    assert new is None and rep["applied"] == []


def test_good_plan_applies_and_stays_dry_run(tmp_path):
    plan = {"source": {"type": "agenthc", "include_other_trades": True},
            "sizing": {"mode": "budget", "budget_per_trade_usd": 250},
            "safety": {"max_entries_per_day": 3, "max_daily_loss_usd": 0},
            "notifications": {"ntfy_topic": "t"},
            "policy": "- Skip puts."}
    new, rep = _apply(plan, home=str(tmp_path))
    assert rep["ok"] and new["dry_run"] is True
    assert new["source"] == "agenthc" and new["include_other_trades"] is True
    assert new["budget_per_trade_usd"] == 250 and new["max_entries_per_day"] == 3
    assert (tmp_path / "policy.md").read_text() == "- Skip puts.\n"


def test_existing_live_choice_is_kept(tmp_path):
    new, rep = _apply({"source": {"type": "manual"}}, cfg={"dry_run": False},
                      home=str(tmp_path))
    assert rep["ok"] and new["dry_run"] is False  # the owner already chose live


def test_feed_payment_cannot_come_from_a_plan():
    new, rep = _apply({"feed_access": {"feed_pass": {"token": "x"}}})
    assert new is None and "paid by the owner" in rep["errors"][0]


def test_broker_keys_verified_before_saving(monkeypatch, tmp_path):
    from brokers import BROKERS
    monkeypatch.setattr(BROKERS["alpaca"], "verify", lambda c: (False, "keys rejected"))
    new, rep = _apply({"broker": {"type": "alpaca", "alpaca_key_id": "k",
                                  "alpaca_secret": "s"}}, home=str(tmp_path))
    assert new is None and "keys rejected" in rep["errors"][0]


def test_ai_model_must_answer_a_test_call(monkeypatch, tmp_path):
    import llm_client

    def boom(*a, **k):
        raise llm_client.LLMError("auth", "bad key")
    monkeypatch.setattr(llm_client, "complete", boom)
    new, rep = _apply({"ai": {"provider": "xai", "api_key": "xai-x", "model": "grok-4.6"}},
                      home=str(tmp_path))
    assert new is None and "test call" in rep["errors"][0]
    monkeypatch.setattr(llm_client, "complete", lambda *a, **k: "OK")
    new, rep = _apply({"ai": {"api_key": "xai-x", "model": "grok-4.6"}},
                      home=str(tmp_path))
    assert rep["ok"] and new["llm_provider"] == "xai"


def test_cli_setup_from_lists_owner_steps(home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"source": {"type": "agenthc"},
                                "broker": {"type": "robinhood"},
                                "notifications": {"ntfy_topic": "t"}}))
    import requests

    class _Up:
        status_code = 200
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Up())  # no live API calls
    with pytest.raises(SystemExit) as exc:
        agent.cmd_setup_from(str(plan), as_json=True)
    assert exc.value.code == 0
    rep = json.loads(capsys.readouterr().out)
    owner = " ".join(rep["owner_steps"])
    assert "Connect Robinhood" in owner and "buys signal days" in owner
    assert agent._load(agent.CONFIG_PATH)["dry_run"] is True


def test_example_plan_is_itself_valid_shape():
    assert set(setup_plan.EXAMPLE_PLAN) <= setup_plan.ALLOWED_TOP
    assert not setup_plan._find_forbidden(setup_plan.EXAMPLE_PLAN)
