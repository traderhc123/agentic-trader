"""PR 4 (/v2 parity with the classic dashboard), the server side of the rows
built so far (webui_app/PARITY.md):

  group 0  view-model readings the classic pages show (paid signal days,
           budget hints, the practice preview, a pending code change) and
           their conditions; GET /api/v2/settings additions; the agreement
  group 1  the dashboard switch (config key "dashboard"; "/" and the setup
           wizard's way back follow it)
  group 2  Stop and Start from /v2 (two-step where money is at stake)
  group 3  open at login; finishing a move and moving to a cloud server
           (two-step, irreversible)

Each write is checked against the classic route or chat command it wraps
(same config.json, same calls on stubs), each two-step write against every
way a confirmation can be wrong, and every new route against the login and
the Host/Origin guard. Hermetic: no broker, wallet, AI, cloud or network."""

import hashlib
import json
import os
import re
import time

import pytest

import agent
import guardian_states as gs
import view_model as vm
import webui

from test_v2_settings_api import (  # noqa: F401
    BASE, SECRETS, app, read_cfg, write_cfg)
from test_v2_settings_forms import _callback, rh  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _running(c_app, **kw):
    """A server with an agent behind it (the chat commands are live)."""
    return c_app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
                 agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}),
                 **kw)


def _get_raw(c, path):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    conn.request("GET", path, headers={
        "Host": f"127.0.0.1:{c.port}",
        "Cookie": f"{webui._SESSION_COOKIE}={webui.ui_token()}"})
    r = conn.getresponse()
    body = r.read().decode("utf-8", "replace")
    conn.close()
    return r.status, r.getheader("Location"), body


# ── group 0: view-model readings ────────────────────────────────────────────

def _rows(rows):
    return [{**r, "n": i} for i, r in enumerate(rows)]


def test_paid_signal_days_equal_the_classic_pnl_figures(home):
    """Results.paid_days is agent.pnl_summary's pass figure (what /api/pnl
    shows), from the same log: trades opened on a paid signal day."""
    c1, c2 = ("SPY", "2026-11-13", 700, "C"), ("QQQ", "2026-11-13", 500, "P")
    rows = _rows([
        gs.entry(c1, gs.at(10, 0, "2026-11-05"), 1, 1.0, True),
        gs.exit_(c1, gs.at(11, 0, "2026-11-05"), 1, 40.0, True),
        gs.entry(c2, gs.at(10, 0, "2026-11-06"), 1, 1.0, True),
        gs.exit_(c2, gs.at(10, 0, "2026-11-09"), 1, -15.5, True),  # carried
        gs.entry(c1, gs.at(10, 0, "2026-11-09"), 1, 1.0, False),
        gs.exit_(c1, gs.at(11, 0, "2026-11-09"), 1, 3.0, False),
    ])
    cfg = {**gs.LIVE, "source": "agenthc"}
    cfg.pop("agenthc_api_key", None)
    state = {**gs._state(True), "paid_days": ["2026-11-05", "2026-11-06"]}
    s = vm.build_status(cfg, state, rows, gs.runtime(), now=gs.NOW)
    classic = agent.pnl_summary(cfg, state, rows=rows)
    paid = s["results"]["paid_days"]
    assert paid["real"]["pnl"] == classic["live"]["pass"]["total"] == 24.5
    assert paid["real"]["closed"] == classic["live"]["pass"]["exits"] == 2
    assert paid["real"]["days"] == 2
    # the day the carried trade closed (11-09) is not a paid day; 11-05 is
    assert paid["real"]["paid_on"] == ["2026-11-05"]
    assert paid["practice"]["pnl"] == classic["dry"]["pass"]["total"]
    assert vm.validate(s) == []
    # a subscription key: no per-day charge, so no paid-day figures at all
    s2 = vm.build_status({**cfg, "agenthc_api_key": "k" * 20}, state, rows,
                         gs.runtime(), now=gs.NOW)
    assert s2["results"]["paid_days"] == {"real": None, "practice": None}


