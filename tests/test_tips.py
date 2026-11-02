"""Dashboard tips: a fixed 100 / 500 / 1,000-sat invoice the owner pays from
their own wallet. The agent never pays a tip, sends nothing but the amount,
and only shows an invoice whose amount matches the tier picked."""

import http.client
import inspect
import threading

import pytest

import webui
from sources import agenthc_day_trade_ideas as hc

HASH = "ab" * 32


def _inv(amount_part, body_len=180):
    """A BOLT11-shaped mainnet invoice string with the given amount part."""
    return f"lnbc{amount_part}1p" + "q" * body_len


class _R:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeHttp:
    """Stands in for net_check.http: records every call, answers from a queue."""

    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._answer("get", url, kw)

    def post(self, url, **kw):
        return self._answer("post", url, kw)


@pytest.fixture
def fake_http(monkeypatch):
    def install(*responses):
        h = _FakeHttp(*responses)
        monkeypatch.setattr(hc, "_http", h)
        return h
    return install


def _created(n, amount_part, **extra):
    return {"payment_request": _inv(amount_part), "payment_hash": HASH,
            "amount_sats": n, "amount_usd_approx": 0.39,
            "invoice_valid_seconds": 3600, **extra}


# ── BOLT11 amount decoder ───────────────────────────────────────────────────

@pytest.mark.parametrize("amount_part, msat", [
    ("1u", 100_000), ("5u", 500_000), ("10u", 1_000_000), ("1000n", 100_000),
    ("1000000p", 100_000), ("1m", 100_000_000), ("2", 200_000_000_000),
])
def test_decoder_reads_the_amount(amount_part, msat):
    assert hc.bolt11_amount_msat(_inv(amount_part)) == msat


def test_decoder_ignores_case_and_whitespace():
    assert hc.bolt11_amount_msat("  " + _inv("5u").upper() + "\n") == 500_000


@pytest.mark.parametrize("invoice", [
    _inv(""),                                  # no amount
    "lntb5u1p" + "q" * 180,                    # testnet
    "lnbcrt5u1p" + "q" * 180,                  # regtest
    _inv("5x"),                                # bad multiplier
    _inv("15p"),                               # not a whole msat
    _inv("05u"),                               # leading zero
    "lnbc5u1pqqqq",                            # too short
    _inv("5u")[:-1] + "b",                     # 'b' isn't bech32
    _inv("5u") + "1",                          # nothing after the separator
    "lnbc5u" + "q" * 180,                      # no separator in the right place
    None, 500, b"lnbc5u1p",
])
def test_decoder_rejects_non_invoices(invoice):
    assert hc.bolt11_amount_msat(invoice) is None


# ── client: what is sent, what is accepted ──────────────────────────────────

@pytest.mark.parametrize("bad", [99, 1001, 0, -100, "100", True, 100.0, None, [100]])
def test_create_tip_rejects_other_amounts_without_a_request(fake_http, bad):
    h = fake_http()
    with pytest.raises(ValueError):
        hc.create_tip(bad)
    assert h.calls == []


@pytest.mark.parametrize("n, amount_part", [(100, "1u"), (500, "5u"), (1000, "10u")])
def test_create_tip_sends_only_the_amount(fake_http, n, amount_part):
    h = fake_http(_R(200, _created(n, amount_part)))
    body = hc.create_tip(n)
    assert body["amount_sats"] == n
    method, url, kw = h.calls[0]
    assert method == "post" and url == hc.AGENTHC_API + hc.TIP_PATH
    assert kw["json"] == {"amount_sats": n}
    assert set(kw) == {"json", "timeout"}  # no headers: no API key, no pass token


def test_create_tip_refuses_a_different_server_amount(fake_http):
    fake_http(_R(200, _created(1000, "5u")))  # body says 1000, invoice 500
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(500)
    assert e.value.code == "amount_mismatch" and str(e.value) == hc.TIP_MISMATCH


def test_create_tip_refuses_a_different_invoice_amount(fake_http):
    fake_http(_R(200, _created(500, "10u")))  # body says 500, invoice 1000
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(500)
    assert e.value.code == "amount_mismatch"


