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

REAL = {"option_id": "abc", "qty": 2, "live": True, "acct": "alpaca:live"}
UNTAGGED = {"option_id": "old", "qty": 1, "live": True}  # pre-tag row
DRY = {"option_id": "dry", "qty": 1, "dry": True}


def _drive(home, monkeypatch, capsys, cfg, broker="", switch="", scope="",
           state=None, sets=None):
    """Run cmd_setup against `cfg` with brokers, wallet, notifications and
    the AI policy step stubbed. Answers go by prompt: `broker`, `switch`
    (the switch confirmation) and `scope` (AgentHC signal scope); every
    other question gets Enter, except the sizing ones (fixed contracts).
    `sets`: what the stubbed broker step writes (paper or live, keys).
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
                            lambda c, _n=name: set_up.append(_n)
                            or {**c, **(sets or {})})
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


# ── each row's own broker (acct tag), not the saved one ──────────────────────

def _real_at(acct):
    return {**REAL, "acct": acct}


RH_TAG = __import__("brokers").account_tag({"broker": "robinhood"})
LIVE_ALPACA = {**BASE, "broker": "alpaca", "alpaca_paper": False}


def test_rows_already_at_the_picked_broker_ask_nothing(home, monkeypatch, capsys):
    # saved Alpaca, but the open row is a Robinhood row (left from an
    # earlier switch): switching to Robinhood is what lets the agent manage
    # it again — no warning, and Enter must not keep Alpaca
    # (the row's tag is the Robinhood account the stubbed setup leaves on:
    # another Robinhood account would be a switch of its own)
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at(RH_TAG)}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {**BASE, "broker": "alpaca"},
                                     broker="1", state=state)
    assert cfg["broker"] == "robinhood" and set_up == ["robinhood"]
    assert not any(p.startswith("Switch to") for p in asked)
    assert "WARNING" not in out


def test_mixed_rows_warn_only_about_the_ones_left_behind(home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:live"),
                           "AAPL|2026-11-21|250.0|P": _real_at("robinhood:abc")}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {**BASE, "broker": "alpaca"},
                                     broker="1", switch="", state=state)
    assert cfg["broker"] == "alpaca" and set_up == ["alpaca"]
    assert "1 real position open at Alpaca" in out
    assert "NVDA" in out and "AAPL" not in out
    assert "open at Robinhood" not in out


def test_each_row_is_named_at_its_own_broker(home, monkeypatch, capsys):
    # fresh config (no broker saved = Robinhood) with moomoo rows left in
    # the ledger: the warning names moomoo, not Robinhood
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("moomoo:real")}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {"source": "agenthc"},
                                     broker="2", switch="y", state=state)
    assert cfg["broker"] == "alpaca" and set_up == ["alpaca"]
    assert "1 real position open at moomoo" in out
    assert "open at Robinhood" not in out
    assert "in the moomoo app" in out


def test_a_fresh_config_picking_the_rows_broker_asks_nothing(home, monkeypatch,
                                                              capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:paper")}}
    cfg, asked, set_up, _ = _drive(home, monkeypatch, capsys,
                                   {"source": "agenthc"}, broker="2", state=state)
    assert cfg["broker"] == "alpaca" and set_up == ["alpaca"]
    assert not any(p.startswith("Switch to") for p in asked)


def test_untagged_rows_are_not_promised_unsold(home, monkeypatch, capsys):
    # a row with no acct tag gets the new account's tag on the first read,
    # so the agent may try to sell it there: the warning must not say it
    # won't
    state = {"positions": {"NVDA|2026-11-21|200.0|C": dict(UNTAGGED)}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {**BASE, "broker": "alpaca"},
                                     broker="1", switch="", state=state)
    assert cfg["broker"] == "alpaca" and set_up == ["alpaca"]
    assert "1 real position open at Alpaca" in out
    assert "will NOT sell" not in out
    assert "may try to sell it at Robinhood, where it is not held" in out


# ── the saved broker picked again: rows elsewhere are still named ───────────

def test_a_config_reset_still_names_rows_at_another_broker(home, monkeypatch,
                                                           capsys):
    # config.json lost its broker (reads as Robinhood) while an Alpaca row
    # is open: Enter keeps Robinhood, and the Alpaca row is named — never
    # stranded without a word. Nothing to switch away from: no question.
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:live")}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {"source": "agenthc"}, broker="",
                                     state=state)
    assert cfg["broker"] == "robinhood" and set_up == ["robinhood"]
    assert not any(p.startswith("Switch to") for p in asked)
    assert "1 real position open at Alpaca" in out and "NVDA" in out
    assert "will NOT sell it" in out and "in the Alpaca app" in out


def test_re_picking_the_saved_broker_names_rows_left_elsewhere(home, monkeypatch,
                                                               capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:live"),
                           "AAPL|2026-11-21|250.0|P": _real_at("moomoo:real")}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     LIVE_ALPACA, broker="",
                                     state=state)
    assert cfg["broker"] == "alpaca" and set_up == ["alpaca"]
    assert not any(p.startswith("Switch to") for p in asked)
    assert "open at moomoo" in out and "AAPL" in out
    assert "open at Alpaca" not in out and "NVDA" not in out


def test_re_picking_the_saved_broker_with_its_own_rows_says_nothing(
        home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:live"),
                           "AAPL|2026-11-21|250.0|P": dict(UNTAGGED)}}
    cfg, asked, _, out = _drive(home, monkeypatch, capsys,
                                LIVE_ALPACA, broker="2",
                                state=state)
    assert cfg["broker"] == "alpaca" and "WARNING" not in out
    assert not any(p.startswith("Switch to") for p in asked)


# ── paper <-> live within one broker: rows left in the other mode ───────────

PAPER_ALPACA = {**BASE, "broker": "alpaca", "alpaca_paper": True,
                "alpaca_key_id": "paper-key", "alpaca_secret": "paper-secret"}
TO_LIVE = {"alpaca_paper": False, "alpaca_key_id": "live-key",
           "alpaca_secret": "live-secret"}


def test_going_live_names_the_paper_rows_and_enter_keeps_paper(
        home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:paper")}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys, PAPER_ALPACA,
                                     broker="", switch="", state=state,
                                     sets=TO_LIVE)
    assert set_up == ["alpaca"]
    assert any(p.startswith("Switch to Alpaca live anyway? [y/N]") for p in asked)
    assert "1 open position in your Alpaca paper account: NVDA" in out
    assert "After switching to Alpaca live the agent will NOT sell it" in out
    assert "Keeping your saved broker settings (Alpaca paper)." in out
    # the whole broker step is undone: keys and mode as saved
    assert cfg["alpaca_paper"] is True
    assert (cfg["alpaca_key_id"], cfg["alpaca_secret"]) == ("paper-key",
                                                            "paper-secret")


def test_going_live_goes_ahead_on_yes(home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:paper"),
                           "AAPL|2026-11-21|250.0|P": _real_at("alpaca:paper")}}
    cfg, asked, _, out = _drive(home, monkeypatch, capsys, PAPER_ALPACA,
                                switch="y", state=state, sets=TO_LIVE)
    assert "2 open positions in your Alpaca paper account" in out
    assert cfg["alpaca_paper"] is False and cfg["alpaca_key_id"] == "live-key"


def test_going_to_paper_only_warns(home, monkeypatch, capsys):
    # the safe direction is never blocked: the live rows are named, no question
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:live")}}
    cfg, asked, _, out = _drive(home, monkeypatch, capsys, LIVE_ALPACA,
                                state=state, sets={"alpaca_paper": True})
    assert not any(p.startswith("Switch to") for p in asked)
    assert "1 open position in your Alpaca live account: NVDA" in out
    assert "the agent will NOT sell it" in out and "in the Alpaca app" in out
    assert cfg["alpaca_paper"] is True


def test_moomoo_going_real_asks_too(home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("moomoo:paper")}}
    cfg, asked, _, out = _drive(home, monkeypatch, capsys,
                                {**BASE, "broker": "moomoo", "moomoo_paper": True},
                                state=state, sets={"moomoo_paper": False})
    assert any(p.startswith("Switch to moomoo real anyway?") for p in asked)
    assert "in your moomoo paper account" in out
    assert cfg["moomoo_paper"] is True


def test_another_robinhood_account_asks(home, monkeypatch, capsys):
    from brokers import account_tag
    old = account_tag({"broker": "robinhood", "robinhood_account": "111"})
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at(old)}}
    cfg, asked, _, out = _drive(home, monkeypatch, capsys,
                                {**BASE, "broker": "robinhood",
                                 "robinhood_account": "111"},
                                state=state, sets={"robinhood_account": "222"})
    assert any(p.startswith("Switch to this Robinhood account anyway?")
               for p in asked)
    assert "in another Robinhood account" in out
    assert cfg["robinhood_account"] == "111"


def test_untagged_rows_are_not_promised_unsold_on_a_mode_switch(
        home, monkeypatch, capsys):
    state = {"positions": {"NVDA|2026-11-21|200.0|C": dict(UNTAGGED)}}
    cfg, asked, _, out = _drive(home, monkeypatch, capsys, PAPER_ALPACA,
                                switch="y", state=state, sets=TO_LIVE)
    assert "will NOT sell" not in out
    assert ("may try to sell it in your Alpaca live account, where it is "
            "not held") in out
    assert cfg["alpaca_paper"] is False


@pytest.mark.parametrize("state", [
    None, {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:live")}},
    {"positions": {"NVDA|2026-11-21|200.0|C": dict(DRY)}}])
def test_a_mode_switch_without_rows_left_behind_asks_nothing(
        home, monkeypatch, capsys, state):
    cfg, asked, _, out = _drive(home, monkeypatch, capsys, PAPER_ALPACA,
                                state=state, sets=TO_LIVE)
    assert not any(p.startswith("Switch to") for p in asked)
    assert "WARNING" not in out and cfg["alpaca_paper"] is False


def test_a_new_broker_in_live_mode_names_its_paper_rows(home, monkeypatch,
                                                        capsys):
    # saved Robinhood; Alpaca paper rows left from earlier: picking Alpaca
    # is no broker warning (the rows are there), but going live strands
    # them. Enter keeps everything as saved — Robinhood included.
    state = {"positions": {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:paper")}}
    cfg, asked, set_up, out = _drive(home, monkeypatch, capsys,
                                     {**BASE, "broker": "robinhood"},
                                     broker="2", state=state, sets=TO_LIVE)
    assert set_up == ["alpaca"]
    assert [p for p in asked if p.startswith("Switch to")] == [
        "Switch to Alpaca live anyway? [y/N]: "]
    assert cfg["broker"] == "robinhood" and "alpaca_key_id" not in cfg


# ── the dashboard's broker connect names them too ──────────────────────────

def test_switch_left_behind_covers_both_brokers_and_modes(home):
    import agent as A
    rows = {"NVDA|2026-11-21|200.0|C": _real_at("alpaca:paper"),
            "AAPL|2026-11-21|250.0|P": _real_at("moomoo:real"),
            "MSFT|2026-11-21|400.0|C": _real_at("alpaca:live")}
    saved = {"broker": "alpaca", "alpaca_paper": True}
    lines = A._switch_left_behind(saved, {**saved, "alpaca_paper": False}, rows)
    text = " ".join(h + " " + a for h, a in lines)
    assert "in your Alpaca paper account: NVDA" in text
    assert "open at moomoo: AAPL" in text and "MSFT" not in text
    assert A._switch_left_behind(saved, saved, {
        "NVDA|2026-11-21|200.0|C": _real_at("alpaca:paper")}) == []