def test_budget_hints_are_the_agents_own(home, monkeypatch):
    """Limits.budget_hints is agent._budget_hints (what the classic page's
    "Set budget to $S" shows), read from the same trade log."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    log = [{"ts": f"{today}T15:0{i}:00+00:00", "action": "skip_budget",
            "book": "main", "contract": f"SPY 2026-12-18 {600 + i}C",
            "reason": f"1 contract ≈ ${cost:,} exceeds per-trade budget $500"}
           for i, cost in enumerate((620, 980, 1450))]
    with open(agent.TRADES_PATH, "w", encoding="utf-8") as f:
        for r in log:
            f.write(json.dumps(r) + "\n")
    cfg = {**BASE}
    write_cfg(home, cfg)
    want = agent._budget_hints(cfg)
    assert want and want[0]["suggest"] == 1000
    got = agent.view_snapshot({"running": True})["limits"]["budget_hints"]
    assert got == [{k: v for k, v in h.items() if k != "command"} for h in want]
    s = agent.view_snapshot({"running": True})
    assert [c["code"] for c in s["conditions"]].count("budget_too_small") == 1
    # a contract size: the hint no longer applies, and nothing is said
    write_cfg(home, {**cfg, "sizing_mode": "contracts"})
    s = agent.view_snapshot({"running": True})
    assert s["limits"]["budget_hints"] == []
    assert "budget_too_small" not in [c["code"] for c in s["conditions"]]


def test_the_practice_preview_is_the_dry_run_preview(home, monkeypatch):
    import dry_run_preview
    data = {"fetched_at": time.time() - 60, "trades": [
        {"date": "2026-11-06", "entry_time": "10:01", "ticker": "SPY",
         "direction": "call", "strike": 700, "expiry": "2026-11-06",
         "paper_pnl_pct": 25.0, "exit_kind": "target", "hold_minutes": 40},
        {"date": "2026-11-06", "entry_time": "11:00", "ticker": "TSLA",
         "direction": "put", "strike": 300, "expiry": "2026-11-06",
         "paper_pnl_pct": -10.0, "exit_kind": "stop", "hold_minutes": 12}]}
    monkeypatch.setattr(dry_run_preview, "load", lambda: data)
    cfg = {**BASE, "rule_sides": "calls"}
    write_cfg(home, cfg)
    want = dry_run_preview.summary(cfg)
    got = agent.view_snapshot({"running": True})["signals"]["preview"]
    for k in ("days", "would_take", "wins", "breakeven", "losses",
              "avg_paper_pnl_pct", "unavailable"):
        assert got[k] == want[k], k
    assert [(r["contract"], r["would_take"], r["skip_reason"]) for r in got["rows"]] \
        == [(r["contract"], r["would_take"], r["skip_reason"] or None) for r in want["rows"]]
    assert got["would_take"] == 1  # the put is skipped by "calls only"
    assert [r["would_take"] for r in got["rows"]] == [False, True]
    # LIVE (or "preview off"): none, as the classic page shows none
    write_cfg(home, {**cfg, "dry_run": False})
    assert agent.view_snapshot({"running": True})["signals"]["preview"] is None
    write_cfg(home, {**cfg, "dry_run_preview": False})
    assert agent.view_snapshot({"running": True})["signals"]["preview"] is None


def _propose(home, diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"):
    import self_edit
    with open(self_edit._proposal_path(), "w", encoding="utf-8") as f:
        json.dump({"explanation": "x", "warnings": [],
                   "changes": [{"path": "x.py", "content": "b\n", "diff": diff}]}, f)


def test_a_pending_code_change_is_read_as_the_review_shows_it(home, app):
    write_cfg(home, BASE)
    c = app()
    s = c.get("/api/v2/status")
    assert s["proposal"] == {"pending": False, "diff_sha256": None}
    assert "proposal_pending" not in [x["code"] for x in s["conditions"]]
    _propose(home)
    s = c.get("/api/v2/status")
    shown = c.get("/api/proposal")["diff"]
    assert s["proposal"] == {"pending": True, "diff_sha256": hashlib.sha256(
        shown.encode("utf-8")).hexdigest()}
    assert [x["code"] for x in s["conditions"]].count("proposal_pending") == 1


def test_an_agreement_not_on_file_is_one_condition(home, app, monkeypatch):
    write_cfg(home, BASE)
    c = app()
    s = c.get("/api/v2/status")  # the fixture's consent_ok: True
    assert "consent_needed" not in [x["code"] for x in s["conditions"]]
    monkeypatch.setattr(agent, "consent_ok", lambda: False)
    s = c.get("/api/v2/status")
    assert [x["code"] for x in s["conditions"]].count("consent_needed") == 1
    # a fact nobody handed over is not known: nothing is said
    s = vm.build_status(BASE, {}, [], {"running": True}, now=gs.NOW)
    assert "consent_needed" not in [x["code"] for x in s["conditions"]]


def test_the_fixture_states_still_validate():
    for name in gs.STATES:
        assert vm.validate(gs.status(name)) == [], name


# ── group 0: GET /api/v2/settings additions, the agreement ──────────────────

def test_settings_additions_are_the_classic_readers(home, app, rh, monkeypatch):  # noqa: F811
    cfg = {**BASE, "expiry_guard_et": "15:30", "expiry_alert_et": "off",
           "open_dashboard_at_login": False, "dry_run_preview": False,
           "self_edit_enabled": True}
    write_cfg(home, cfg)
    acc = {"accepted": True, "terms_version": agent.TERMS_VERSION,
           "accepted_at": "2026-10-01T12:00:00+00:00"}
    (home / "acceptance.json").write_text(json.dumps(acc), encoding="utf-8")
    c = app()
    s = c.get("/api/v2/settings")
    assert s["expiry"]["sell_et"] == "15:30" and s["expiry"]["alert_et"] is None
    assert s["expiry"]["described"] == agent._expiry_guard_desc(cfg)
    assert s["computer"]["open_dashboard_at_login"] is False
    assert s["computer"]["dashboard"] == "classic"
    assert s["signals"]["dry_run_preview"] is False
    assert s["code"] == {"self_edit_enabled": True}
    assert s["consent"]["accepted"] is True  # the fixture's consent_ok
    assert s["consent"]["accepted_at"] == acc["accepted_at"]
    assert s["consent"]["terms_version"] == agent.TERMS_VERSION
    assert s["consent"]["current_terms_version"] == agent.TERMS_VERSION
    # the classic wizard's Robinhood warning, the same text
    assert s["broker"]["warning"] is None and c.get("/api/state")["rh_warning"] == ""
    rh.accounts = [{"account_number": "5QR74821", "agentic_allowed": True,
                    "state": "active", "option_level": "option_level_0"}]
    c.confirmed("/api/v2/broker/robinhood", {})
    _callback(c)
    assert "Options are NOT enabled" in c.get("/api/state")["rh_warning"]
    v = c.get("/api/v2/settings")
    # the same warning, as a code: the classic text carries the account number
    assert v["broker"]["warning"] == "options_off"
    assert "5QR74821" not in json.dumps(v)
    # defaults: on, on, classic
    write_cfg(home, BASE)
    s = c.get("/api/v2/settings")
    assert s["computer"]["open_dashboard_at_login"] is True
    assert s["signals"]["dry_run_preview"] is True
    assert s["expiry"]["sell_et"] == agent.EXPIRY_GUARD_DEFAULT
    assert s["code"] == {"self_edit_enabled": False}


def test_the_agreement_text_is_the_one_on_file(home, app):
    write_cfg(home, BASE)
    c = app()
    a = c.get("/api/v2/agreement")
    assert a == {"ok": True, "text": agent._disclaimer_text(),
                 "terms_version": agent.TERMS_VERSION}


def test_no_v2_get_carries_a_secret(home, app, monkeypatch):
    import provision
    page = "https://legend.lnbits.com/wallet?usr=fakeWALLETuser0042&wal=fakeWAL99"
    full = {**BASE, **SECRETS, "lnbits_url": "https://legend.lnbits.com",
            "lnbits_wallet_page": page}
    write_cfg(home, full)
    _propose(home)
    c = _running(app)
    # a cloud move made with a DigitalOcean token: never shown again
    do_token = "dop_v1_fakeDIGITALOCEANtoken00112233"
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating"))
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 0)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    import transfer
    transfer.CYCLE_IDLE.set()
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    first = c.post("/api/v2/move/deploy", {"token": do_token})
    assert do_token not in json.dumps(first)
    gets = [p for p in c.handler.routes_get if p.startswith("/api/v2/")]
    assert {"/api/v2/settings", "/api/v2/status", "/api/v2/agreement",
            "/api/v2/activity"} <= set(gets)
    secrets = list(SECRETS.values()) + [page, "fakeWALLETuser0042", do_token]
    for path in gets:
        text = c.raw("GET", path)[1]
        for value in secrets:
            assert value not in text, (path, value[:12])


# ── group 0: every server route has a /v2 caller or a classic-only reason ──

def _parity_route_kinds():
    """The Server routes table of webui_app/PARITY.md: {(path, method): kind}."""
    with open(os.path.join(ROOT, "webui_app", "PARITY.md"), encoding="utf-8") as f:
        text = f.read()
    part = text.split("## Server routes", 1)[1].split("\n## ", 1)[0]
    out = {}
    for m in re.finditer(r"^\| `(/[^`]+)` \| (GET|POST) \|.*\| (v2|classic|pending: [^|]+) \|$",
                         part, re.M):
        out[(m.group(1), m.group(2))] = m.group(3).strip()
    return out


def _v2_bundle():
    folder = os.path.join(ROOT, "webui_dist", "assets")
    text = ""
    for name in os.listdir(folder):
        if name.endswith(".js"):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                body = f.read()
            if "/api/v2/status" in body:  # the /v2 bundle (classic has none)
                text += body
    assert text, "no /v2 bundle in webui_dist"
    return text


def test_every_route_has_a_v2_caller_or_a_classic_only_reason(home, app):
    write_cfg(home, BASE)
    h = app().handler
    kinds = _parity_route_kinds()
    bundle = _v2_bundle()
    routes = [(p, "GET") for p in h.routes_get] + [(p, "POST") for p in h.routes_post]
    for path, method in routes:
        kind = kinds.get((path, method))
        assert kind is not None, f"{method} {path}: not in PARITY.md's Server routes"
        quoted = re.search(r"[\"'`]" + re.escape(path) + r"(?:[\"'`?])", bundle)
        if kind == "v2":
            assert quoted, f"{method} {path}: PARITY.md says /v2 calls it; the bundle doesn't"
        elif kind.startswith("pending"):
            # a row a later step of PR 4 builds: listed, and not yet called
            assert not quoted, f"{method} {path}: called now; mark it v2 in PARITY.md"
    # every row names a real route
    assert set(kinds) <= set(routes), set(kinds) - set(routes)


# ── group 1: the dashboard switch ───────────────────────────────────────────

def test_the_landing_page_follows_the_saved_choice(home, app, monkeypatch):
    write_cfg(home, BASE)
    c = _running(app)
    land = lambda: _get_raw(c, "/")[1]  # noqa: E731
    assert land() == "/dash"  # default: classic
    r = c.post("/api/v2/settings/computer", {"dashboard": "v2"})
    assert r["ok"] and r["settings"]["computer"]["dashboard"] == "v2"
    assert read_cfg(home) == {**BASE, "dashboard": "v2"}
    assert land() == "/v2"
    c.post("/api/v2/settings/computer", {"dashboard": "classic"})
    assert land() == "/dash"
    # an unknown value is refused, nothing saved
    for bad in ("v3", "", None, True, "/v2", "V2"):
        r = c.post("/api/v2/settings/computer", {"dashboard": bad})
        assert r["ok"] is False and "classic" in r["error"], bad
    assert read_cfg(home)["dashboard"] == "classic"
    # a hand-edited unknown value lands on classic
    write_cfg(home, {**BASE, "dashboard": "nope"})
    assert land() == "/dash"
    assert c.get("/api/v2/settings")["computer"]["dashboard"] == "classic"
    # both pages answer whichever is chosen
    for choice in ("classic", "v2"):
        write_cfg(home, {**BASE, "dashboard": choice})
        assert _get_raw(c, "/dash")[0] == 200
        st, loc, body = _get_raw(c, "/v2")
        assert (st, loc) == (200, None) if webui._app_v2() else st == 302
    # no /v2 in the build: classic, whatever was chosen
    write_cfg(home, {**BASE, "dashboard": "v2"})
    monkeypatch.setattr(webui, "_app_v2", lambda: None)
    assert land() == "/dash"


def test_the_setup_server_lands_on_the_wizard_whatever_was_chosen(home, app):
    write_cfg(home, {**BASE, "dashboard": "v2"})
    c = app()  # no agent behind it: the setup wizard
    assert _get_raw(c, "/")[1] == "/setup"


def test_the_wizard_goes_back_to_the_chosen_dashboard(home, app):
    write_cfg(home, BASE)
    c = _running(app)
    # classic chosen (or nothing): the wizard exactly as before
    st, loc, page = _get_raw(c, "/setup?ui=classic")
    assert (st, loc) == (200, None) and page == webui._WIZARD_HTML
    write_cfg(home, {**BASE, "dashboard": "v2"})
    assert _get_raw(c, "/setup")[1] == "/setup?from=v2"
    assert _get_raw(c, "/setup?ui=classic")[1] == "/setup?ui=classic&from=v2"
    st, loc, page = _get_raw(c, "/setup?ui=classic&from=v2")
    assert st == 200 and 'href="/dash"' not in page and "location='/v2'" in page
    # an explicit from= is kept as it is (never rewritten, never a loop)
    st, loc, page = _get_raw(c, "/setup?ui=classic&from=dash")
    assert st == 200 and page == webui._WIZARD_HTML


DASH_LINK = ('  <a class="btn btn-ghost" href="/v2" title="A redesigned dashboard. '
             'You can come back here at any time.">Try the new dashboard</a>\n')
# sha256 of _DASH_HTML at origin/main c73dfc2
DASH_AT_MAIN = "5df4ddc7ae3c7482db3e134e7964a6ca31c19655ad3fd1b97f3263793554957c"


def test_the_classic_page_only_gains_the_one_link():
    assert webui._DASH_HTML.count(DASH_LINK) == 1
    assert webui._DASH_HTML.count('href="/v2"') == 1
    assert hashlib.sha256(webui._DASH_HTML.replace(DASH_LINK, "").encode()
                          ).hexdigest() == DASH_AT_MAIN


# ── group 2: stop and start ─────────────────────────────────────────────────

POS_VIEW = {"positions": [
    {"id": "SPY|2026-11-13|700|C", "label": "SPY 700C 11/13", "real": True,
     "qty": 2, "contract": {"symbol": "SPY", "expiry": "2026-11-13",
                            "strike": 700, "side": "C"}, "expires_today": False},
    {"id": "QQQ|2026-11-13|500|P", "label": "QQQ 500P", "real": False, "qty": 1,
     "contract": None, "expires_today": False}],
    "broker": {}, "limits": {}}


def test_stop_is_the_chat_stop_after_confirming(home, app, monkeypatch):
    write_cfg(home, BASE)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    c = _running(app, get_view=lambda: POS_VIEW)
    r = c.post("/api/command", {"text": "stop"})
    assert webui.CONTROLS["stop"] is True and read_cfg(home) == BASE
    webui.CONTROLS["stop"] = False
    first = c.post("/api/v2/command", {"action": "stop"})
    assert first["needs_confirm"] is True and webui.CONTROLS["stop"] is False
    p = first["preview"]
    # the real positions nobody sells while it is stopped, and only those
    assert [x["id"] for x in p["unsold"]] == ["SPY|2026-11-13|700|C"]
    assert p["real_positions"] == 1
    assert p["changes"][0]["direction"] == "raises_risk"
    assert any("does not sell" in d for d in p["does_not"])
    done = c.post("/api/v2/command", {"action": "stop", "confirm": p["token"]})
    assert done == {"ok": True, "reply": r["reply"]}
    assert webui.CONTROLS["stop"] is True and read_cfg(home) == BASE


def test_stop_works_while_the_settings_file_is_damaged(home, app, monkeypatch):
    """As the chat's "stop": a stop needs no setting."""
    write_cfg(home, BASE)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    c = _running(app, get_view=lambda: {"positions": []})
    first = c.post("/api/v2/command", {"action": "stop"})
    (home / "config.json").write_text("{not json", encoding="utf-8")
    r = c.post("/api/v2/command", {"action": "stop", "confirm": first["preview"]["token"]})
    assert r["ok"] is True and webui.CONTROLS["stop"] is True


