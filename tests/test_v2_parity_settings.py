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
from test_v2_settings_forms import rh  # noqa: F401


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


# ── group 5: Settings → Code changes ────────────────────────────────────────

# what self_edit.propose writes for a new file sources/x.py holding "b\n"
# (/v2 applies only a diff that its content makes against the tree as it is)
DIFF = "--- a/sources/x.py\n+++ b/sources/x.py\n@@ -0,0 +1 @@\n+b\n"


def _propose(diff=DIFF, explanation="x"):
    import self_edit
    with open(self_edit._proposal_path(), "w", encoding="utf-8") as f:
        json.dump({"explanation": explanation, "warnings": ["careful"],
                   "changes": [{"path": "sources/x.py", "content": "b\n",
                                "diff": diff}]}, f)


def _sha(c):
    import hashlib
    return hashlib.sha256(c.get("/api/proposal")["diff"].encode("utf-8")).hexdigest()


@pytest.mark.parametrize("on", [True, False])
def test_the_self_edit_switch_is_the_chat_command(home, app, on):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = _running(app)
    out = same_effect(home, {**BASE, "self_edit_enabled": not on},
                      lambda: c.post("/api/command", {"text": f"self-edit {'on' if on else 'off'}"}),
                      lambda: c.post("/api/v2/settings/code", {"self_edit_enabled": on}))
    assert out["self_edit_enabled"] is on
    assert c.get("/api/v2/settings")["code"] == {"self_edit_enabled": on}
    for bad in ({}, {"self_edit_enabled": "on"}, {"self_edit_enabled": 1},
                {"self_edit_enabled": True, "x": 1}):
        r = c.post("/api/v2/settings/code", bad)
        assert r["ok"] is False and "needs_confirm" not in r, bad


@pytest.fixture
def proposing(monkeypatch):
    """self_edit.propose stubbed (no AI): what it was asked."""
    import self_edit
    asked = []
    monkeypatch.setattr(self_edit, "propose", lambda cfg, req: (
        asked.append((dict(cfg), req)), (True, "Proposal ready: 1 file(s) changed."))[1])
    return asked


def test_asking_for_a_change_only_ever_proposes(home, app, monkeypatch, proposing):
    """The words are a request to the AI, never a command: what would be a
    chat command is proposed, not run, and no setting changes."""
    cfg = {**BASE, "self_edit_enabled": True}
    write_cfg(home, cfg)
    ran = []
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            lambda text: (ran.append(text), agent._apply_command(text))[1],
            lambda: agent._load(agent.CONFIG_PATH, {}))
    # the chat's "code: …": the same call, with the settings on disk
    c.post("/api/command", {"text": "code:  add a broker  "})
    assert proposing == [(cfg, "add a broker")]
    proposing.clear()
    ran.clear()
    for text in ("pause", "dry off", "stop", "set budget 1", "code: x", "go live"):
        r = c.post("/api/v2/proposal/request", {"request": text})
        assert r == {"ok": True, "note": "Proposal ready: 1 file(s) changed."}
    assert [req for _cfg, req in proposing] == [
        "pause", "dry off", "stop", "set budget 1", "code: x", "go live"]
    assert ran == [] and read_cfg(home) == cfg and webui.CONTROLS["stop"] is False


def test_asking_for_a_change_needs_self_edit_and_an_ai(home, app, proposing):
    write_cfg(home, BASE)  # self-edit off
    c = app()
    r = c.post("/api/v2/proposal/request", {"request": "add a broker"})
    assert r["ok"] is False and "propose code changes" in r["error"]
    write_cfg(home, {**{k: v for k, v in BASE.items() if not k.startswith("llm_")},
                     "self_edit_enabled": True})
    r = c.post("/api/v2/proposal/request", {"request": "add a broker"})
    assert r["ok"] is False and "AI helper" in r["error"]
    for bad in ({}, {"request": ""}, {"request": "  "}, {"request": 5},
                {"request": "x" * 4001}, {"request": "a\x00b"}, {"request": "x", "y": 1}):
        assert c.post("/api/v2/proposal/request", bad)["ok"] is False, bad
    assert proposing == []


