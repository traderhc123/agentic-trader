"""PR 4 (/v2 parity with the classic dashboard), the server side of groups
4-7 (webui_app/PARITY.md):

  group 4  Settings → Help and support: the connection check, the support
           report, the tip and the agreement (classic routes reused as they
           are, plus GET /api/v2/agreement)
  group 5  Settings → Code changes: reviewing, rejecting and applying the
           agent's proposed code change (apply is two-step, its token bound
           to the sha256 of the exact diff shown), the self-edit switch and
           asking for a change (proposing only, never running a command)
  group 6  Settings → Connections: the expiry-day alert time
  group 7  Signals & pass: signal source, scope, subscription key, practice
           preview, and the payment wallet (create / replace, fund, connect
           your own, recurring, the wallet page link by POST only)

Each write is checked against the classic route or chat command it wraps
(same config.json, same calls on stubs), each two-step write against every
way a confirmation can be wrong, and every new route against the login and
the Host/Origin guard. Hermetic: no broker, wallet, AI, AgentHC or network."""

import json

import pytest

import agent
import webui

from test_v2_settings_api import (  # noqa: F401
    BASE, SECRETS, app, read_cfg, write_cfg)
from test_v2_parity import _running


# ── group 4: Settings → Help and support ────────────────────────────────────

@pytest.fixture
def offline(monkeypatch):
    """The help tools' network checks and the setup checks, answered here."""
    import net_check
    monkeypatch.setattr(net_check, "diagnose",
                        lambda host: {"ok": True, "detail": f"{host} reachable"})
    monkeypatch.setattr(net_check, "proxy_for", lambda url: None)
    monkeypatch.setattr(agent, "doctor_checks",
                        lambda: ([{"ok": True, "label": "config readable",
                                   "detail": ""}], None))


def test_the_help_tools_answer_with_text_and_no_secret(home, app, offline):
    """/v2 shows the classic help tools' text as it is: the report (a GET)
    never carries a key, token or wallet id."""
    write_cfg(home, {**BASE, **SECRETS, "lnbits_url": "https://legend.lnbits.com",
                     "lnbits_wallet_page": "https://legend.lnbits.com/wallet?usr=u0042"})
    c = app()
    net = c.get("/api/help/netcheck")
    assert net["ok"] is True and "AgentHC: OK" in net["text"]
    rep = c.get("/api/help/report")
    assert rep["ok"] is True and "agentic-trader support report" in rep["text"]
    for value in list(SECRETS.values()) + ["usr=u0042"]:
        assert value not in rep["text"], value[:12]
    assert read_cfg(home)["source"] == "agenthc"  # nothing saved


def test_a_tip_never_touches_the_agents_wallet_or_settings(home, app, monkeypatch):
    """The tip is an invoice the owner pays from their own wallet: the
    agent's wallet is never asked anything, and nothing is saved."""
    import lightning_wallet
    from sources import agenthc_day_trade_ideas as hc
    cfg = {**BASE, "lnbits_url": "https://legend.lnbits.com",
           "lnbits_admin_key": SECRETS["lnbits_admin_key"]}
    write_cfg(home, cfg)

    class NoWallet:
        def __init__(self, *a, **k):
            raise AssertionError("a tip asked the agent's wallet")
    monkeypatch.setattr(lightning_wallet, "LNbitsWallet", NoWallet)
    monkeypatch.setattr(lightning_wallet, "create_wallet",
                        lambda *a, **k: pytest.fail("a tip made a wallet"))
    monkeypatch.setattr(webui, "_btc_usd", lambda: 100_000.0)
    quote = {"btc_usd": 100_000.0, "age_seconds": 30, "source": "test"}
    monkeypatch.setattr(hc, "tip_options", lambda: {
        "tiers": [{"amount_usd": 100, "amount_sats": 100_000, "available": True,
                   "reason": None}], "quote": quote, "invoice_valid_seconds": 3600})
    monkeypatch.setattr(hc, "create_tip", lambda n: {
        "payment_request": "lnbc1fakeinvoice", "payment_hash": "a" * 64,
        "amount_sats": 100_000, "btc_usd": 100_000.0, "invoice_valid_seconds": 3600})
    monkeypatch.setattr(hc, "tip_status", lambda h: {"status": "paid"})
    c = app()
    assert c.get("/api/tip/options")["tiers"][0]["available"] is True
    inv = c.post("/api/tip/create", {"amount_usd": 100})
    assert inv["ok"] is True and inv["bolt11"] == "lnbc1fakeinvoice"
    assert c.post("/api/tip/status", {"payment_hash": "a" * 64}) == {
        "ok": True, "status": "paid"}
    assert read_cfg(home) == cfg


def test_the_agreement_route_is_the_text_on_file(home, app):
    write_cfg(home, BASE)
    a = app().get("/api/v2/agreement")
    assert a["text"] == agent._disclaimer_text() and a["ok"] is True
