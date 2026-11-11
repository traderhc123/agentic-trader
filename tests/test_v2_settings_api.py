"""The /v2 settings API (webui.start_app): typed routes over the classic code.

One implementation per setting: from the same starting config, the classic
route (or chat command) and the /v2 route leave the same config.json and the
same policy file. Money-affecting writes need the server's two-step
confirmation. Every route sits behind the same login and Host/Origin guard
as the rest of the API, and a GET never carries a secret. All over real HTTP,
with no broker, wallet, AI or network behind it."""

import copy
import http.client
import json
import threading

import pytest

import agent
import webui

SECRETS = {"alpaca_key_id": "AKFAKEKEYID0123456789",
           "alpaca_secret": "fakeSECRETvalue98765432109876",
           "llm_api_key": "sk-ant-fake-llm-key-000011112222",
           "discord_webhook_url": "https://discord.com/api/webhooks/1/fakeHOOKtoken777",
           "ntfy_topic": "fake-ntfy-topic-secret-4242",
           "telegram_bot_token": "123456:FAKEtelegramTOKENabcdef",
           "telegram_chat_id": "987654321012",
           "agenthc_api_key": "ahc_fake_api_key_55556666",
           "lnbits_admin_key": "fakeLNBITSadminKEY13579"}

# a set-up agent: every first-setup default already in place
BASE = {"source": "agenthc", "broker": "alpaca",
        "alpaca_key_id": SECRETS["alpaca_key_id"],
        "alpaca_secret": SECRETS["alpaca_secret"], "alpaca_paper": True,
        "dry_run": True, "max_entries_per_day": 3, "max_daily_loss_usd": 200.0,
        "max_event_age_s": 300, "poll_seconds": 30,
        "sizing_mode": "budget", "budget_per_trade_usd": 500.0,
        "contracts_per_trade": 1, "max_contracts_per_trade": 100,
        "keep_awake": True, "llm_provider": "anthropic",
        "llm_api_key": SECRETS["llm_api_key"], "llm_model": "claude-x",
        "llm_fallback": "skip"}


@pytest.fixture
def app(home, monkeypatch):
    """start(**start_app arguments) -> client with get/post/raw, on the
    real routes and the real login + Host/Origin guard."""
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    real_server = webui._AppServer
    made, servers = {}, []

    class Capture:
        def __init__(self, addr, handler):
            made["handler"] = handler

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    old = (webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS)

    def start(*args, **kwargs):
        monkeypatch.setattr(webui, "_AppServer", Capture)
        webui.start_app(*args, **kwargs)
        monkeypatch.setattr(webui, "_AppServer", real_server)
        srv = real_server(("127.0.0.1", 0), made["handler"])
        servers.append(srv)
        port = srv.server_address[1]
        hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        webui._ALLOWED_HOSTS = hosts
        webui._ALLOWED_ORIGINS = tuple(f"http://{h}" for h in hosts)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return Client(port)

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS = old