@pytest.fixture
def applying(monkeypatch):
    """self_edit.apply_and_restart stubbed: how it was called."""
    import self_edit
    calls = []

    def apply(restart=True, expect_sha256=None):
        prop = self_edit.current()
        if expect_sha256 is not None and self_edit.diff_sha256(prop) != expect_sha256:
            return False, "not the one you reviewed"
        calls.append(restart)
        self_edit.reject()
        return True, "Applied 1 file(s)."
    monkeypatch.setattr(self_edit, "apply_and_restart", apply)
    return calls


def test_applying_is_the_classic_apply_after_confirming(home, app, applying):
    write_cfg(home, BASE)
    c = _running(app)
    _propose()
    assert c.post("/api/proposal/apply", {}) == {"ok": True, "reply": "Applied 1 file(s)."}
    _propose()
    sha = _sha(c)
    first = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
    assert first["needs_confirm"] is True and applying == [True]
    p = first["preview"]
    assert p["diff_sha256"] == sha and p["files"] == ["sources/x.py"]
    assert p["warnings"] == ["careful"] and p["restarts"] is True
    assert any("Practice and LIVE" in d for d in p["does_not"])
    done = c.post("/api/v2/proposal/apply", {"diff_sha256": sha, "confirm": p["token"]})
    assert done == {"ok": True, "reply": "Applied 1 file(s)."}
    assert applying == [True, True]  # the same call: restart, as the agent runs
    assert read_cfg(home) == BASE
    # the setup server (no agent running): applied, no restart, both ways
    c2 = app()
    _propose()
    c2.post("/api/proposal/apply", {})
    _propose()
    c2.confirmed("/api/v2/proposal/apply", {"diff_sha256": _sha(c2)})
    assert applying[2:] == [False, False]


def test_a_diff_that_changed_after_the_review_is_never_applied(home, app, applying):
    write_cfg(home, BASE)
    c = _running(app)
    _propose()
    sha = _sha(c)
    first = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
    _propose(diff=DIFF + "+c\n")  # a new proposal arrives meanwhile
    r = c.post("/api/v2/proposal/apply", {"diff_sha256": sha, "confirm": first["preview"]["token"]})
    assert r["ok"] is False and "not the one you reviewed" in r["error"] and applying == []
    # a preview for a diff nobody was shown is never made
    r = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
    assert r["ok"] is False and "needs_confirm" not in r
    # nothing waiting: nothing to apply
    import self_edit
    self_edit.reject()
    r = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
    assert r["ok"] is False and "No code change" in r["error"] and applying == []
    for bad in ({}, {"diff_sha256": "abc"}, {"diff_sha256": sha.upper()},
                {"diff_sha256": sha, "x": 1}):
        assert c.post("/api/v2/proposal/apply", bad)["ok"] is False, bad


def test_the_apply_guard_holds_where_the_proposal_is_read(home):
    """self_edit.apply_and_restart(expect_sha256=…) leaves any other
    proposal alone (the last word, after the confirmation)."""
    import self_edit
    _propose()
    ok, msg = self_edit.apply_and_restart(restart=False, expect_sha256="0" * 64)
    assert ok is False and "not the one you reviewed" in msg
    assert self_edit.current() is not None  # still waiting, untouched


def test_rejecting_is_the_classic_reject(home, app):
    import self_edit
    write_cfg(home, BASE)
    c = app()
    _propose()
    assert c.post("/api/proposal/reject", {})["ok"] is True
    assert self_edit.current() is None and c.get("/api/proposal") == {"pending": False}


# ── group 6: Settings → Connections ─────────────────────────────────────────

@pytest.mark.parametrize("chat, sent", [("15:40", "15:40"), ("off", "off"),
                                        ("9:45", "09:45"), ("15:59", "15:59")])
