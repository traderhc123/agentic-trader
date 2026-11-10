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

The Pause / Resume / Switch buttons work: POST /api/command answers the way
the agent does and moves this tab to the matching state.
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
_tabs = {}  # tab id -> {"state": name, "served": status answers so far}


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
            with _lock:
                _tabs[tab] = {"state": name, "served": 0}
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
        if urlparse(self.path).path != "/api/command" or tab is None:
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
