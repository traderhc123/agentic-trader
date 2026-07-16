"""Wizard state restore — settings must survive a new wizard session.

A key-based broker connection (alpaca/moomoo) persists as cfg["broker"];
the wizard's /api/state must report it so a returning user isn't forced to
redo the broker step to unlock the ones after it.
"""

import webui


def test_broker_display_prefers_in_session_hint():
    assert webui._broker_display({"broker": "alpaca"}, last4="1234") == "1234"


def test_broker_display_robinhood_last4():
    cfg = {"broker": "robinhood", "robinhood_account": "693916850"}
    assert webui._broker_display(cfg) == "6850"


def test_broker_display_key_based_persists_across_sessions():
    # Fresh session: pending hint empty, only the saved config remains.
    cfg = {"broker": "alpaca", "alpaca_key_id": "AK", "alpaca_secret": "S"}
    assert webui._broker_display(cfg) == "alpaca"


def test_broker_display_unconfigured():
    assert webui._broker_display({}) == ""


def test_restore_notes_full_config():
    cfg = {"anthropic_api_key": "sk-ant-x", "llm_model": "claude-opus-4-8",
           "source": "agenthc", "lnbits_url": "https://w", "include_other_trades": True,
           "sizing_mode": "budget", "budget_per_trade_usd": 500.0,
           "dry_run": True, "max_entries_per_day": 5}
    acc = {"accepted": True, "accepted_at": "2026-07-16T20:00:00+00:00"}
    n = webui._restore_notes(cfg, acc)
    assert n["consent"] == "Agreement signed on 2026-07-16 — on file"
    assert "Anthropic key on file" in n["llm"] and "claude-opus-4-8" in n["llm"]
    assert "Lightning wallet connected" in n["source"]
    assert "main + other trades" in n["source"]
    assert n["sizing"] == "Sizing saved: up to $500 per trade"
    assert n["safety"] == "Safety saved: DRY-RUN · max 5 entries/day"
    # no secrets leak into notes
    assert "sk-ant" not in str(n)


def test_restore_notes_contracts_and_live():
    n = webui._restore_notes({"sizing_mode": "contracts", "contracts_per_trade": 2,
                              "dry_run": False, "max_entries_per_day": 3})
    assert n["sizing"] == "Sizing saved: 2 contracts per trade"
    assert n["safety"] == "Safety saved: LIVE · max 3 entries/day"


def test_restore_notes_empty_config():
    assert webui._restore_notes({}, {}) == {}