class Client:
    def __init__(self, port):
        self.port = port

    def raw(self, method, path, body=None, login=True, host=None, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if login:
            headers["Cookie"] = f"{webui._SESSION_COOKIE}={webui.ui_token()}"
        if origin:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, text

    def get(self, path):
        status, text = self.raw("GET", path)
        assert status == 200, text
        return json.loads(text)

    def post(self, path, data):
        status, text = self.raw("POST", path, json.dumps(data),
                                origin=f"http://127.0.0.1:{self.port}")
        assert status == 200, text
        return json.loads(text)

    def confirmed(self, path, data, **extra):
        """Preview, then confirm with the preview's token."""
        first = self.post(path, data)
        assert first.get("needs_confirm") is True, first
        return self.post(path, {**data, "confirm": first["preview"]["token"],
                                **extra})


def cfg_path(home):
    return home / "config.json"


def write_cfg(home, cfg):
    cfg_path(home).write_text(json.dumps(cfg), encoding="utf-8")


def read_cfg(home):
    return json.loads(cfg_path(home).read_text(encoding="utf-8"))


def policy_file(home):
    p = home / "policy.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def same_effect(home, start, classic, v2, ignore=(), scrub=None):
    """Run `classic` and then `v2`, each from `start` (config + no policy
    file): both must leave the same config.json and policy file."""
    def run(fn):
        write_cfg(home, copy.deepcopy(start))
        for p in (home / "policy.md", home / "policy.md.disabled"):
            if p.exists():
                p.unlink()
        fn()
        out = read_cfg(home)
        for k in ignore:
            out.pop(k, None)
        if scrub:
            scrub(out)
        return out, policy_file(home), (home / "policy.md.disabled").exists()
    a, b = run(classic), run(v2)
    assert a == b
    return a[0]


# ── one implementation per setting: classic vs /v2 ──────────────────────────

def test_trade_size_both_books_same_as_classic_sizing(home, app):
    write_cfg(home, BASE)
    c = app()
    out = same_effect(
        home, BASE,
        lambda: c.post("/api/sizing", {"main": {"mode": "budget", "value": "750"},
                                       "other": {"mode": "percent", "value": "2"}}),
        lambda: c.confirmed("/api/v2/settings/limits", {"trade_size": {
            "main": {"mode": "budget", "value": 750},
            "other": {"mode": "percent", "value": 2}}}))
    assert out["budget_per_trade_usd"] == 750.0
    assert out["other_sizing_mode"] == "percent" and out["other_percent_per_trade"] == 2.0
    # "same as main" clears the other book's override, as the classic form does
    start = {**BASE, "other_sizing_mode": "budget", "other_budget_per_trade_usd": 90.0}
    out = same_effect(
        home, start,
        lambda: c.post("/api/sizing", {"main": {"mode": "contracts", "value": "2"},
                                       "other": {"mode": "same"}}),
        lambda: c.confirmed("/api/v2/settings/limits", {"trade_size": {
            "main": {"mode": "contracts", "value": 2}, "other": {"mode": "same"}}}))
    assert "other_sizing_mode" not in out and out["contracts_per_trade"] == 2


def test_cap_and_loss_limit_same_as_classic_safety(home, app):
    write_cfg(home, BASE)
    c = app()
    out = same_effect(
        home, BASE,
        lambda: c.post("/api/safety", {"cap": "7", "seeded": True, "max_loss": "350"}),
        lambda: c.confirmed("/api/v2/settings/limits",
                            {"max_entries_per_day": 7, "max_daily_loss_usd": 350}))
    assert out["max_entries_per_day"] == 7 and out["max_daily_loss_usd"] == 350.0
    # 0 = no daily loss limit, both ways
    out = same_effect(
        home, BASE,
        lambda: c.post("/api/safety", {"max_loss": "0"}),
        lambda: c.confirmed("/api/v2/settings/limits", {"max_daily_loss_usd": 0}))
    assert out["max_daily_loss_usd"] == 0.0


def test_positions_same_as_the_chat_command(home, app):
    write_cfg(home, BASE)
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    out = same_effect(
        home, BASE,
        lambda: c.post("/api/command", {"text": "set positions 6"}),
        lambda: c.confirmed("/api/v2/settings/limits", {"max_open_positions": 6}))
    assert out["max_open_positions"] == 6


def test_rules_same_as_classic_safety(home, app):
    write_cfg(home, BASE)
    c = app()
    out = same_effect(
        home, BASE,
        lambda: c.post("/api/safety", {
            "rule_tickers": "spy, QQQ", "rule_skip_tickers": "TSLA",
            "rule_sides": "calls", "rule_min_strike": "5",
            "rule_skip_0dte": True, "rule_loss_streak": "2"}),
        lambda: c.post("/api/v2/settings/rules", {
            "rule_tickers": ["SPY", "QQQ"], "rule_skip_tickers": ["TSLA"],
            "rule_sides": "calls", "rule_min_strike": 5,
            "rule_skip_0dte": True, "rule_loss_streak": 2}))
    assert out["rule_tickers"] == ["SPY", "QQQ"] and out["rule_sides"] == "calls"
    assert out["rule_min_strike"] == 5.0 and out["rule_loss_streak"] == 2


def test_written_policy_same_as_classic_policy_save_and_disable(home, app):
    write_cfg(home, BASE)
    c = app()
    text = "Never trade on FOMC days.\nOnly SPY."
    same_effect(home, BASE,
                lambda: c.post("/api/policy", {"policy": text}),
                lambda: c.post("/api/v2/settings/rules", {"policy": text}))
    assert policy_file(home) == text + "\n"

    def with_policy(fn):
        def run():
            (home / "policy.md").write_text("old rules\n", encoding="utf-8")
            fn()
        return run
    same_effect(home, BASE,
                with_policy(lambda: c.post("/api/policy/disable", {})),
                with_policy(lambda: c.post("/api/v2/settings/rules", {"policy": None})))
    assert policy_file(home) is None
    assert (home / "policy.md.disabled").read_text(encoding="utf-8") == "old rules\n"
    # no AI to check it: both refuse, and nothing is written
    no_ai = {k: v for k, v in BASE.items() if not k.startswith("llm_")}
    for path in ("/api/policy", "/api/v2/settings/rules"):
        write_cfg(home, no_ai)
        r = c.post(path, {"policy": text})
        assert r["ok"] is False and "AI" in r["error"]
        assert read_cfg(home) == no_ai and policy_file(home) is None


def test_alerts_same_as_classic_safety_and_null_clears(home, app):
    write_cfg(home, BASE)
    c = app()
    targets = {"discord": SECRETS["discord_webhook_url"],
               "ntfy": SECRETS["ntfy_topic"], "tg_token": SECRETS["telegram_bot_token"],
               "tg_chat": SECRETS["telegram_chat_id"]}
    out = same_effect(home, BASE,
                      lambda: c.post("/api/safety", targets),
                      lambda: c.post("/api/v2/settings/alerts", targets))
    assert out["ntfy_topic"] == SECRETS["ntfy_topic"]
    # the one deliberate addition: null clears a target (only that one)
    write_cfg(home, out)
    r = c.post("/api/v2/settings/alerts", {"ntfy": None})
    assert r["ok"] is True
    after = read_cfg(home)
    assert "ntfy_topic" not in after
    assert after == {k: v for k, v in out.items() if k != "ntfy_topic"}


def test_computer_same_as_classic(home, app, monkeypatch):
    import keep_running
    calls = []
    monkeypatch.setattr(keep_running, "set_autostart",
                        lambda on: (calls.append(on), (True, "noted"))[1])
    monkeypatch.setattr(keep_running, "autostart_status",
                        lambda: {"supported": True, "enabled": bool(calls and calls[-1]),
                                 "path": "/x", "how": "test"})
    write_cfg(home, BASE)
    c = app()
    same_effect(home, BASE,
                lambda: c.post("/api/autostart", {"enabled": True}),
                lambda: c.post("/api/v2/settings/computer", {"autostart": True}))
    assert calls == [True, True]
    out = same_effect(home, BASE,
                      lambda: c.post("/api/safety", {"keep_awake": False}),
                      lambda: c.post("/api/v2/settings/computer", {"keep_awake": False}))
    assert out["keep_awake"] is False
    r = c.post("/api/v2/settings/computer", {"autostart": False})
    assert r["ok"] is True and r["settings"]["computer"]["autostart"]["enabled"] is False


def test_ai_is_the_classic_llm_handler(home, app, monkeypatch):
    import llm_client
    pinged = []
    monkeypatch.setattr(llm_client, "complete",
                        lambda trial, *a, **k: pinged.append(trial["llm_model"]) or "OK")
    write_cfg(home, BASE)
    c = app()
    data = {"provider": "anthropic", "key": "sk-ant-another-fake-key-999988887777",
            "model": "claude-y"}
    out = same_effect(home, BASE,
                      lambda: c.post("/api/llm", data),
                      lambda: c.post("/api/v2/settings/ai", data))
    assert out["llm_model"] == "claude-y" and pinged == ["claude-y", "claude-y"]
    # a model that doesn't answer: refused the same way, nothing saved
    monkeypatch.setattr(llm_client, "complete", lambda *a, **k: (_ for _ in ()).throw(
        llm_client.LLMError("auth", "nope")))
    write_cfg(home, BASE)
    r = c.post("/api/v2/settings/ai", data)
    assert r["ok"] is False and "didn't answer" in r["error"]
    assert read_cfg(home) == BASE


def test_broker_connect_is_the_classic_handler_after_confirming(home, app, monkeypatch):
    from brokers import alpaca
    monkeypatch.setattr(alpaca, "verify", lambda cfg: (True, "connected (test)"))
    start = {k: v for k, v in BASE.items()
             if k not in ("broker", "alpaca_key_id", "alpaca_secret", "alpaca_paper")}
    write_cfg(home, start)
    c = app()
    data = {"broker": "alpaca", "values": {"alpaca_key_id": "AKNEWKEY000111222",
                                           "alpaca_secret": "NEWSECRET000111222333",
                                           "alpaca_paper": False}}
    out = same_effect(home, start,
                      lambda: c.post("/api/broker/connect", data),
                      lambda: c.confirmed("/api/v2/broker/connect", data))
    assert out["broker"] == "alpaca" and out["alpaca_paper"] is False
    # the preview says it raises the risk (a live account) and echoes no key
    write_cfg(home, start)
    first = c.post("/api/v2/broker/connect", data)
    ch = first["preview"]["changes"][0]
    assert ch["direction"] == "raises_risk" and ch["new"] == {"id": "alpaca", "paper": False}
    assert "NEWSECRET" not in json.dumps(first)
    assert read_cfg(home) == start
    # the broker refuses the keys: nothing saved, the classic error
    monkeypatch.setattr(alpaca, "verify", lambda cfg: (False, "keys refused"))
    r = c.confirmed("/api/v2/broker/connect", data)
    assert r == {"ok": False, "error": "keys refused"}
    assert read_cfg(home) == start


def test_pass_buy_is_the_classic_handler_after_confirming(home, app, monkeypatch):
    from sources import agenthc_day_trade_ideas as hc
    bought = []

    def buy_pass(days, extend_token=None):
        bought.append(days)
        return {"payment_request": f"lnbc{days}fakeinvoice", "amount_sats": 100 * days,
                "amount_usd_approx": days, "pass_token": f"tok{days}",
                "access_until": "2026-12-01"}
    monkeypatch.setattr(hc, "buy_pass", buy_pass)
    write_cfg(home, BASE)
    c = app()
    replies = []
    out = same_effect(home, BASE,
                      lambda: replies.append(c.post("/api/pass/buy", {"days": 5})),
                      lambda: replies.append(c.confirmed("/api/v2/pass/buy", {"days": 5})),
                      scrub=lambda cfg: cfg["feed_pass_pending"].pop("bought_at"))
    assert bought == [5, 5] and replies[0]["bolt11"] == replies[1]["bolt11"]
    a = {k: v for k, v in replies[0].items()}
    assert {k: v for k, v in replies[1].items()} == a
    assert out["source"] == "agenthc" and out["feed_pass_pending"]["token"] == "tok5"
    # the preview alone makes no invoice
    c.post("/api/v2/pass/buy", {"days": 20})
    assert bought == [5, 5]


def test_recurring_day_pass_is_the_classic_switch_after_confirming(home, app):
    start = {**BASE, "lnbits_url": "https://wallet.example",
             "lnbits_admin_key": SECRETS["lnbits_admin_key"]}
    write_cfg(home, start)
    c = app()
    out = same_effect(home, start,
                      lambda: c.post("/api/wallet/recurring", {"on": True}),
                      lambda: c.confirmed("/api/v2/wallet/recurring", {"on": True}),
                      ignore=("day_pass_recurring_on_at",))
    assert out["day_pass_recurring"] is True
    # no wallet: refused (the classic switch would save a setting that can't work)
    write_cfg(home, BASE)
    r = c.post("/api/v2/wallet/recurring", {"on": True})
    assert r["ok"] is False and read_cfg(home) == BASE


def test_commands_are_the_chat_commands(home, app):
    write_cfg(home, BASE)
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    same_effect(home, BASE,
                lambda: c.post("/api/command", {"text": "pause"}),
                lambda: c.post("/api/v2/command", {"action": "pause"}))
    paused = {**BASE, "paused": True}
    same_effect(home, paused,
                lambda: c.post("/api/command", {"text": "resume"}),
                lambda: c.post("/api/v2/command", {"action": "resume"}))
    live = {**BASE, "dry_run": False}
    out = same_effect(home, live,
                      lambda: c.post("/api/command", {"text": "dry on"}),
                      lambda: c.post("/api/v2/command",
                                     {"action": "set_mode", "mode": "practice"}))
    assert out["dry_run"] is True
    out = same_effect(home, BASE,
                      lambda: c.post("/api/command", {"text": "go live"}),
                      lambda: c.confirmed("/api/v2/command",
                                          {"action": "set_mode", "mode": "live"},
                                          acknowledge=True))
    assert out["dry_run"] is False


def test_command_refused_by_the_agent_is_an_error(home, app, monkeypatch):
    import transfer
    write_cfg(home, {**BASE, "paused": True})
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    monkeypatch.setattr(transfer, "moved_away", lambda: {"moved_at": "x"})
    r = c.post("/api/v2/command", {"action": "resume"})
    assert r["ok"] is False and "Can't resume here" in r["error"]
    assert read_cfg(home)["paused"] is True
    # no agent behind the server: nothing to command
    c2 = app()
    r = c2.post("/api/v2/command", {"action": "pause"})
    assert r["ok"] is False and "isn't running" in r["error"]


def test_going_live_is_refused_while_a_policy_blocks_entries(home, app):
    no_ai = {k: v for k, v in BASE.items() if not k.startswith("llm_")}
    write_cfg(home, no_ai)
    (home / "policy.md").write_text("Only SPY.\n", encoding="utf-8")
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    r = c.post("/api/v2/command", {"action": "set_mode", "mode": "live"})
    assert r["ok"] is False and "policy" in r["error"]
    assert read_cfg(home)["dry_run"] is True


# ── validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,body", [
    ("/api/v2/settings/limits", {"max_entries_per_day": 3, "dry_run": False}),
    ("/api/v2/settings/limits", {"max_entries_per_day": "7"}),
    ("/api/v2/settings/limits", {"max_entries_per_day": 2.5}),
    ("/api/v2/settings/limits", {"max_entries_per_day": True}),
    ("/api/v2/settings/limits", {"max_entries_per_day": 0}),
    ("/api/v2/settings/limits", {"max_entries_per_day": 100000}),
    ("/api/v2/settings/limits", {"max_daily_loss_usd": -1}),
    ("/api/v2/settings/limits", {"max_open_positions": None}),
    ("/api/v2/settings/limits", {"trade_size": {"main": {"mode": "budget", "value": -5}}}),
    ("/api/v2/settings/limits", {"trade_size": {"main": {"mode": "budget", "value": 0}}}),
    ("/api/v2/settings/limits", {"trade_size": {"main": {"mode": "percent", "value": 101}}}),
    ("/api/v2/settings/limits", {"trade_size": {"main": {"mode": "contracts", "value": 1.5}}}),
    ("/api/v2/settings/limits", {"trade_size": {"main": {"mode": "same"}}}),
    ("/api/v2/settings/limits", {"trade_size": {"shadow": {"mode": "budget", "value": 5}}}),
    ("/api/v2/settings/limits", {"trade_size": {"main": {"mode": "budget", "value": 5, "x": 1}}}),
    ("/api/v2/settings/limits", {}),
    ("/api/v2/settings/limits", []),
    ("/api/v2/settings/rules", {"rule_tickers": "SPY QQQ"}),
    ("/api/v2/settings/rules", {"rule_tickers": ["SPY", "not a ticker"]}),
    ("/api/v2/settings/rules", {"rule_sides": "straddles"}),
    ("/api/v2/settings/rules", {"rule_min_strike": -1}),
    ("/api/v2/settings/rules", {"rule_skip_0dte": "yes"}),
    ("/api/v2/settings/rules", {"rule_loss_streak": 1.5}),
    ("/api/v2/settings/rules", {"policy": "   "}),
    ("/api/v2/settings/rules", {"never_trade": ["TSLA"]}),
    ("/api/v2/settings/alerts", {"discord": ""}),
    ("/api/v2/settings/alerts", {"discord": "http://insecure.example/hook"}),
    ("/api/v2/settings/alerts", {"ntfy": 42}),
    ("/api/v2/settings/alerts", {"email": "a@b.c"}),
    ("/api/v2/settings/computer", {"keep_awake": "on"}),
    ("/api/v2/settings/computer", {"autostart": 1}),
    ("/api/v2/settings/ai", {"model": 5}),
    ("/api/v2/settings/ai", {"temperature": "0"}),
    ("/api/v2/broker/connect", {"broker": "nope", "values": {}}),
    ("/api/v2/broker/connect", {"broker": "alpaca", "values": {"other_field": "x"}}),
    ("/api/v2/broker/connect", {"broker": "alpaca", "values": {"alpaca_paper": "no"}}),
    ("/api/v2/pass/buy", {"days": 0}),
    ("/api/v2/pass/buy", {"days": "5"}),
    ("/api/v2/pass/buy", {"days": 5, "price": 1}),
    ("/api/v2/wallet/recurring", {"on": "true"}),
    ("/api/v2/command", {"action": "stop"}),
    ("/api/v2/command", {"action": "pause", "scope": "all"}),
    ("/api/v2/command", {"action": "set_mode", "mode": "paper"}),
    ("/api/v2/command", {"text": "go live"}),
])
def test_bad_payloads_are_refused_and_change_nothing(home, app, monkeypatch, path, body):
    import keep_running
    monkeypatch.setattr(keep_running, "set_autostart",
                        lambda on: pytest.fail("autostart changed"))
    write_cfg(home, BASE)
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    r = c.post(path, body)
    assert r["ok"] is False and r["error"], r
    assert "needs_confirm" not in r
    assert read_cfg(home) == BASE and policy_file(home) is None