def test_a_stop_reviewed_before_a_new_position_opened_is_refused(home, app, monkeypatch):
    write_cfg(home, BASE)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    view = {"positions": []}
    c = _running(app, get_view=lambda: view)
    first = c.post("/api/v2/command", {"action": "stop"})
    assert first["preview"]["unsold"] == []
    view["positions"] = POS_VIEW["positions"]  # a real position opened meanwhile
    r = c.post("/api/v2/command", {"action": "stop", "confirm": first["preview"]["token"]})
    assert r == {"ok": False, "error": webui._REVIEW_AGAIN}
    assert webui.CONTROLS["stop"] is False


@pytest.fixture
def starting(monkeypatch):
    """keep_running stubbed: what start_local asks it to do."""
    import keep_running
    calls = []
    monkeypatch.setattr(keep_running, "start_background",
                        lambda port=None: calls.append(("start", port)))
    monkeypatch.setattr(keep_running, "set_autostart",
                        lambda on: (calls.append(("autostart", on)), (True, "ok"))[1])
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    return calls


def test_start_is_the_wizards_start(home, app, starting):
    write_cfg(home, BASE)  # practice: no confirmation
    c = app()
    a = c.post("/api/start-local", {})
    b = c.post("/api/v2/agent/start", {})
    assert a == b and a["ok"] is True
    assert starting == [("start", webui.APP_PORT), ("start", webui.APP_PORT)]
    assert read_cfg(home) == BASE