@pytest.mark.parametrize("change", [
    {"payment_request": _inv("")},             # amountless invoice
    {"payment_request": "lntb5u1p" + "q" * 180},
    {"payment_hash": "AB" * 32},               # not lowercase hex
    {"payment_hash": "ab" * 31},
    {"amount_sats": 500.0},
])
def test_create_tip_refuses_odd_answers(fake_http, change):
    fake_http(_R(200, {**_created(500, "5u"), **change}))
    with pytest.raises(hc.TipRefused):
        hc.create_tip(500)


@pytest.mark.parametrize("status", [404, 405])
def test_missing_tip_routes_mean_not_yet(fake_http, status):
    fake_http(_R(status), _R(status))
    with pytest.raises(hc.TipsUnavailable):
        hc.tip_options()
    with pytest.raises(hc.TipsUnavailable):
        hc.create_tip(100)


def test_capacity_refusal_asks_for_fresh_tiers(fake_http):
    msg = "Our Lightning node can't take this amount right now."
    fake_http(_R(503, {"detail": {"error": "tip_size_temporarily_unavailable",
                                  "message": msg}}))
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(1000)
    assert str(e.value) == msg and e.value.refresh is True
    assert e.value.code == "tip_size_temporarily_unavailable"


def test_rate_limit_refusal_keeps_the_tiers(fake_http):
    fake_http(_R(429, {"detail": {"error": "too_many_tips", "message": "Wait."}}))
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(100)
    assert e.value.refresh is False and str(e.value) == "Wait."


def test_options_keep_only_known_tiers(fake_http):
    fake_http(_R(200, {"enabled": True, "tiers": [
        {"amount_sats": 100, "available": True},
        {"amount_sats": 2000, "available": True},
        {"amount_sats": True, "available": True},
        "junk",
    ]}))
    assert [t["amount_sats"] for t in hc.tip_options()["tiers"]] == [100]


def test_tip_status_checks_the_id_before_asking(fake_http):
    h = fake_http()
    for bad in ("../x", "AB" * 32, "ab" * 31, "", None, HASH + "/../../x"):
        with pytest.raises(ValueError):
            hc.tip_status(bad)
    assert h.calls == []


def test_tip_status_answers(fake_http):
    h = fake_http(_R(404), _R(200, {"status": "paid"}), _R(503))
    assert hc.tip_status(HASH) == {"status": "expired"}
    assert hc.tip_status(HASH)["status"] == "paid"
    with pytest.raises(RuntimeError):
        hc.tip_status(HASH)
    assert h.calls[0][1] == f"{hc.AGENTHC_API}{hc.TIP_PATH}/{HASH}"
    assert all(set(kw) == {"timeout"} for _, _, kw in h.calls)


# ── local dashboard routes ──────────────────────────────────────────────────

def test_options_route_labels_tiers(monkeypatch):
    monkeypatch.setattr(hc, "tip_options", lambda: {
        "tiers": [
            {"amount_sats": 100, "available": True, "reason": None, "amount_usd_approx": 0.08},
            {"amount_sats": 1000, "available": False, "reason": "Our node is full.",
             "amount_usd_approx": None},
        ],
        "quote": {"btc_usd": 77700.0, "age_seconds": 412}, "invoice_valid_seconds": 3600})
    r = webui._tip_options_route(None)
    assert r["ok"] is True and r["quote_age_s"] == 412 and r["valid_seconds"] == 3600
    assert r["tiers"][0]["label"] == "100 sats (~$0.08)"
    assert r["tiers"][1] == {"amount_sats": 1000, "available": False,
                             "reason": "Our node is full.", "amount_usd_approx": None,
                             "label": "1,000 sats"}


def test_options_route_not_yet(monkeypatch):
    def gone():
        raise hc.TipsUnavailable(hc.TIPS_NOT_YET)
    monkeypatch.setattr(hc, "tip_options", gone)
    assert webui._tip_options_route(None) == {"ok": False, "unavailable": True,
                                              "error": hc.TIPS_NOT_YET}


@pytest.mark.parametrize("bad", [99, "100", True, 100.0, None])
def test_create_route_rejects_other_amounts(monkeypatch, bad):
    called = []
    monkeypatch.setattr(hc, "create_tip", lambda n: called.append(n))
    r = webui._tip_create(None, {"amount_sats": bad})
    assert r == {"ok": False, "error": "Pick one of the tip amounts shown."}
    assert called == []