def test_the_expiry_alert_time_is_the_chat_command(home, app, chat, sent):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = _running(app)
    out = same_effect(home, BASE,
                      lambda: c.post("/api/command", {"text": f"expiry alert {chat}"}),
                      lambda: c.post("/api/v2/settings/alerts", {"expiry_alert_et": sent}))
    assert out["expiry_alert_et"] == ("off" if chat == "off" else sent.zfill(5))
    shown = c.get("/api/v2/settings")["expiry"]
    assert shown["alert_et"] == (None if chat == "off" else sent.zfill(5))


@pytest.mark.parametrize("bad", ["16:00", "09:00", "9:29", "25:00", "on", "OFF",
                                 "", "3pm", None, 1540, True])
def test_an_expiry_alert_time_outside_the_day_is_refused(home, app, bad):
    write_cfg(home, BASE)
    c = _running(app)
    # the chat refuses it too (it shows the usage, and saves nothing)
    if isinstance(bad, str) and bad and bad.lower() not in ("on", "off"):
        c.post("/api/command", {"text": f"expiry alert {bad}"})
        assert read_cfg(home) == BASE
    r = c.post("/api/v2/settings/alerts", {"expiry_alert_et": bad})
    assert r["ok"] is False and "09:30 to 15:59" in r["error"], bad
    assert read_cfg(home) == BASE


def test_the_alert_time_saves_with_the_alert_targets_or_alone(home, app):
    write_cfg(home, BASE)
    c = app()
    r = c.post("/api/v2/settings/alerts", {"ntfy": "my-topic-name", "expiry_alert_et": "15:30"})
    assert r["ok"] is True
    assert read_cfg(home) == {**BASE, "ntfy_topic": "my-topic-name", "expiry_alert_et": "15:30"}


def test_the_v2_robinhood_sign_in_is_the_classic_start(home, app, rh):  # noqa: F811
    """POST /api/v2/broker/robinhood (built in PR 3) hands back the address
    GET /api/rh/start gives, from the same sign-in code."""
    write_cfg(home, BASE)
    c = app()
    classic = c.get("/api/rh/start")["url"]
    assert c.confirmed("/api/v2/broker/robinhood", {})["url"] == classic
    assert rh.started == [True, True]


# ── group 7: Signals & pass ─────────────────────────────────────────────────

FEED = "https://feed.example/signals.json?token=fakeFEEDtoken0099"
OWN = "https://lnbits.own.example"
OWN_KEY = "fakeOWNadminKEY24680"


@pytest.fixture
def lnbits(monkeypatch):
    """LNbits answered here (install before app()): wallets made, balance
    checks and invoices asked for."""
    import lightning_wallet
    log = {"made": [], "balance": [], "invoices": []}

    class FakeWallet:
        def __init__(self, url, key):
            self.url, self.key = url, key

        def balance_sats(self):
            log["balance"].append((self.url, self.key))
            return 4321

        def create_invoice(self, sats, memo=""):
            log["invoices"].append((self.url, self.key, sats, memo))
            return f"lnbc{sats}n1fakeinvoice"

    def create_wallet(instance_url=None, name="agentic-trader"):
        n = len(log["made"])
        log["made"].append(n)
        return ("https://demo.lnbits.com", f"fakeNEWadminKEY{n:04d}",
                f"https://demo.lnbits.com/wallet?usr=fakeNEWuser{n}&wal=w{n}")
    monkeypatch.setattr(lightning_wallet, "LNbitsWallet", FakeWallet)
    monkeypatch.setattr(lightning_wallet, "create_wallet", create_wallet)
    monkeypatch.setattr(webui, "_qr_matrix", lambda data: None)
    return log


def _old_wallet_files(home):
    return sorted(p.name for p in home.iterdir() if p.name.startswith("old-wallet-"))