@pytest.mark.parametrize("why", ["consent", "moved", "policy", "no_source"])
def test_start_has_the_wizards_refusals(home, app, starting, monkeypatch, why):
    import llm_policy
    import transfer
    cfg = dict(BASE)
    if why == "consent":
        monkeypatch.setattr(agent, "consent_ok", lambda: False)
    elif why == "moved":
        monkeypatch.setattr(transfer, "moved_away", lambda: {"moved_at": "x"})
    elif why == "policy":
        monkeypatch.setattr(llm_policy, "entries_blocked", lambda c: True)
    else:
        cfg.pop("source")
    write_cfg(home, cfg)
    c = app()
    if why == "consent":
        monkeypatch.setattr(agent, "consent_ok", lambda: False)
    a = c.post("/api/start-local", {})
    for body in ({}, {"confirm": "x"}):
        b = c.post("/api/v2/agent/start", body)
        assert b == a and a["ok"] is False
    assert starting == [] and read_cfg(home) == cfg


def test_starting_in_live_mode_needs_the_confirmation(home, app, starting):
    live = {**BASE, "dry_run": False}
    write_cfg(home, live)
    c = app()
    r = c.post("/api/v2/agent/start", {})
    assert r["needs_confirm"] is True and starting == []
    p = r["preview"]
    assert p["mode"] == "live" and p["changes"][0]["direction"] == "raises_risk"
    assert p["trade_size"]["main"] == {"mode": "budget", "value": 500.0}
    assert p["limits"]["max_entries_per_day"] == 3
    r = c.post("/api/v2/agent/start", {"confirm": p["token"]})
    assert r["ok"] is True and starting == [("start", webui.APP_PORT)]


