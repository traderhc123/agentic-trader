"""PR 4 review fixes (the 11-11 review of feat/v2-parity-and-switch): each
test here failed on the code the review read and passes on the fix.

Hermetic: no broker, wallet, AI, AgentHC or network."""

import json

import pytest

import agent
import webui

from test_v2_settings_api import (  # noqa: F401
    BASE, app, read_cfg, write_cfg)
from test_v2_parity import _running
from test_v2_parity_settings import (  # noqa: F401
    OWN, OWN_KEY, _old_wallet_files, lnbits)

WALLET = {**BASE, "lnbits_url": OWN, "lnbits_admin_key": OWN_KEY}


# ── the agent's wallet: made or replaced in /v2 as the classic route does ──

@pytest.mark.parametrize("start, payload", [
    ({**BASE, "source": "manual"}, {}),
    (WALLET, {"replace": True})])
def test_making_a_wallet_needs_the_agreement_as_the_classic_route_does(
        home, app, lnbits, monkeypatch, start, payload):
    write_cfg(home, start)
    c = app()
    # a token got while the agreement was on file...
    first = c.post("/api/v2/wallet/create", payload)
    assert first["needs_confirm"] is True
    monkeypatch.setattr(agent, "consent_ok", lambda: False)
    # ...the classic route refuses now, and so does /v2: no preview, no
    # confirmed make, no LNbits call, nothing saved, no old-wallet file
    refused = {"ok": False, "error": "accept the agreement first"}
    assert c.post("/api/wallet/create", payload) == refused
    assert c.post("/api/v2/wallet/create", payload) == refused
    assert c.post("/api/v2/wallet/create",
                  {**payload, "confirm": first["preview"]["token"]}) == refused
    assert lnbits["made"] == [] and read_cfg(home) == start
    assert _old_wallet_files(home) == []


@pytest.mark.parametrize("other", [
    {**WALLET, "lnbits_url": "https://lnbits.other.example",
     "lnbits_admin_key": "fakeOTHERkey13579"},           # another tab's wallet
    {**WALLET, "source": "manual"}])                     # another tab's source
def test_a_wallet_made_while_the_settings_changed_is_left_unused(
        home, app, lnbits, monkeypatch, other):
    """The LNbits call runs outside the settings lock: a save that lands
    meanwhile (another tab) is never overwritten by the reviewed replace."""
    import lightning_wallet
    write_cfg(home, WALLET)
    c = app()
    made = lightning_wallet.create_wallet

    def slow_create(*a, **k):
        out = made(*a, **k)
        write_cfg(home, other)  # saved while LNbits was answering
        return out
    monkeypatch.setattr(lightning_wallet, "create_wallet", slow_create)
    r = c.confirmed("/api/v2/wallet/create", {"replace": True})
    assert r == {"ok": False, "error": webui._REVIEW_AGAIN}
    assert read_cfg(home) == other and _old_wallet_files(home) == []
    assert len(lnbits["made"]) == 1  # made, empty, and never used


@pytest.mark.parametrize("start, payload", [
    (WALLET, {"replace": 1}), (WALLET, {"replace": 1.0}), (WALLET, {"replace": "true"}),
    ({**BASE, "source": "manual"}, {"replace": 0}),
    ({**BASE, "source": "manual"}, {"replace": 0.0}),
    ({**BASE, "source": "manual"}, {"replace": None})])
def test_the_replace_flag_is_true_or_false_only(home, app, lnbits, start, payload):
    write_cfg(home, start)
    c = app()
    r = c.post("/api/v2/wallet/create", payload)
    assert r["ok"] is False and "needs_confirm" not in r, payload
    assert lnbits["made"] == [] and read_cfg(home) == start


# ── signal source: a subscriber goes back to AgentHC signals ──

@pytest.mark.parametrize("was", ["manual", "url"])
def test_a_subscriber_with_only_a_key_can_switch_back_to_agenthc(home, app, was):
    """No pass and no wallet, a subscription key on file: the preview is
    offered, so the confirmed switch must work (it used to be refused with
    the classic "buy a pass above", the token used up)."""
    from test_v2_settings_api import same_effect
    key = "ahc_fake_saved_key_24681357"
    start = {**BASE, "source": was, "agenthc_api_key": key,
             "include_other_trades": True}
    if was == "url":
        start["source_url"] = "https://feed.example/signals.json"
    write_cfg(home, start)
    c = app()
    # the classic source form, with the key the owner has on file
    out = same_effect(home, start,
                      lambda: c.post("/api/source", {"source": "agenthc", "scope": "all",
                                                     "agenthc_key": key}),
                      lambda: c.confirmed("/api/v2/settings/source", {"source": "agenthc"}))
    assert out == {**start, "source": "agenthc"}
    write_cfg(home, start)
    r = c.confirmed("/api/v2/settings/source", {"source": "agenthc"})
    assert r["ok"] is True and r["saved"] is True and key not in json.dumps(r)