def test_the_signal_source_is_the_classic_source_save(home, app):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = app()
    start = {**BASE, "include_other_trades": True}
    # a JSON feed (the saved "which signals" choice is kept)
    out = same_effect(home, start,
                      lambda: c.post("/api/source", {"source": "url", "source_url": FEED}),
                      lambda: c.confirmed("/api/v2/settings/source", {"source": "url", "url": FEED}))
    assert out["source"] == "url" and out["source_url"] == FEED
    assert out["include_other_trades"] is True
    # its own commands file
    out = same_effect(home, start,
                      lambda: c.post("/api/source", {"source": "manual"}),
                      lambda: c.confirmed("/api/v2/settings/source", {"source": "manual"}))
    assert out["source"] == "manual"
    # back to AgentHC with a pass on file: the scope as it was saved
    back = {**start, "source": "manual", "feed_pass": {"token": "t"}}
    out = same_effect(home, back,
                      lambda: c.post("/api/source", {"source": "agenthc", "scope": "all"}),
                      lambda: c.confirmed("/api/v2/settings/source", {"source": "agenthc"}))
    assert out["source"] == "agenthc" and out["include_other_trades"] is True
    # the feed's address is never read back whole (it can carry a token)
    write_cfg(home, {**BASE, "source": "url", "source_url": FEED})
    v = c.get("/api/v2/settings")
    assert v["signals"]["source_site"] == "feed.example"
    assert "fakeFEEDtoken" not in json.dumps(v)


def test_a_source_change_needs_a_working_choice(home, app):
    write_cfg(home, {**BASE, "source": "manual", "agenthc_api_key": ""})
    c = app()
    r = c.post("/api/v2/settings/source", {"source": "agenthc"})
    assert r["ok"] is False and "signal days" in r["error"]
    for bad in ({"source": "rss"}, {"source": "url"}, {"source": "url", "url": "http://x.example"},
                {"source": "manual", "url": FEED}, {"source": "url", "url": 5}, {}):
        r = c.post("/api/v2/settings/source", bad)
        assert r["ok"] is False and "needs_confirm" not in r, bad
    assert c.post("/api/v2/settings/source", {"source": "manual"}) == {
        "ok": True, "saved": False,
        "note": "Nothing to change: that is your signal source already."}
    assert read_cfg(home)["source"] == "manual"


def test_the_subscription_key_is_the_classic_save_and_write_only(home, app):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = app()
    key = "ahc_fake_new_key_77778888"
    start = {**BASE, "include_other_trades": True}
    out = same_effect(home, start,
                      lambda: c.post("/api/source", {"source": "agenthc", "scope": "all",
                                                     "agenthc_key": key}),
                      lambda: c.post("/api/v2/settings/source", {"agenthc_key": key}))
    assert out["agenthc_api_key"] == key and out["include_other_trades"] is True
    v = c.get("/api/v2/settings")
    assert v["signals"]["api_key"] == {"set": True, "last4": "8888"}
    assert key not in json.dumps(v)
    # with another source, or with anything else in the same save: refused
    write_cfg(home, {**BASE, "source": "manual"})
    r = c.post("/api/v2/settings/source", {"agenthc_key": key})
    assert r["ok"] is False and "AgentHC" in r["error"]
    for bad in ({"agenthc_key": ""}, {"agenthc_key": 7}, {"agenthc_key": key, "source": "agenthc"}):
        assert c.post("/api/v2/settings/source", bad)["ok"] is False, bad
    assert read_cfg(home) == {**BASE, "source": "manual"}