@pytest.mark.parametrize("raw", ['{"max_daily_loss_usd": NaN}',
                                 '{"max_daily_loss_usd": Infinity}',
                                 '{"max_entries_per_day": -Infinity}',
                                 '{"trade_size": {"main": {"mode": "budget", "value": NaN}}}'])
def test_nan_and_infinity_are_refused(home, app, raw):
    write_cfg(home, BASE)
    c = app()
    status, text = c.raw("POST", "/api/v2/settings/limits", raw,
                         origin=f"http://127.0.0.1:{c.port}")
    assert status == 200 and json.loads(text)["ok"] is False
    assert read_cfg(home) == BASE


def test_a_partial_limits_save_changes_only_what_was_sent(home, app):
    start = {**BASE, "other_sizing_mode": "budget", "other_budget_per_trade_usd": 90.0,
             "max_open_positions": 4, "paused": True}
    write_cfg(home, start)
    c = app()
    r = c.confirmed("/api/v2/settings/limits", {"max_entries_per_day": 9})
    assert r["ok"] is True and r["saved"] is True
    assert read_cfg(home) == {**start, "max_entries_per_day": 9}
    r = c.confirmed("/api/v2/settings/limits",
                    {"trade_size": {"main": {"mode": "budget", "value": 600}}})
    assert read_cfg(home) == {**start, "max_entries_per_day": 9,
                              "budget_per_trade_usd": 600.0}
    # the reply carries the settings as saved, re-read from the server
    assert r["settings"]["limits"]["trade_size"]["other"] == {"mode": "budget", "value": 90.0}