def test_create_route_returns_the_invoice(monkeypatch):
    monkeypatch.setattr(hc, "create_tip", lambda n: _created(n, "5u"))
    monkeypatch.setattr(webui, "_qr_matrix", lambda data: [[1, 0], [0, 1]])
    r = webui._tip_create(None, {"amount_sats": 500})
    assert r == {"ok": True, "bolt11": _inv("5u"), "qr": [[1, 0], [0, 1]],
                 "payment_hash": HASH, "amount_sats": 500,
                 "amount_usd_approx": 0.39, "expires_in": 3600}


def test_create_route_passes_refusals_on(monkeypatch):
    def refuse(n):
        raise hc.TipRefused("Too many unpaid tip invoices right now.",
                            code="tips_busy", refresh=True)
    monkeypatch.setattr(hc, "create_tip", refuse)
    r = webui._tip_create(None, {"amount_sats": 100})
    assert r == {"ok": False, "error": "Too many unpaid tip invoices right now.",
                 "refresh_tiers": True}

    def gone(n):
        raise hc.TipsUnavailable(hc.TIPS_NOT_YET)
    monkeypatch.setattr(hc, "create_tip", gone)
    assert webui._tip_create(None, {"amount_sats": 100})["unavailable"] is True


def test_status_route(monkeypatch):
    seen = []
    monkeypatch.setattr(hc, "tip_status", lambda h: seen.append(h) or {"status": "paid"})
    assert webui._tip_status_route(None, {"payment_hash": "nope"}) == {
        "ok": False, "error": "That invoice id doesn't look right."}
    assert seen == []
    assert webui._tip_status_route(None, {"payment_hash": HASH}) == {"ok": True, "status": "paid"}

    def down(h):
        raise RuntimeError("status_unavailable")
    monkeypatch.setattr(hc, "tip_status", down)
    r = webui._tip_status_route(None, {"payment_hash": HASH})
    assert r["ok"] is False and r["retry"] is True


def test_tip_routes_are_registered():
    src = inspect.getsource(webui.start_app)
    assert '"/api/tip/options": _tip_options_route' in src
    assert '"/api/tip/create": _tip_create' in src
    assert '"/api/tip/status": _tip_status_route' in src


def test_tip_code_never_pays_or_saves():
    """The money rule: no tip code path can pay anything or write anywhere."""
    fns = (webui._tip_label, webui._tip_options_route, webui._tip_create,
           webui._tip_status_route, hc.tip_options, hc.create_tip, hc.tip_status,
           hc.bolt11_amount_msat, hc._is_tier, hc._detail_message)
    for fn in fns:
        src = inspect.getsource(fn)
        for banned in ("lightning_wallet", "wallet_from_cfg", "pay_invoice", "pay(",
                       "trades.jsonl", "cfg[", "cfg.get", "save(", "state[",
                       "headers=", "open("):
            assert banned not in src, (fn.__name__, banned)


# ── the Host/Origin guard and the login cookie cover the tip routes ─────────

@pytest.fixture
def tip_server(home, monkeypatch):
    monkeypatch.setattr(hc, "create_tip", lambda n: _created(n, "1u"))
    monkeypatch.setattr(webui, "_qr_matrix", lambda data: None)

    class H(webui._Handler):
        pages = {}
        routes_get = {"/api/tip/options": webui._tip_options_route}
        routes_post = {"/api/tip/create": webui._tip_create,
                       "/api/tip/status": webui._tip_status_route}
        landing = "/dash"

    srv = webui._AppServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    old = (webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS)
    webui._ALLOWED_HOSTS = hosts
    webui._ALLOWED_ORIGINS = tuple(f"http://{h}" for h in hosts)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()
    srv.server_close()
    webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS = old


def _post(port, path, body, cookie=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    c.request("POST", path, body=body, headers=headers)
    r = c.getresponse()
    out = (r.status, r.read().decode())
    c.close()
    return out


def test_tip_create_needs_login_and_same_origin(tip_server):
    cookie = f"{webui._SESSION_COOKIE}={webui.ui_token()}"
    body = '{"amount_sats": 100}'
    assert _post(tip_server, "/api/tip/create", body)[0] == 401
    assert _post(tip_server, "/api/tip/create", body, cookie=cookie,
                 origin="http://evil.example.com")[0] == 403
    status, text = _post(tip_server, "/api/tip/create", body, cookie=cookie,
                         origin=f"http://127.0.0.1:{tip_server}")
    assert status == 200 and '"ok": true' in text and HASH in text