@pytest.mark.parametrize("scope", ["all", "main"])
def test_which_signals_is_the_classic_scope_and_the_chat_command(home, app, scope):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = _running(app)
    start = {**BASE, "include_other_trades": scope == "main"}
    a = same_effect(home, start,
                    lambda: c.post("/api/source/scope", {"scope": scope}),
                    lambda: c.confirmed("/api/v2/settings/signals", {"scope": scope}))
    b = same_effect(home, start,
                    lambda: c.post("/api/command", {"text": f"other trades {'on' if scope == 'all' else 'off'}"}),
                    lambda: c.confirmed("/api/v2/settings/signals", {"scope": scope}))
    assert a == b and a["include_other_trades"] is (scope == "all")
    # more signals raise the risk; fewer lower it
    write_cfg(home, start)
    ch = c.post("/api/v2/settings/signals", {"scope": scope})["preview"]["changes"][0]
    assert ch["direction"] == ("raises_risk" if scope == "all" else "lowers_risk")
    # another source has no such choice, as the classic route says
    write_cfg(home, {**start, "source": "manual"})
    assert c.post("/api/source/scope", {"scope": scope})["ok"] is False
    assert c.post("/api/v2/settings/signals", {"scope": scope})["ok"] is False


@pytest.mark.parametrize("on", [True, False])
def test_the_practice_preview_switch_is_the_chat_command(home, app, on):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = _running(app)
    out = same_effect(home, {**BASE, "dry_run_preview": not on},
                      lambda: c.post("/api/command", {"text": f"preview {'on' if on else 'off'}"}),
                      lambda: c.post("/api/v2/settings/signals", {"dry_run_preview": on}))
    assert out["dry_run_preview"] is on
    assert c.get("/api/v2/settings")["signals"]["dry_run_preview"] is on
    for bad in ({"dry_run_preview": "on"}, {"scope": "both"}, {"preview": True}, {}):
        assert c.post("/api/v2/settings/signals", bad)["ok"] is False, bad


def test_making_the_agents_wallet_is_the_classic_create(home, app, lnbits):
    from test_v2_settings_api import same_effect
    start = {**BASE, "source": "manual"}
    write_cfg(home, start)
    c = app()
    out = same_effect(home, start,
                      lambda: c.post("/api/wallet/create", {}),
                      lambda: c.confirmed("/api/v2/wallet/create", {}),
                      scrub=lambda o: o.update(lnbits_admin_key="k", lnbits_wallet_page="p"))
    assert out["source"] == "agenthc" and out["max_autopay_sats"] == 30_000
    assert len(lnbits["made"]) == 2
    # the preview names the source change too, and no link to the wallet
    write_cfg(home, start)
    first = c.post("/api/v2/wallet/create", {})
    assert [x["key"] for x in first["preview"]["changes"]] == ["wallet", "source"]
    done = c.post("/api/v2/wallet/create", {"confirm": first["preview"]["token"]})
    assert done["ok"] is True and "wallet?usr" not in json.dumps(done)
    assert done["settings"]["signals"]["wallet"]["page_on_file"] is True


def test_replacing_the_agents_wallet_keeps_the_old_ones_details(home, app, lnbits):
    have = {**BASE, "lnbits_url": "https://demo.lnbits.com",
            "lnbits_admin_key": "fakeOLDadminKEY1111",
            "lnbits_wallet_page": "https://demo.lnbits.com/wallet?usr=fakeOLDuser"}
    write_cfg(home, have)
    c = app()
    # a wallet is there: only a replace, said as one
    r = c.post("/api/v2/wallet/create", {})
    assert r["ok"] is False and "replace" in r["error"] and lnbits["made"] == []
    a = c.post("/api/wallet/create", {"replace": True})
    after_classic = read_cfg(home)
    files = _old_wallet_files(home)
    assert a["ok"] is True and len(files) == 1
    write_cfg(home, have)
    first = c.post("/api/v2/wallet/create", {"replace": True})
    assert any("not emptied or deleted" in d for d in first["preview"]["does_not"])
    assert "fakeOLDuser" not in json.dumps(first)
    b = c.post("/api/v2/wallet/create", {"replace": True, "confirm": first["preview"]["token"]})
    assert b["ok"] is True and len(_old_wallet_files(home)) == 2
    assert "fakeOLDuser" not in json.dumps(b) and "fakeNEWuser" not in json.dumps(b)
    after_v2 = read_cfg(home)
    for k in ("lnbits_admin_key", "lnbits_wallet_page"):
        after_classic.pop(k), after_v2.pop(k)
    assert after_classic == after_v2


