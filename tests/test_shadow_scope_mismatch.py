"""A shadow-book size saved while the AgentHC source is "main pick only" is
never used (no other-trades signals are requested). Every screen must say so
and offer the switch, and re-saving the source must not turn other trades off."""

import os

import agent

SHADOW = {"source": "agenthc", "sizing_mode": "budget", "budget_per_trade_usd": 500,
          "other_sizing_mode": "budget", "other_budget_per_trade_usd": 100}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_unused_when_main_only():
    assert agent.shadow_sizing_unused({**SHADOW, "include_other_trades": False})
    assert not agent.shadow_sizing_unused({**SHADOW, "include_other_trades": True})
    # no shadow override, or a non-AgentHC source: nothing to warn about
    assert not agent.shadow_sizing_unused({"source": "agenthc", "sizing_mode": "budget"})
    assert not agent.shadow_sizing_unused({**SHADOW, "source": "url"})


def test_dashboard_sizing_line_says_not_used():
    desc = agent._sizing_desc({**SHADOW, "include_other_trades": False})
    assert "$100/trade budget" in desc and "NOT USED" in desc and "other trades on" in desc
    assert "NOT USED" not in agent._sizing_desc({**SHADOW, "include_other_trades": True})


def test_chat_command_switches_scope(home):
    agent._save(agent.CONFIG_PATH, {**SHADOW, "include_other_trades": False})
    handled, reply = agent._apply_command("other trades on")
    assert handled and "other trades" in reply and "NOT USED" not in reply
    assert agent._load(agent.CONFIG_PATH, {})["include_other_trades"] is True
    agent._apply_command("other trades off")
    assert agent._load(agent.CONFIG_PATH, {})["include_other_trades"] is False


def test_source_step_starts_from_saved_scope():
    src = open(os.path.join(ROOT, "webui_app/src/wizard/SourceStep.tsx")).read()
    assert 'useState(st?.include_other_trades ? "all" : "main")' in src
    web = open(os.path.join(ROOT, "webui.py")).read()
    assert '"include_other_trades": bool(cfg.get("include_other_trades"))' in web
    assert '"/api/source/scope": source_scope' in web
    # classic UI pre-selects it too
    assert "hc-scope').value=st.include_other_trades?'all':'main'" in web