def test_start_is_refused_while_the_agent_runs(home, app, starting):
    write_cfg(home, BASE)
    c = _running(app)
    r = c.post("/api/v2/agent/start", {})
    assert r["ok"] is False and "already running" in r["error"] and starting == []


# ── group 3: this computer ─────────────────────────────────────────────────

@pytest.mark.parametrize("on", [True, False])
def test_open_at_login_is_the_chat_command(home, app, monkeypatch, on):
    import keep_running
    monkeypatch.setattr(keep_running, "autostart_enabled", lambda: True)
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = _running(app)
    out = same_effect(home, BASE,
                      lambda: c.post("/api/command", {"text": f"open at login {'on' if on else 'off'}"}),
                      lambda: c.post("/api/v2/settings/computer", {"open_dashboard_at_login": on}))
    assert out["open_dashboard_at_login"] is on
    assert c.get("/api/v2/settings")["computer"]["open_dashboard_at_login"] is on


@pytest.fixture
def moving(monkeypatch, tmp_path):
    """transfer/keep_running stubbed around the real move handlers."""
    import keep_running
    import transfer
    calls = []
    monkeypatch.setattr(transfer, "export_bundle", lambda pw: b"x" * 4096)
    monkeypatch.setattr(keep_running, "disable_autostart",
                        lambda: calls.append("autostart off"))
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    return calls