def test_funding_the_wallet_is_the_classic_invoice_after_confirming(home, app, lnbits):
    have = {**BASE, "lnbits_url": OWN, "lnbits_admin_key": OWN_KEY}
    write_cfg(home, have)
    c = app()
    a = c.post("/api/wallet/fund", {"sats": 25000})
    first = c.post("/api/v2/wallet/fund", {"sats": 25000})
    assert first["needs_confirm"] is True and len(lnbits["invoices"]) == 1
    b = c.post("/api/v2/wallet/fund", {"sats": 25000, "confirm": first["preview"]["token"]})
    assert a == b and b["ok"] is True
    assert lnbits["invoices"] == [(OWN, OWN_KEY, 25000, "fund agentic-trader")] * 2
    assert read_cfg(home) == have
    # the classic form raises a small amount to 1,000 sats; /v2 refuses it
    assert c.post("/api/wallet/fund", {"sats": 10})["sats"] == 1000
    for bad in (999, 0, -5, "25000", 2.5, None, True):
        r = c.post("/api/v2/wallet/fund", {"sats": bad})
        assert r["ok"] is False and "needs_confirm" not in r, bad
    # a cap the wallet is known to have: refused above it, with the range
    write_cfg(home, {**have, "lnbits_max_invoice_sats": 100_000})
    r = c.post("/api/v2/wallet/fund", {"sats": 100_001})
    assert r["ok"] is False and "100,000" in r["error"]
    # no wallet: nothing to fund
    write_cfg(home, BASE)
    assert c.post("/api/v2/wallet/fund", {"sats": 5000})["ok"] is False


def test_connecting_your_own_wallet_is_the_classic_source_save(home, app, lnbits):
    from test_v2_settings_api import same_effect
    have = {**BASE, "lnbits_url": "https://demo.lnbits.com",
            "lnbits_admin_key": "fakeOLDadminKEY1111", "include_other_trades": True}
    write_cfg(home, have)
    c = app()
    out = same_effect(home, have,
                      lambda: c.post("/api/source", {"source": "agenthc", "scope": "all",
                                                     "lnbits_url": OWN, "lnbits_key": OWN_KEY}),
                      lambda: c.confirmed("/api/v2/wallet/connect", {"url": OWN, "admin_key": OWN_KEY}))
    assert out["lnbits_url"] == OWN and out["lnbits_admin_key"] == OWN_KEY
    assert out["include_other_trades"] is True
    assert lnbits["balance"] == [(OWN, OWN_KEY)] * 2
    assert len(_old_wallet_files(home)) == 2  # the old wallet's details, each time
    # the key never comes back, in the preview or after
    write_cfg(home, have)
    first = c.post("/api/v2/wallet/connect", {"url": OWN, "admin_key": OWN_KEY})
    assert OWN_KEY not in json.dumps(first)
    done = c.post("/api/v2/wallet/connect", {"url": OWN, "admin_key": OWN_KEY,
                                             "confirm": first["preview"]["token"]})
    assert done["ok"] is True and OWN_KEY not in json.dumps(done)
    for bad in ({"url": "http://lnbits.example", "admin_key": "k" * 12},
                {"url": OWN}, {"admin_key": OWN_KEY}, {"url": OWN, "admin_key": ""}):
        r = c.post("/api/v2/wallet/connect", bad)
        assert r["ok"] is False and "needs_confirm" not in r, bad