@pytest.mark.parametrize("dry", [True, False, None])
def test_a_limits_save_never_changes_practice_or_live(home, app, dry):
    start = {k: v for k, v in BASE.items() if k != "dry_run"}
    if dry is not None:
        start["dry_run"] = dry
    write_cfg(home, start)
    c = app()
    c.confirmed("/api/v2/settings/limits", {
        "max_entries_per_day": 8, "max_daily_loss_usd": 900, "max_open_positions": 12,
        "trade_size": {"main": {"mode": "contracts", "value": 3},
                       "other": {"mode": "budget", "value": 40}}})
    after = read_cfg(home)
    assert after.get("dry_run", "missing") == (dry if dry is not None else "missing")
    assert after["max_entries_per_day"] == 8 and after["max_open_positions"] == 12


def test_nothing_to_change_saves_nothing(home, app):
    write_cfg(home, BASE)
    c = app()
    r = c.post("/api/v2/settings/limits", {"max_entries_per_day": 3})
    assert r == {"ok": True, "saved": False,
                 "note": "Nothing to change: those are your settings already."}


# ── the two-step confirmation ───────────────────────────────────────────────

def test_preview_saves_nothing_and_says_what_changes(home, app):
    write_cfg(home, BASE)
    c = app()
    r = c.post("/api/v2/settings/limits", {
        "max_entries_per_day": 2, "max_daily_loss_usd": 0,
        "trade_size": {"main": {"mode": "budget", "value": 1150}}})
    assert r["ok"] is True and r["needs_confirm"] is True
    p = r["preview"]
    assert p["expires_in_s"] == 120 and p["token"]
    by_key = {ch["key"]: ch for ch in p["changes"]}
    assert by_key["trade_size.main"] == {
        "key": "trade_size.main", "label": "Trade size (main pick)",
        "saved_now": {"mode": "budget", "value": 500.0},
        "new": {"mode": "budget", "value": 1150.0}, "direction": "raises_risk"}
    assert by_key["max_entries_per_day"]["direction"] == "lowers_risk"
    # 0 = no daily loss limit: that raises the risk
    assert by_key["max_daily_loss_usd"]["direction"] == "raises_risk"
    assert any("Practice and LIVE" in d for d in p["does_not"])
    assert read_cfg(home) == BASE