def test_finishing_a_move_is_the_classic_finish_after_confirming(home, app, moving):
    import transfer
    write_cfg(home, BASE)
    c = _running(app, get_view=lambda: POS_VIEW)
    # nothing to finish before the move file exists
    r = c.post("/api/v2/move/finish", {})
    assert r["ok"] is False and "move file" in r["error"]
    assert c.post("/api/move/export", {"passphrase": "long enough"})["ok"]
    a = c.post("/api/move/finish", {})
    classic = (transfer.moved_away() is not None, list(moving), webui.CONTROLS["stop"])
    os.remove(transfer._moved_path())
    moving.clear()
    webui.CONTROLS["stop"] = False
    assert c.post("/api/move/export", {"passphrase": "long enough"})["ok"]
    first = c.post("/api/v2/move/finish", {})
    assert first["needs_confirm"] is True and transfer.moved_away() is None
    assert [x["id"] for x in first["preview"]["unsold"]] == ["SPY|2026-11-13|700|C"]
    b = c.post("/api/v2/move/finish", {"confirm": first["preview"]["token"]})
    assert b == a and b["ok"] is True
    assert (transfer.moved_away() is not None, list(moving), webui.CONTROLS["stop"]) \
        == classic == (True, ["autostart off"], True)
    assert read_cfg(home) == BASE


