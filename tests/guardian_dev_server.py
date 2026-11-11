"""Development only: serve the built /v2 dashboard against made-up states.

    python tests/guardian_dev_server.py [port]        (default 8799)
    open http://127.0.0.1:8799/v2?state=live
         http://127.0.0.1:8799/v2?state=broker-disconnected#/holdings

No agent, broker, wallet or network is involved, and nothing here is part of
the product: the dashboard bundle has no demo switch, so the state is chosen
by this server (it remembers `?state=` in a cookie for that browser tab).
Every state is a real view-model built by view_model.build_status from the
inputs in tests/guardian_states.py.

Besides those states:
    fetch-failed    answers once (as `live`), then fails every request:
                    the page must turn grey and say "as of"
    never-loaded    fails from the first request
    locked          answers 401, like an expired dashboard login

The Pause / Resume / Switch / Stop / Start buttons work: POST
/api/v2/command and /api/v2/agent/start answer the way the agent does and
move this tab to the matching state (Stop and a LIVE Start preview first).
The move dialog's file and finish answer in the server's shapes too. The settings forms
work too, against a made-up copy of the state's settings kept per tab:
GET /api/v2/settings and the /api/v2/... writes answer in the server's
shapes (a preview with a token first for money-affecting ones), but nothing
here is the real server's code: tests/test_v2_settings_api.py tests that.
"""

import json
import os
import re
import secrets
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import guardian_states as gs  # noqa: E402

DIST = os.path.join(ROOT, "webui_dist")
SPECIAL = ("fetch-failed", "never-loaded", "locked")
TYPES = {".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
         ".woff": "font/woff"}
# command -> ({state it applies in: state it leads to}, the agent's reply)
MOVES = {
    "pause": ({"live": "paused-new-trades",
               "practice": "practice-paused-new-trades"},
              "Paused — no new entries. Exit signals, the expiry guard and "
              "the broker checks keep running for positions you already "
              "have. The agent stays paused, also after a restart, until you "
              "say 'resume'."),
    "resume": ({"paused-new-trades": "live",
                "practice-paused-new-trades": "practice",
                "state-hold": "live"},
               "Resumed — the agent takes new entries again."),
    "dry on": ({"live": "practice", "paused-new-trades":
                "practice-paused-new-trades"},
               "Dry-run ON — You have 5 real positions open. I will still "
               "sell those when their exit signal arrives, or on their "
               "expiry day if still open then (expiry guard). No new real "
               "trades will be placed."),
    "dry off": ({"practice": "live", "practice-paused-new-trades":
                 "paused-new-trades", "empty-first-run": "empty-live"},
                "Dry-run OFF — the agent will place REAL orders from now on."),
    "stop": ({name: "stopped" for name in gs.STATES},
             "Stopping after this cycle. Open positions remain in your "
             "brokerage account — close them there if needed."),
}

_lock = threading.Lock()
_tabs = {}  # tab id -> {"state": name, "served": n, "cfg": {...}, "tokens": set()}

_SECRET = {"set": True, "last4": None}
_ALPACA = {"id": "alpaca", "name": "Alpaca (paper or live)", "fields": [
    {"id": "alpaca_key_id", "label": "API Key ID", "type": "text"},
    {"id": "alpaca_secret", "label": "API Secret", "type": "password"},
    {"id": "alpaca_paper", "label": "Paper trading (recommended)",
     "type": "checkbox", "default": True}]}
# /api/v2/command action -> the classic command it stands for (MOVES)
_V2_COMMAND = {"pause": "pause", "resume": "resume", "practice": "dry on",
               "live": "dry off", "stop": "stop"}


def _size(cfg, prefix):
    mode = cfg.get(prefix + "sizing_mode")
    key = {"budget": "budget_per_trade_usd", "contracts": "contracts_per_trade",
           "percent": "percent_per_trade"}.get(mode)
    return {"mode": mode, "value": cfg.get(prefix + key)} if key else None