def test_the_wallet_page_link_comes_only_from_its_post(home, app):
    page = "https://demo.lnbits.com/wallet?usr=fakePAGEuser77&wal=w1"
    have = {**BASE, "lnbits_url": "https://demo.lnbits.com",
            "lnbits_admin_key": "fakeOLDadminKEY1111", "lnbits_wallet_page": page}
    write_cfg(home, have)
    c = app()
    assert c.post("/api/v2/wallet/page", {}) == {"ok": True, "url": page}
    for path in [p for p in c.handler.routes_get if p.startswith("/api/v2/")]:
        assert "fakePAGEuser77" not in c.raw("GET", path)[1], path
    v = c.get("/api/v2/settings")["signals"]["wallet"]
    assert v["page_on_file"] is True and v["site"] == "demo.lnbits.com"
    assert v["warning"] and "TEST" in v["warning"]
    assert c.post("/api/v2/wallet/page", {"x": 1})["ok"] is False
    write_cfg(home, {**have, "lnbits_wallet_page": ""})
    assert c.post("/api/v2/wallet/page", {})["ok"] is False
    write_cfg(home, BASE)
    assert c.post("/api/v2/wallet/page", {})["ok"] is False


# ── the two-step rules, for every money or irreversible write built here ──

def _case_proposal_apply(c, home, monkeypatch):
    import self_edit
    applied = []
    monkeypatch.setattr(self_edit, "apply_and_restart",
                        lambda restart=True, expect_sha256=None: (
                            applied.append(expect_sha256), (True, "Applied."))[1])
    _propose()
    return {"path": "/api/v2/proposal/apply", "payload": {"diff_sha256": _sha(c)},
            "done": lambda: bool(applied), "reset": applied.clear,
            "start": BASE, "again": _propose}


def _case_source(c, home, monkeypatch):
    return {"path": "/api/v2/settings/source", "payload": {"source": "url", "url": FEED},
            "done": lambda: read_cfg(home).get("source") == "url",
            "reset": lambda: None, "start": BASE}


def _case_scope(c, home, monkeypatch):
    return {"path": "/api/v2/settings/signals", "payload": {"scope": "all"},
            "done": lambda: read_cfg(home).get("include_other_trades") is True,
            "reset": lambda: None, "start": BASE}


WALLET = {**BASE, "lnbits_url": OWN, "lnbits_admin_key": OWN_KEY}


def _case_wallet_create(c, home, monkeypatch, log):
    return {"path": "/api/v2/wallet/create", "payload": {"replace": True},
            "done": lambda: read_cfg(home).get("lnbits_admin_key") != OWN_KEY,
            "reset": lambda: None, "start": WALLET}


def _case_wallet_fund(c, home, monkeypatch, log):
    return {"path": "/api/v2/wallet/fund", "payload": {"sats": 5000},
            "done": lambda: bool(log["invoices"]), "reset": log["invoices"].clear,
            "start": WALLET}


def _case_wallet_connect(c, home, monkeypatch, log):
    return {"path": "/api/v2/wallet/connect",
            "payload": {"url": "https://lnbits.other.example", "admin_key": "fakeOTHERkey13579"},
            "done": lambda: read_cfg(home).get("lnbits_admin_key") == "fakeOTHERkey13579",
            "reset": lambda: None, "start": WALLET}


# name -> case(client, home, monkeypatch[, lnbits log]): {path, payload,
# done(), reset(), start config, again() (puts back what the write used up)}
TWO_STEP = {"proposal-apply": _case_proposal_apply, "source": _case_source,
            "scope": _case_scope}
WALLET_TWO_STEP = {"wallet-create": _case_wallet_create,
                   "wallet-fund": _case_wallet_fund,
                   "wallet-connect": _case_wallet_connect}