@pytest.fixture
def cloud(monkeypatch):
    import provision
    import transfer
    made = []
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (made.append((token, region)),
                                               (7, f"droplet 7 creating in {region}"))[1])
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 0)
    monkeypatch.setattr(provision, "LOCAL_STOP_AFTER_SHOWN_S", 0)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    import keep_running
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr(keep_running, "disable_autostart", lambda: None)
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    transfer.CYCLE_IDLE.set()
    return made


def test_the_cloud_move_is_the_classic_deploy_after_confirming(home, app, cloud):
    import transfer
    write_cfg(home, BASE)
    c = app()
    data = {"token": "dop_v1_faketoken0011", "region": "sfo3"}
    a = c.post("/api/deploy", data)
    assert a["ok"] is True and cloud == [("dop_v1_faketoken0011", "sfo3")]
    # moved away now: a second server is refused, by both routes alike
    again = c.post("/api/deploy", data)
    first = c.post("/api/v2/move/deploy", data)
    assert "dop_v1" not in json.dumps(first)
    b = c.post("/api/v2/move/deploy", {**data, "confirm": first["preview"]["token"]})
    assert b == again and b["ok"] is False and len(cloud) == 1
    # from a computer that was never moved: the same call, the same answer
    os.remove(transfer._moved_path())
    first = c.post("/api/v2/move/deploy", data)
    assert first["needs_confirm"] is True and len(cloud) == 1
    b = c.post("/api/v2/move/deploy", {**data, "confirm": first["preview"]["token"]})
    assert b == a and cloud[-1] == ("dop_v1_faketoken0011", "sfo3")


def test_the_cloud_move_keeps_the_one_at_a_time_lock(home, app, cloud, monkeypatch):
    """A confirmed /v2 move while the classic one is still talking to
    DigitalOcean is refused by the same lock: one server, never two."""
    import provision
    import threading
    write_cfg(home, BASE)
    c = app()
    gate, inside = threading.Event(), threading.Event()

    def slow(token, region):
        inside.set()
        gate.wait(10)
        return 7, "droplet 7"
    monkeypatch.setattr(provision, "create_droplet", slow)
    data = {"token": "dop_v1_faketoken0011"}
    first = c.post("/api/v2/move/deploy", data)
    t = threading.Thread(target=lambda: c.post("/api/deploy", data))
    t.start()
    assert inside.wait(10)
    r = c.post("/api/v2/move/deploy", {**data, "confirm": first["preview"]["token"]})
    gate.set()
    t.join(10)
    assert r["ok"] is False and "already being created" in r["error"]


@pytest.mark.parametrize("body", [{"token": ""}, {"token": 5}, {"region": "nyc3"},
                                  {"token": "t", "region": "mars1"},
                                  {"token": "t", "size": "big"}])
def test_bad_cloud_move_payloads_are_refused(home, app, cloud, body):
    write_cfg(home, BASE)
    c = app()
    r = c.post("/api/v2/move/deploy", body)
    assert r["ok"] is False and "needs_confirm" not in r and cloud == []