def _sell(cfg):
    v = cfg.get("expiry_guard_et", "15:45")
    return None if v == "off" else v


def _settings(tab):
    """A made-up GET /api/v2/settings for the tab's state (same shape)."""
    cfg = tab["cfg"]
    return {
        "ok": True, "fingerprint": f"{abs(hash(json.dumps(cfg, sort_keys=True, default=str))):032x}"[:32],
        "settings_readable": True, "agent_running": tab["state"] != "stopped",
        "mode": {"mode": "practice" if cfg.get("dry_run") else "live", "explicit": True},
        "paused": bool(cfg.get("paused")),
        "limits": {"trade_size": {"main": _size(cfg, ""), "other": _size(cfg, "other_")},
                   "max_entries_per_day": cfg.get("max_entries_per_day"),
                   "max_daily_loss_usd": cfg.get("max_daily_loss_usd"),
                   "max_open_positions": cfg.get("max_open_positions"),
                   "defaults": {"max_entries_per_day": 5, "max_open_positions": 10},
                   "allowed": {"max_entries_per_day": [1, 1000],
                               "max_daily_loss_usd": [0, 1000000000],
                               "max_open_positions": [1, 1000]}},
        "rules": {**{k: cfg.get(k) for k in (
            "rule_tickers", "rule_skip_tickers", "rule_sides", "rule_min_strike",
            "rule_skip_0dte", "rule_loss_streak")},
            "summary": "", "policy": cfg.get("_policy", ""),
            "policy_blocks_entries": False},
        "alerts": {k: ({"set": True, "last4": "7777"} if cfg.get("_alert_" + k)
                       else {"set": False, "last4": None})
                   for k in ("discord", "ntfy", "tg_token", "tg_chat")},
        "computer": {"autostart": {"supported": True, "enabled": bool(cfg.get("_autostart")),
                                   "how": "a login item"},
                     "keep_awake": cfg.get("keep_awake", True) is not False,
                     "open_dashboard_at_login": cfg.get("open_dashboard_at_login", True) is not False,
                     "dashboard": cfg.get("dashboard", "classic")},
        "expiry": {"sell_et": _sell(cfg), "alert_et": "15:50",
                   "effective_today": {"sell_et": _sell(cfg), "alert_et": "15:50",
                                       "close_et": "16:00", "early_close": False},
                   "described": "sells at 15:45 ET, alert at 15:50 ET",
                   "allowed": ["09:30", "15:59"]},
        "code": {"self_edit_enabled": bool(cfg.get("self_edit_enabled"))},
        "consent": {"accepted": True, "on_file": True,
                    "accepted_at": "2026-10-01T12:00:00+00:00",
                    "terms_version": "fixture", "current_terms_version": "fixture"},
        "ai": {"connected": bool(cfg.get("_ai")), "provider": cfg.get("_ai"),
               "name": "Anthropic" if cfg.get("_ai") else None,
               "model": cfg.get("_ai_model"), "endpoint": None,
               "key": _SECRET if cfg.get("_ai") else {"set": False, "last4": None}},
        "broker": {"id": cfg.get("broker"), "ready": True,
                   "account_mask": (cfg.get("robinhood_account") or "")[-4:] or None,
                   "paper": bool(cfg.get("alpaca_paper")) if cfg.get("broker") == "alpaca" else False,
                   "fields": {},
                   "signin": {"seq": 0, "connected": None, "account_mask": None,
                              "options_off": False, "error": None},
                   "warning": None},
        "signals": {"source": cfg.get("source"),
                    "include_other_trades": bool(cfg.get("include_other_trades")),
                    "dry_run_preview": cfg.get("dry_run_preview", True) is not False,
                    "recurring": bool(cfg.get("day_pass_recurring")),
                    "wallet_connected": bool(cfg.get("lnbits_url")),
                    "source_site": None,
                    "wallet": {"site": "demo.lnbits.com" if cfg.get("lnbits_url") else None,
                               "warning": None, "page_on_file": bool(cfg.get("lnbits_url")),
                               "autopay_cap_sats": 30000, "invoice_cap_sats": None,
                               "new_site": "demo.lnbits.com", "new_warning": None},
                    "api_key": {"set": False, "last4": None}, "pass_on_file": True,
                    # the fixture's invoice is never paid: it stays waiting
                    "purchases": {"paid": [], "waiting": ["fixture-purchase"]}},
    }