def _check_two_step(name, home, app, monkeypatch, log=None):
    c = _running(app)
    case = (TWO_STEP[name](c, home, monkeypatch) if name in TWO_STEP
            else WALLET_TWO_STEP[name](c, home, monkeypatch, log))
    path, payload, done, reset, start = (case[k] for k in (
        "path", "payload", "done", "reset", "start"))
    write_cfg(home, start)
    # no token: a preview, nothing done
    first = c.post(path, payload)
    assert first.get("needs_confirm") is True and not done(), (name, first)
    token = first["preview"]["token"]
    # wrong tokens
    for bad in ("nope", "", None, 7, token[:-1] + ("0" if token[-1] != "0" else "1")):
        r = c.post(path, {**payload, "confirm": bad})
        assert r["ok"] is False and not done(), (name, bad)
    # expired
    token = c.post(path, payload)["preview"]["token"]
    real = webui._clock()
    monkeypatch.setattr(webui, "_clock", lambda: real + 121)
    r = c.post(path, {**payload, "confirm": token})
    monkeypatch.setattr(webui, "_clock", lambda: real)
    assert r["ok"] is False and "expired" in r["error"] and not done(), name
    # another payload than the one previewed
    token = c.post(path, payload)["preview"]["token"]
    r = c.post(path, {**payload, "confirm": token, "extra": 1})
    assert r["ok"] is False and not done(), name
    for k, v in payload.items():  # each value changed in turn
        token = c.post(path, payload)["preview"]["token"]
        other = (not v) if isinstance(v, bool) else (v + 1000 if isinstance(v, int)
                                                     else v[:-1] + ("x" if v[-1] != "x" else "y"))
        r = c.post(path, {**payload, k: other, "confirm": token})
        assert r["ok"] is False and not done(), (name, k)
    # settings changed after the preview
    token = c.post(path, payload)["preview"]["token"]
    write_cfg(home, {**start, "max_entries_per_day": 9})
    r = c.post(path, {**payload, "confirm": token})
    assert r == {"ok": False, "error": webui._REVIEW_AGAIN} and not done(), name
    write_cfg(home, start)
    # the right one: done once; the same token again: refused
    token = c.post(path, payload)["preview"]["token"]
    r = c.post(path, {**payload, "confirm": token})
    assert r["ok"] is True and done(), (name, r)
    reset()
    write_cfg(home, start)
    if case.get("again"):
        case["again"]()
    r = c.post(path, {**payload, "confirm": token})
    assert r["ok"] is False and not done(), name


@pytest.mark.parametrize("name", ["proposal-apply", "source", "scope"])
def test_code_and_signal_writes_refuse_a_wrong_confirmation(home, app, monkeypatch, name):
    _check_two_step(name, home, app, monkeypatch)


@pytest.mark.parametrize("name", sorted(WALLET_TWO_STEP))
def test_wallet_writes_refuse_a_wrong_confirmation(home, app, monkeypatch, lnbits, name):
    _check_two_step(name, home, app, monkeypatch, lnbits)


NEW_POST = {"/api/v2/settings/code": {"self_edit_enabled": True},
            "/api/v2/proposal/request": {"request": "x"},
            "/api/v2/proposal/apply": {"diff_sha256": "0" * 64},
            "/api/v2/settings/alerts": {"expiry_alert_et": "15:40"},
            "/api/v2/settings/source": {"source": "manual"},
            "/api/v2/settings/signals": {"dry_run_preview": False},
            "/api/v2/wallet/create": {},
            "/api/v2/wallet/fund": {"sats": 5000},
            "/api/v2/wallet/connect": {"url": OWN, "admin_key": OWN_KEY},
            "/api/v2/wallet/page": {}}


def test_new_routes_need_the_login_and_the_right_host_and_origin(home, app, monkeypatch, lnbits):
    import self_edit
    monkeypatch.setattr(self_edit, "propose", lambda *a: pytest.fail("proposed"))
    monkeypatch.setattr(self_edit, "apply_and_restart", lambda *a, **k: pytest.fail("applied"))
    write_cfg(home, {**BASE, "self_edit_enabled": False})
    c = _running(app)
    own = f"http://127.0.0.1:{c.port}"
    for path, data in NEW_POST.items():
        body = json.dumps(data)
        assert c.raw("POST", path, body, login=False, origin=own)[0] == 401, path
        assert c.raw("POST", path, body, host="evil.example", origin=own)[0] == 403, path
        assert c.raw("POST", path, body, origin="http://evil.example")[0] == 403, path
    assert read_cfg(home) == {**BASE, "self_edit_enabled": False}
    assert lnbits == {"made": [], "balance": [], "invoices": []}