def _preview(c, data, path="/api/v2/settings/limits"):
    r = c.post(path, data)
    assert r["needs_confirm"] is True
    return r["preview"]["token"]


def test_wrong_or_reused_tokens_save_nothing(home, app):
    write_cfg(home, BASE)
    c = app()
    data = {"max_entries_per_day": 9}
    token = _preview(c, data)
    for bad in ("nope", "", None, 5, token[:-1] + ("0" if token[-1] != "0" else "1"),
                "9999999999.abc.def"):
        r = c.post("/api/v2/settings/limits", {**data, "confirm": bad})
        assert r["ok"] is False and read_cfg(home) == BASE
    # the tampered try above spent that token: a fresh one works once
    token = _preview(c, data)
    assert c.post("/api/v2/settings/limits", {**data, "confirm": token})["saved"] is True
    write_cfg(home, BASE)
    r = c.post("/api/v2/settings/limits", {**data, "confirm": token})
    assert r["ok"] is False and "already used" in r["error"]
    assert read_cfg(home) == BASE


def test_an_expired_token_saves_nothing(home, app, monkeypatch):
    write_cfg(home, BASE)
    c = app()
    data = {"max_entries_per_day": 9}
    token = _preview(c, data)
    real = webui._clock()
    monkeypatch.setattr(webui, "_clock", lambda: real + 121)
    r = c.post("/api/v2/settings/limits", {**data, "confirm": token})
    assert r["ok"] is False and "expired" in r["error"]
    assert read_cfg(home) == BASE


