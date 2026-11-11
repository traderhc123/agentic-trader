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


# ── group 5: Settings → Code changes ────────────────────────────────────────

DIFF = "--- a/sources/x.py\n+++ b/sources/x.py\n@@ -1 +1 @@\n-a\n+b\n"


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


# name -> case(client, home, monkeypatch): {path, payload, done(), reset(),
# start config, again() (puts back what the write used up, or None)}
TWO_STEP = {"proposal-apply": _case_proposal_apply}


def _check_two_step(name, home, app, monkeypatch):
    c = _running(app)
    case = TWO_STEP[name](c, home, monkeypatch)
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


@pytest.mark.parametrize("name", ["proposal-apply"])
def test_code_writes_refuse_a_wrong_confirmation(home, app, monkeypatch, name):
    _check_two_step(name, home, app, monkeypatch)


NEW_POST = {"/api/v2/settings/code": {"self_edit_enabled": True},
            "/api/v2/proposal/request": {"request": "x"},
            "/api/v2/proposal/apply": {"diff_sha256": "0" * 64}}


def test_new_routes_need_the_login_and_the_right_host_and_origin(home, app, monkeypatch):
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
