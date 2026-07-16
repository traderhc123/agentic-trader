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
