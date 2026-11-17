"""Re-running the terminal setup keeps the saved broker and "other trades".

Audit report: an Alpaca or moomoo owner who re-ran `agent.py setup` (to
change a key, say) and pressed Enter at the broker question was switched to
Robinhood — the prompt's default was always 1 — and the agent then stopped
managing the positions in the old account (a real row opened in another
broker account is never sold). Enter at the AgentHC "which trade signals?"
question likewise turned include_other_trades off. Enter now keeps what is
saved, and a broker switch while the ledger holds real positions warns and
asks first (Enter there keeps the saved broker)."""

import json

import pytest

import agent

REAL = {"option_id": "abc", "qty": 2, "live": True}
DRY = {"option_id": "dry", "qty": 1, "dry": True}


def _drive(home, monkeypatch, capsys, cfg, broker="", switch="", scope="",
           state=None):
    """Run cmd_setup against `cfg` with brokers, wallet, notifications and
    the AI policy step stubbed. Answers go by prompt: `broker`, `switch`
    (the switch confirmation) and `scope` (AgentHC signal scope); every
    other question gets Enter, except the sizing ones (fixed contracts).
    Returns (saved config, prompts asked, brokers set up, printed text)."""
    from sources import agenthc_day_trade_ideas as hc
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if state is not None:
        (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(agent, "notifications_setup", lambda c: c)
    monkeypatch.setattr(agent.llm_policy, "setup", lambda c: c)
    monkeypatch.setattr(hc, "wallet_setup", lambda c: c)
    monkeypatch.setattr(hc, "wallet_from_cfg", lambda c: object())
    set_up = []
    for name, mod in agent.BROKERS.items():
        monkeypatch.setattr(mod, "setup",
                            lambda c, _n=name: set_up.append(_n) or c)
    asked = []
    scope_asked = []

    def answer(prompt=""):
        asked.append(prompt)
        if prompt.startswith("Choose [1/2"):          # scope, then access
            scope_asked.append(prompt)
            return scope if len(scope_asked) == 1 else "1"
        if prompt.startswith("Choose [1-3") and not set_up:
            return broker
        if prompt.startswith("Switch to"):
            return switch
        if prompt.startswith("Choose [1-3"):          # sizing
            return "2"
        if prompt.startswith("Contracts per trade"):
            return "1"
        return ""

    monkeypatch.setattr("builtins.input", answer)
    agent.cmd_setup()
    saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
    return saved, asked, set_up, capsys.readouterr().out


BASE = {"source": "agenthc", "dry_run": False, "include_other_trades": True,
        "sizing_mode": "contracts", "contracts_per_trade": 1}


@pytest.mark.parametrize("saved, default", [
    ("alpaca", "2"), ("moomoo", "3"), ("robinhood", "1")])
def test_enter_keeps_the_saved_broker(home, monkeypatch, capsys, saved, default):
    cfg, asked, set_up, _ = _drive(home, monkeypatch, capsys, {**BASE, "broker": saved})
    assert cfg["broker"] == saved
    assert set_up == [saved]
    assert any(p.startswith(f"Choose [1-3, default {default}]") for p in asked)


def test_a_fresh_setup_still_defaults_to_robinhood(home, monkeypatch, capsys):
    cfg, asked, set_up, _ = _drive(home, monkeypatch, capsys, {"source": "agenthc"})
    assert cfg["broker"] == "robinhood" and set_up == ["robinhood"]
    assert cfg["include_other_trades"] is False


@pytest.mark.parametrize("saved", [True, False])
def test_enter_keeps_the_saved_signal_scope(home, monkeypatch, capsys, saved):
    cfg, asked, _, _ = _drive(home, monkeypatch, capsys,
                              {**BASE, "broker": "alpaca",
                               "include_other_trades": saved})
    assert cfg["include_other_trades"] is saved
    assert any(p.startswith(f"Choose [1/2, default {'2' if saved else '1'}]")
               for p in asked)


@pytest.mark.parametrize("pick", ["1", "2"])
def test_an_explicit_scope_answer_still_wins(home, monkeypatch, capsys, pick):
    cfg, _, _, _ = _drive(home, monkeypatch, capsys,
                          {**BASE, "broker": "alpaca",
                           "include_other_trades": pick == "1"}, scope=pick)
    assert cfg["include_other_trades"] is (pick == "2")


def test_switching_with_real_positions_open_warns_and_enter_keeps(home, capsys,
                                                                  monkeypatch):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": dict(REAL)}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {**BASE, "broker": "alpaca"},
                                     broker="1", switch="", state=state)
    assert cfg["broker"] == "alpaca" and set_up == ["alpaca"]
    assert any(p.startswith("Switch to Robinhood anyway? [y/N]") for p in asked)
    assert "1 real position open at Alpaca" in out
    assert "will NOT sell it" in out


def test_switching_with_real_positions_open_goes_ahead_on_yes(home, capsys,
                                                              monkeypatch):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": dict(REAL),
                           "AAPL|2026-11-21|250.0|P": dict(REAL)}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {**BASE, "broker": "alpaca"},
                                     broker="3", switch="y", state=state)
    assert cfg["broker"] == "moomoo" and set_up == ["moomoo"]
    assert "2 real positions open at Alpaca" in out


@pytest.mark.parametrize("state", [
    None,                                                    # no ledger
    {"positions": {}},
    {"positions": {"NVDA|2026-11-21|200.0|C": dict(DRY)}},   # practice only
])
def test_switching_without_real_positions_asks_nothing(home, monkeypatch, capsys,
                                                       state):
    cfg, asked, set_up, _ = _drive(home, monkeypatch, capsys,
                                   {**BASE, "broker": "alpaca"},
                                   broker="1", state=state)
    assert cfg["broker"] == "robinhood" and set_up == ["robinhood"]
    assert not any(p.startswith("Switch to") for p in asked)


def test_choosing_the_saved_broker_asks_nothing(home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": dict(REAL)}}
    cfg, asked, _, _ = _drive(home, monkeypatch, capsys, {**BASE, "broker": "alpaca"},
                              broker="2", state=state)
    assert cfg["broker"] == "alpaca"
    assert not any(p.startswith("Switch to") for p in asked)