# ── the two-step rules, for every irreversible or money write here ────────

def _two_step_route(name, app, monkeypatch):
    """(client, path, payload, done(), reset()) for one write: how to tell it
    happened, and how to undo it for the next try."""
    import keep_running
    import provision
    import transfer
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    started = []
    monkeypatch.setattr(keep_running, "start_background",
                        lambda port=None: started.append(port))
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr(keep_running, "disable_autostart", lambda: None)
    monkeypatch.setattr(transfer, "export_bundle", lambda pw: b"x" * 2048)
    made = []
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (made.append(region), (7, "d"))[1])
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 0)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    transfer.CYCLE_IDLE.set()

    def unmove():
        if transfer.moved_away():
            os.remove(transfer._moved_path())
        webui.CONTROLS["stop"] = False
        transfer.HANDOFF["hold"] = False

    if name in ("stop", "move-finish"):
        c = _running(app, get_view=lambda: {"positions": []})
        c.post("/api/move/export", {"passphrase": "long enough"})
    else:
        c = app()
    return {
        "stop": (c, "/api/v2/command", {"action": "stop"},
                 lambda: webui.CONTROLS["stop"], unmove),
        "start-live": (c, "/api/v2/agent/start", {},
                       lambda: bool(started), lambda: started.clear()),
        "move-finish": (c, "/api/v2/move/finish", {},
                        lambda: transfer.moved_away() is not None, unmove),
        "move-deploy": (c, "/api/v2/move/deploy", {"token": "dop_v1_x0011"},
                        lambda: bool(made), lambda: (made.clear(), unmove())),
    }[name]


@pytest.mark.parametrize("name", ["stop", "start-live", "move-finish", "move-deploy"])
def test_every_two_step_write_refuses_a_wrong_confirmation(home, app, monkeypatch, name):
    start = {**BASE, "dry_run": False}  # LIVE: Start needs the confirmation
    write_cfg(home, start)
    c, path, payload, done, reset = _two_step_route(name, app, monkeypatch)
    write_cfg(home, start)
    # no token: a preview, nothing done
    first = c.post(path, payload)
    assert first.get("needs_confirm") is True and not done(), name
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
    token = c.post(path, payload)["preview"]["token"]
    if "token" in payload:  # the cloud move: another token, another region
        for other in ({**payload, "token": "dop_v1_other0022"},
                      {**payload, "region": "lon1"}):
            r = c.post(path, {**other, "confirm": token})
            assert r["ok"] is False and not done(), name
            token = c.post(path, payload)["preview"]["token"]
    # (stop, start and finish take no values: any other key is refused)
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
    r = c.post(path, {**payload, "confirm": token})
    assert r["ok"] is False and not done(), name
    reset()


NEW_POST = ("/api/v2/agent/start", "/api/v2/move/finish", "/api/v2/move/deploy",
            "/api/v2/command", "/api/v2/settings/computer")
NEW_GET = ("/api/v2/agreement", "/api/v2/settings", "/api/v2/status")


def test_new_routes_need_the_login_and_the_right_host_and_origin(home, app, monkeypatch):
    import keep_running
    monkeypatch.setattr(keep_running, "start_background",
                        lambda port=None: pytest.fail("started"))
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    write_cfg(home, BASE)
    c = _running(app)
    own = f"http://127.0.0.1:{c.port}"
    for path in NEW_GET:
        assert c.raw("GET", path, login=False)[0] == 401, path
        assert c.raw("GET", path, host="evil.example")[0] == 403, path
    body = json.dumps({"action": "stop", "dashboard": "v2", "token": "t"})
    for path in NEW_POST:
        assert c.raw("POST", path, body, login=False, origin=own)[0] == 401, path
        assert c.raw("POST", path, body, host="evil.example", origin=own)[0] == 403, path
        assert c.raw("POST", path, body, origin="http://evil.example")[0] == 403, path
    assert read_cfg(home) == BASE and webui.CONTROLS["stop"] is False
    # "/" and /setup are behind the same guard
    assert c.raw("GET", "/", host="evil.example")[0] == 403
    assert c.raw("GET", "/", login=False)[0] == 401