def test_a_token_only_confirms_the_payload_it_previewed(home, app):
    write_cfg(home, BASE)
    c = app()
    token = _preview(c, {"max_entries_per_day": 4})
    r = c.post("/api/v2/settings/limits", {"max_entries_per_day": 40, "confirm": token})
    assert r["ok"] is False and "different change" in r["error"]
    assert read_cfg(home) == BASE
    # nor another route's payload
    token = _preview(c, {"max_entries_per_day": 4})
    r = c.post("/api/v2/pass/buy", {"days": 4, "confirm": token})
    assert r["ok"] is False and read_cfg(home) == BASE


def test_settings_changed_after_the_preview_save_nothing(home, app):
    write_cfg(home, BASE)
    c = app()
    data = {"max_entries_per_day": 9}
    token = _preview(c, data)
    changed = {**BASE, "budget_per_trade_usd": 999.0}  # e.g. a chat command
    write_cfg(home, changed)
    r = c.post("/api/v2/settings/limits", {**data, "confirm": token})
    assert r == {"ok": False, "error": "Your settings changed since you "
                 "reviewed this. Review again."}
    assert read_cfg(home) == changed
    # ...and so does a new written policy
    token = _preview(c, data)
    (home / "policy.md").write_text("Only SPY.\n", encoding="utf-8")
    r = c.post("/api/v2/settings/limits", {**data, "confirm": token})
    assert r["ok"] is False and read_cfg(home) == changed