def _preview(tab, changes, does_not, extra=None):
    token = secrets.token_hex(8)
    tab["tokens"].add(token)
    return {"ok": True, "needs_confirm": True, "preview": {
        "changes": changes, "does_not": does_not, **(extra or {}),
        "token": token, "expires_in_s": 120}}


def _risk(old, new, unlimited=False):
    eff = (lambda v: float("inf") if unlimited and not v else v)
    a, b = eff(old), eff(new)
    if a is None or b is None or a == b:
        return "neutral"
    return "raises_risk" if b > a else "lowers_risk"


_NOT = ["It does not switch between Practice and LIVE.",
        "It does not buy or sell anything."]


def _v2_post(tab, path, data):
    """The /api/v2/... writes, in the server's shapes (development only)."""
    cfg = tab["cfg"]
    token = data.pop("confirm", None)
    ack = data.pop("acknowledge", None)
    if token is not None and token not in tab["tokens"]:
        return {"ok": False, "error": "That confirmation has expired or was "
                "already used. Nothing was changed. Review again."}
    tab["tokens"].discard(token)
    if path == "/api/v2/command":
        which = data.get("mode") if data.get("action") == "set_mode" else data.get("action")
        if which == "live" and token is None:
            b = gs.status(tab["state"])["broker"]
            return _preview(tab, [{"key": "mode", "label": "Mode", "saved_now": "practice",
                                   "new": "live", "direction": "raises_risk"}],
                            ["It does not buy or sell anything by itself.",
                             "It does not change your limits or trade size."],
                            {"needs_acknowledge": True,
                             "account": {"broker": b["id"], "name": b["name"],
                                         "account_mask": b["account_mask"],
                                         "paper_account": b["paper_account"]},
                             "buying_power": b["buying_power"],
                             "buying_power_at": b["buying_power_at"],
                             "trade_size": {"main": _size(cfg, ""), "other": _size(cfg, "other_")},
                             "limits": {k: cfg.get(k) for k in ("max_entries_per_day",
                                        "max_daily_loss_usd", "max_open_positions")},
                             "real_positions": gs.status(tab["state"])["real_positions"],
                             "cash_low": False})
        if which == "stop" and token is None:
            st = gs.status(tab["state"])
            left = [{"id": x["id"], "label": x["label"], "contract": x["contract"],
                     "qty": x["qty"], "expires_today": x["expires_today"]}
                    for x in st["positions"] if x["real"]]
            return _preview(tab, [{"key": "agent", "label": "Agent on this computer",
                                   "saved_now": "running", "new": "stopped",
                                   "direction": "raises_risk" if left else "neutral"}],
                            ["It does not sell anything on the way out: open "
                             "positions stay open at your broker."],
                            {"unsold": left, "real_positions": len(left)})
        if which == "live" and ack is not True:
            return {"ok": False, "needs_acknowledge": True,
                    "error": "Tick the box to confirm you want real orders. Nothing was changed."}
        moves, reply = MOVES.get(_V2_COMMAND.get(which, ""), ({}, None))
        nxt = moves.get(tab["state"])
        if not nxt:
            return {"ok": False, "error": "(fixture server) nothing to move to from here"}
        tab["state"] = nxt
        cfg["dry_run"] = which == "practice" if which in ("live", "practice") else cfg.get("dry_run")
        return {"ok": True, "reply": reply}
    if path == "/api/v2/settings/limits":
        new = dict(cfg)
        changes = []
        for k, label, unl in (("max_entries_per_day", "New trades per day", False),
                              ("max_daily_loss_usd", "Daily loss limit", True),
                              ("max_open_positions", "Positions open at once", False)):
            if k in data and data[k] != cfg.get(k):
                new[k] = data[k]
                changes.append({"key": k, "label": label, "saved_now": cfg.get(k),
                                "new": data[k], "direction": _risk(cfg.get(k), data[k], unl)})
        if "expiry_sell_et" in data and data["expiry_sell_et"] != (cfg.get("expiry_guard_et") or "15:45"):
            new["expiry_guard_et"] = data["expiry_sell_et"]
            before, after = _sell(cfg), _sell(new)
            changes.append({"key": "expiry_sell_et", "label": "Expiry-day sell time",
                            "saved_now": before, "new": after,
                            "direction": "raises_risk" if after is None or (before and after > before)
                            else "lowers_risk"})
        for book, prefix in (("main", ""), ("other", "other_")):
            spec = (data.get("trade_size") or {}).get(book)
            if spec is None:
                continue
            before = _size(cfg, prefix)
            if spec.get("mode") == "same":
                new[prefix + "sizing_mode"] = ""
            else:
                new[prefix + "sizing_mode"] = spec["mode"]
                new[prefix + {"budget": "budget_per_trade_usd", "contracts": "contracts_per_trade",
                              "percent": "percent_per_trade"}[spec["mode"]]] = spec["value"]
            after = _size(new, prefix)
            same_mode = before and after and before["mode"] == after["mode"]
            changes.append({"key": f"trade_size.{book}", "label": f"Trade size ({book})",
                            "saved_now": before, "new": after,
                            "direction": _risk(before["value"], after["value"]) if same_mode else "neutral"})
        if not changes:
            return {"ok": True, "saved": False, "note": "Nothing to change: those are your settings already."}
        if token is None:
            return _preview(tab, changes, _NOT)
        cfg.clear()
        cfg.update(new)
        return {"ok": True, "saved": True}
    if path == "/api/v2/settings/rules":
        for k, v in data.items():
            cfg["_policy" if k == "policy" else k] = v or ""
        return {"ok": True, "saved": True}
    if path == "/api/v2/settings/alerts":
        for k, v in data.items():
            cfg["_alert_" + k] = v
        return {"ok": True, "saved": True}
    if path == "/api/v2/settings/computer":
        if "autostart" in data:
            cfg["_autostart"] = data["autostart"]
        for k in ("keep_awake", "open_dashboard_at_login", "dashboard"):
            if k in data:
                cfg[k] = data[k]
        return {"ok": True, "saved": True, "autostart_note": ""}
    if path == "/api/v2/agent/start":
        if tab["state"] != "stopped":
            return {"ok": False, "error": "The agent is already running. Nothing was changed."}
        live = not cfg.get("dry_run")
        if live and token is None:
            return _preview(tab, [{"key": "agent", "label": "Agent on this computer",
                                   "saved_now": "stopped", "new": "running",
                                   "direction": "raises_risk"}],
                            ["It does not change a setting: it runs with them as saved."],
                            {"mode": "live", "trade_size": {"main": _size(cfg, "")},
                             "limits": {k: cfg.get(k) for k in ("max_entries_per_day",
                                        "max_daily_loss_usd", "max_open_positions")},
                             "real_positions": gs.status(tab["state"])["real_positions"]})
        tab["state"] = "live" if live else "practice"
        return {"ok": True, "autostart_note": ""}
    if path == "/api/v2/move/finish":
        if token is None:
            return _preview(tab, [{"key": "this_computer", "label": "This computer",
                                   "saved_now": "runs the agent", "new": "retired",
                                   "direction": "neutral"}],
                            ["It does not sell anything."],
                            {"unsold": [], "real_positions": 0, "agent_running": True})
        tab["state"] = "stopped"
        return {"ok": True, "note": "(fixture server) this computer is retired"}
    if path == "/api/v2/settings/ai":
        cfg["_ai"], cfg["_ai_model"] = data.get("provider") or "anthropic", data.get("model")
        return {"ok": True, "saved": True, "describe": "Anthropic"}
    if path in ("/api/v2/broker/connect", "/api/v2/broker/robinhood"):
        rh = path.endswith("robinhood")
        if token is None:
            paper = None if rh else bool((data.get("values") or {}).get("alpaca_paper"))
            return _preview(tab, [{"key": "broker", "label": "Broker account",
                                   "saved_now": {"id": cfg.get("broker"), "paper": False},
                                   "new": {"id": "robinhood" if rh else data.get("broker"),
                                           "paper": False if rh else paper},
                                   "direction": "neutral" if rh else ("lowers_risk" if paper else "neutral")}], _NOT)
        if rh:
            return {"ok": True, "url": "https://robinhood.example/fixture-sign-in",
                    "signin_seq": 0}
        cfg["broker"] = data.get("broker")
        cfg["alpaca_paper"] = bool((data.get("values") or {}).get("alpaca_paper"))
        return {"ok": True, "saved": True, "note": "connected (fixture)"}
    if path == "/api/v2/pass/buy":
        if token is None:
            return _preview(tab, [{"key": "signal_days", "label": "Signal days to buy",
                                   "saved_now": None, "new": data.get("days"),
                                   "direction": "neutral"}],
                            ["Nothing is paid yet: the next step shows a Lightning invoice "
                             "with its price, and nothing is paid until you pay it from your wallet."] + _NOT)
        qr = [[(x * y + x + y) % 3 == 0 for x in range(25)] for y in range(25)]
        return {"ok": True, "bolt11": "lnbc" + "fixture" * 30, "purchase": "fixture-purchase",
                "amount_sats": 10200 * data["days"],
                "amount_usd": 10 * data["days"], "extends": False, "carried": 0,
                "access_until": None, "qr": [[int(v) for v in r] for r in qr]}
    if path == "/api/v2/ask":
        # answer-only, as the agent's own route: never a command
        q = str(data.get("question") or "").strip()
        if not q:
            return {"ok": False, "error": "Type a question first."}
        return {"ok": True, "answer": "(fixture server) The agent would answer here from "
                f"its status and recent trades. You asked: {q[:200]}"}
    if path == "/api/v2/settings/code":
        cfg["self_edit_enabled"] = bool(data.get("self_edit_enabled"))
        return {"ok": True, "saved": True}
    if path == "/api/v2/proposal/request":
        tab["proposal"] = True
        return {"ok": True, "note": "(fixture server) Proposal ready: 1 file(s) changed."}
    if path == "/api/v2/proposal/apply":
        if token is None:
            return _preview(tab, [{"key": "code", "label": "The agent's code",
                                   "saved_now": "as it is now", "new": "1 file changed by the AI helper",
                                   "direction": "neutral"}], _NOT,
                            {"files": ["brokers/example.py"], "warnings": [], "restarts": True,
                             "diff_sha256": data.get("diff_sha256")})
        tab["proposal"] = False
        return {"ok": True, "reply": "(fixture server) Applied 1 file(s). Restarting."}
    if path in ("/api/v2/settings/source", "/api/v2/settings/signals", "/api/v2/wallet/create",
                "/api/v2/wallet/fund", "/api/v2/wallet/connect", "/api/v2/wallet/recurring"):
        direct = (path.endswith("source") and "agenthc_key" in data) or (
            path.endswith("signals") and set(data) - {"confirm"} == {"dry_run_preview"})
        if token is None and not direct:
            return _preview(tab, [{"key": k, "label": k.replace("_", " ").capitalize(),
                                   "saved_now": None, "new": v, "direction": "neutral"}
                                  for k, v in data.items()] or [
                                      {"key": "wallet", "label": "Payment wallet", "saved_now": None,
                                       "new": {"site": "demo.lnbits.com", "new": True},
                                       "direction": "neutral"}], _NOT)
        if path.endswith("signals"):
            if "scope" in data:
                cfg["include_other_trades"] = data["scope"] == "all"
            if "dry_run_preview" in data:
                cfg["dry_run_preview"] = data["dry_run_preview"]
        elif path.endswith("source") and "source" in data:
            cfg["source"] = data["source"]
        elif path.endswith("recurring"):
            cfg["day_pass_recurring"] = bool(data.get("on"))
        elif path.endswith("create") or path.endswith("connect"):
            cfg["lnbits_url"] = "https://demo.lnbits.com"
        elif path.endswith("fund"):
            return {"ok": True, "bolt11": "lnbc" + "fixturefund" * 20, "sats": data.get("sats"),
                    "note": "", "qr": None}
        return {"ok": True, "saved": True}
    if path == "/api/v2/wallet/page":
        return {"ok": True, "url": "https://demo.lnbits.com/wallet?usr=fixture"}
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _tab(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
        except Exception:
            return None
        m = c.get("gtab")
        with _lock:
            return _tabs.get(m.value) if m else None

    def _send(self, body, ctype="application/json", status=200, extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/dash", "/setup"):
            names = "".join(f'<li><a href="/v2?state={n}">{n}</a></li>'
                            for n in list(gs.STATES) + list(SPECIAL))
            return self._send(
                ("<!doctype html><meta charset=utf-8><title>fixture server"
                 "</title><h1>Fixture server (development only)</h1><p>The "
                 "real app serves the classic dashboard and the settings "
                 f"here. States:</p><ul>{names}</ul>").encode(),
                "text/html; charset=utf-8")
        if u.path == "/v2":
            name = (qs.get("state") or ["practice"])[0]
            if name not in gs.STATES and name not in SPECIAL:
                return self._send({"error": f"no state {name!r}"}, status=404)
            tab = secrets.token_hex(8)
            cfg = dict(gs.STATES[name][0]) if name in gs.STATES else {}
            with _lock:
                _tabs[tab] = {"state": name, "served": 0, "cfg": cfg, "tokens": set()}
            with open(os.path.join(DIST, "v2.html"), "rb") as f:
                return self._send(f.read(), "text/html; charset=utf-8",
                                  extra=[("Set-Cookie", f"gtab={tab}; Path=/; "
                                          "SameSite=Lax")])
        if u.path.startswith("/assets/"):
            name = u.path[len("/assets/"):]
            ext = os.path.splitext(name)[1]
            full = os.path.join(DIST, "assets", name)
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                    or ext not in TYPES or not os.path.isfile(full)):
                return self._send({"error": "not found"}, status=404)
            with open(full, "rb") as f:
                return self._send(f.read(), TYPES[ext])
        tab = self._tab()
        if tab is None:
            return self._send({"error": "open /v2?state=<name> first"},
                              status=401)
        name = tab["state"]
        if name == "locked":
            return self._send({"error": "locked"}, status=401)
        if u.path == "/api/v2/status":
            with _lock:
                tab["served"] += 1
                served = tab["served"]
            if name == "never-loaded" or (name == "fetch-failed" and served > 1):
                return self._send({"ok": False, "error": "fixture: failing"},
                                  status=503)
            return self._send(gs.status("live" if name == "fetch-failed"
                                        else name))
        fixed = {"/api/v2/settings": lambda: _settings(tab),
                 "/api/brokers": lambda: {"brokers": [_ALPACA]},
                 "/api/llm/providers": lambda: {"providers": {"anthropic": {
                     "name": "Anthropic", "base_url": "https://api.anthropic.com",
                     "keys_url": "console.anthropic.com"}}},
                 "/api/pass/options": lambda: {"options": [1, 5, 20, 60]},
                 "/api/pass/status": lambda: {"status": "pending"},
                 "/api/wallet/balance": lambda: (
                     {"ok": True, "sats": 41200} if tab["cfg"].get("lnbits_url")
                     else {"ok": False, "error": "no wallet"}),
                 "/api/wallet/quote": lambda: {"ok": True, "btc_usd": 97000.0, "usd": 0.0,
                                               "day_passes": 0, "day_pass_sats": 10400,
                                               "suggested_sats": 10400, "invoice_cap": None},
                 "/api/proposal": lambda: (
                     {"pending": True, "explanation": "(fixture) Adds a connector for another broker.",
                      "warnings": [], "diff": "--- a/brokers/example.py\n+++ b/brokers/example.py\n"
                                              "@@ -0,0 +1 @@\n+x = 1\n"}
                     if tab.get("proposal") or name == "proposal-pending" else {"pending": False}),
                 "/api/help/netcheck": lambda: {"ok": True, "text": "AgentHC: OK (fixture)\nGitHub (updates): OK (fixture)"},
                 "/api/help/report": lambda: {"ok": True, "text": "agentic-trader support report\n(fixture)"},
                 "/api/v2/agreement": lambda: {"ok": True, "text": "(fixture) The agreement text.", "terms_version": "fixture"},
                 "/api/tip/options": lambda: {"ok": True, "valid_seconds": 3600,
                                              "quote": {"btc_usd": 97000.0, "age_s": 30, "source": "fixture"},
                                              "tiers": [{"amount_usd": n, "amount_sats": n * 1031, "available": True,
                                                         "reason": None, "label": f"${n}"} for n in (100, 500, 1000)]}}
        if u.path in fixed:
            return self._send(fixed[u.path]())
        if u.path == "/api/v2/activity":
            if name in SPECIAL:
                return self._send({"ok": False}, status=503)
            return self._send(gs.activity(
                name, before=(qs.get("before") or [None])[0],
                limit=(qs.get("limit") or [None])[0]))
        return self._send({"error": "not found"}, status=404)

    def do_POST(self):
        tab = self._tab()
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            data = {}
        path = urlparse(self.path).path
        if tab is None:
            return self._send({"error": "not found"}, status=404)
        if path == "/api/move/export":
            return self._send({"ok": True, "filename": "agentic-trader-fixture.atmove", "kb": 12})
        if path == "/api/proposal/reject":
            tab["proposal"] = False
            return self._send({"ok": True, "reply": "proposal rejected"})
        if path == "/api/tip/create":
            return self._send({"ok": True, "bolt11": "lnbc" + "fixturetip" * 20, "qr": None,
                               "payment_hash": "a" * 64, "amount_usd": data.get("amount_usd"),
                               "amount_sats": (data.get("amount_usd") or 0) * 1031,
                               "btc_usd": 97000.0, "expires_in": 3600})
        if path == "/api/tip/status":
            return self._send({"ok": True, "status": "pending"})
        if path == "/api/llm/models":
            return self._send({"ok": True, "provider": "anthropic",
                               "models": ["claude-a", "claude-b"], "suggested": "claude-a"})
        if path.startswith("/api/v2/"):
            with _lock:
                out = _v2_post(tab, path, dict(data))
            return self._send(out if out is not None else {"error": "not found"},
                              status=200 if out is not None else 404)
        if path != "/api/command":
            return self._send({"error": "not found"}, status=404)
        text = str(data.get("text", "")).strip().lower()
        moves, reply = MOVES.get(text, ({}, "(fixture server) not a command"))
        with _lock:
            nxt = moves.get(tab["state"])
            if nxt:
                tab["state"] = nxt
        if text == "dry off" and tab["state"] == "pass-exits-only":
            reply = ("Not going live: your written rules need an AI helper, "
                     "and none is connected. (Say 'policy off' to remove it.)")
        return self._send({"reply": reply})


def serve(port=8799):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    srv = serve(port)
    print(f"fixture server (development only): http://127.0.0.1:"
          f"{srv.server_address[1]}/  pid {os.getpid()}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