def test_going_live_needs_the_preview_the_token_and_the_tick(home, app):
    write_cfg(home, {**BASE, "max_open_positions": 6})
    view = {"broker": {"id": "alpaca", "name": "Alpaca", "account_mask": None,
                       "paper_account": True, "buying_power": 1234.5,
                       "buying_power_at": "2026-11-11T15:00:00Z"},
            "limits": {"cash": {"low": False}}, "real_positions": 2}
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}),
            get_view=lambda: view)
    data = {"action": "set_mode", "mode": "live"}
    r = c.post("/api/v2/command", data)
    p = r["preview"]
    assert r["needs_confirm"] is True and p["needs_acknowledge"] is True
    assert p["account"] == {"broker": "alpaca", "name": "Alpaca",
                            "account_mask": None, "paper_account": True}
    assert p["buying_power"] == 1234.5 and p["real_positions"] == 2
    assert p["trade_size"]["main"] == {"mode": "budget", "value": 500.0}
    assert p["limits"] == {"max_entries_per_day": 3, "max_daily_loss_usd": 200.0,
                           "max_open_positions": 6}
    assert read_cfg(home)["dry_run"] is True
    # confirmed but not ticked: nothing changes, and the token still works
    r2 = c.post("/api/v2/command", {**data, "confirm": p["token"]})
    assert r2["ok"] is False and r2["needs_acknowledge"] is True
    r2 = c.post("/api/v2/command", {**data, "confirm": p["token"], "acknowledge": "yes"})
    assert r2["ok"] is False and read_cfg(home)["dry_run"] is True
    r3 = c.post("/api/v2/command", {**data, "confirm": p["token"], "acknowledge": True})
    assert r3["ok"] is True and read_cfg(home)["dry_run"] is False
    # no token at all: never live
    write_cfg(home, BASE)
    r4 = c.post("/api/v2/command", {**data, "acknowledge": True})
    assert r4.get("needs_confirm") is True and read_cfg(home)["dry_run"] is True


def test_practice_and_pause_apply_at_once(home, app):
    write_cfg(home, {**BASE, "dry_run": False})
    c = app(lambda: {"mode": "LIVE", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    r = c.post("/api/v2/command", {"action": "set_mode", "mode": "practice"})
    assert r["ok"] is True and read_cfg(home)["dry_run"] is True
    r = c.post("/api/v2/command", {"action": "pause"})
    assert r["ok"] is True and read_cfg(home)["paused"] is True


# ── auth and secrets ────────────────────────────────────────────────────────

V2_POST = ("/api/v2/settings/limits", "/api/v2/settings/rules",
           "/api/v2/settings/alerts", "/api/v2/settings/computer",
           "/api/v2/settings/ai", "/api/v2/broker/connect", "/api/v2/pass/buy",
           "/api/v2/wallet/recurring", "/api/v2/command")


def test_every_new_route_needs_the_login_and_the_right_host_and_origin(home, app):
    write_cfg(home, BASE)
    c = app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
            agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    own = f"http://127.0.0.1:{c.port}"
    assert c.raw("GET", "/api/v2/settings", login=False)[0] == 401
    assert c.raw("GET", "/api/v2/settings", host="evil.example")[0] == 403
    body = json.dumps({"action": "pause", "max_entries_per_day": 9})
    for path in V2_POST:
        assert c.raw("POST", path, body, login=False, origin=own)[0] == 401, path
        assert c.raw("POST", path, body, host="evil.example", origin=own)[0] == 403, path
        assert c.raw("POST", path, body, origin="http://evil.example")[0] == 403, path
    assert read_cfg(home) == BASE


def test_settings_get_never_carries_a_secret(home, app):
    full = {**BASE, **SECRETS, "lnbits_url": "https://wallet.example/?usr=fakeuser123456",
            "robinhood_account": "5QR748219999"}
    write_cfg(home, full)
    c = app()
    s = c.get("/api/v2/settings")
    text = json.dumps(s)
    for name, value in SECRETS.items():
        assert value not in text, name
        assert value[:8] not in text, name
    assert "fakeuser123456" not in text
    assert s["alerts"]["discord"] == {"set": True, "last4": SECRETS[
        "discord_webhook_url"][-4:]}
    assert s["broker"]["fields"]["alpaca_secret"] == {"set": True, "last4": None}
    assert s["broker"]["fields"]["alpaca_paper"] is True
    assert s["ai"]["key"]["set"] is True and s["signals"]["api_key"]["set"] is True
    assert s["mode"] == {"mode": "practice", "explicit": True}
    assert s["limits"]["max_entries_per_day"] == 3
    assert isinstance(s["fingerprint"], str) and len(s["fingerprint"]) == 32
    # the fingerprint follows the settings
    write_cfg(home, {**full, "max_entries_per_day": 4})
    assert c.get("/api/v2/settings")["fingerprint"] != s["fingerprint"]


def test_saved_settings_round_trip_through_the_get(home, app):
    write_cfg(home, BASE)
    c = app()
    c.post("/api/v2/settings/rules", {"rule_tickers": ["SPY"], "rule_sides": "puts"})
    s = c.get("/api/v2/settings")
    assert s["rules"]["rule_tickers"] == ["SPY"] and s["rules"]["rule_sides"] == "puts"
    assert s["limits"]["trade_size"] == {"main": {"mode": "budget", "value": 500.0},
                                         "other": None}


# ── the setup wizard's way back to /v2 ──────────────────────────────────────

def test_setup_from_v2_links_back_to_v2(home, app):
    write_cfg(home, BASE)
    c = app()
    status, plain = c.raw("GET", "/setup?ui=classic")
    assert status == 200 and plain == webui._WIZARD_HTML
    status, page = c.raw("GET", "/setup?ui=classic&from=v2")
    assert status == 200
    assert 'href="/dash"' not in page and "location='/dash'" not in page
    assert page.count('href="/v2"') == 2 and "location='/v2'" in page
    # only the two allowed values; anything else is the wizard as it was
    for q in ("from=dash", "from=https://evil.example", "from=/v2", "from=v2&from=v2",
              "from=V2", "from="):
        assert c.raw("GET", f"/setup?ui=classic&{q}")[1] == webui._WIZARD_HTML, q
    # the classic dashboard page is never rewritten
    assert c.raw("GET", "/dash?ui=classic&from=v2")[1] == webui._DASH_HTML


def test_return_to_allow_list():
    assert webui._return_to("/setup?from=v2") == "/v2"
    assert webui._return_to("/setup?from=dash") == "/dash"
    for bad in ("/setup", "/setup?from=//evil", "/setup?from=v3", "/setup?from=v2&from=dash"):
        assert webui._return_to(bad) is None
