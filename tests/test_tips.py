"""Dashboard tips: $100 / $500 / $1,000 paid in sats at the live Bitcoin
price, as an invoice the owner pays from their own wallet. AgentHC prices
the invoice; the agent never pays a tip, sends nothing but the $ tier, and
only shows an invoice whose BOLT11 amount is exactly its amount_sats and
whose amount_sats matches the tier at the price it came with."""

import http.client
import inspect
import math
import threading

import pytest

import webui
from sources import agenthc_day_trade_ideas as hc

HASH = "ab" * 32
BTC = 77700.0
# What AgentHC charges at 77,700.00: ceil(usd * 1e8 / btc_usd).
SATS = {100: 128701, 500: 643501, 1000: 1287002}
QUOTE = {"btc_usd": BTC, "age_seconds": 412, "source": "fmp"}
REASON_PRICE = "Can't get a current Bitcoin price right now. Try again in a minute."
REASON_CAPACITY = ("Our Lightning node can't receive this much in one payment "
                   "right now. Try a smaller tip or check back later.")


def _inv(amount_part, body_len=180):
    """A BOLT11-shaped mainnet invoice string with the given amount part."""
    return f"lnbc{amount_part}1p" + "q" * body_len


def _sinv(sats):
    """A BOLT11-shaped invoice for exactly `sats` (1n = 100 msat = 0.1 sat)."""
    return _inv(f"{sats * 10}n")


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


@pytest.fixture(autouse=True)
def _tip_state(monkeypatch):
    """Every tier offered, and an independent BTC price that agrees with
    AgentHC's (no network)."""
    monkeypatch.setitem(webui._tip_offered, "usd", (100, 500, 1000))
    monkeypatch.setattr(webui, "_btc_usd", lambda: BTC)


def _created(n, sats=None, invoice=None, **extra):
    sats = SATS[n] if sats is None else sats
    return {"payment_request": invoice or _sinv(sats), "payment_hash": HASH,
            "amount_usd": n, "amount_sats": sats, "btc_usd": BTC,
            "quote": dict(QUOTE), "invoice_valid_seconds": 3600,
            "memo": f"AgentHC tip - thank you (${n:,})", **extra}


def _options(tiers=None, quote=QUOTE):
    if tiers is None:
        tiers = [{"amount_usd": 100, "amount_sats": SATS[100], "available": True, "reason": None},
                 {"amount_usd": 500, "amount_sats": SATS[500], "available": True, "reason": None},
                 {"amount_usd": 1000, "amount_sats": SATS[1000], "available": False,
                  "reason": REASON_CAPACITY}]
    return {"enabled": True, "currency": "USD", "tiers": tiers,
            "quote": dict(quote) if quote else None, "quote_max_age_seconds": 900,
            "invoice_valid_seconds": 3600}


# ── BOLT11 amount decoder ───────────────────────────────────────────────────

@pytest.mark.parametrize("amount_part, msat", [
    ("1u", 100_000), ("5u", 500_000), ("10u", 1_000_000), ("1000n", 100_000),
    ("1000000p", 100_000), ("1m", 100_000_000), ("2", 200_000_000_000),
    ("1287010n", 128_701_000), ("6435010n", 643_501_000),
    ("12870020n", 1_287_002_000), ("12870020000p", 1_287_002_000),
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


# ── sats maths ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("usd", [100, 500, 1000])
def test_agenthc_prices_are_accepted(usd):
    assert SATS[usd] == math.ceil(usd * 1e8 / BTC)
    assert hc.tip_sats_ok(usd, SATS[usd], BTC)
    assert hc.tip_sats_ok(usd, SATS[usd], int(BTC))


def test_sats_band_is_two_percent():
    fair = 500 * 1e8 / BTC
    assert hc.tip_sats_ok(500, math.floor(fair * 1.02), BTC)
    assert hc.tip_sats_ok(500, math.ceil(fair * 0.98), BTC)
    assert not hc.tip_sats_ok(500, math.ceil(fair * 1.02) + 1, BTC)
    assert not hc.tip_sats_ok(500, math.floor(fair * 0.98) - 1, BTC)
    assert not hc.tip_sats_ok(500, SATS[1000], BTC)  # the $1,000 price for $500


@pytest.mark.parametrize("usd, sats, btc", [
    (100, SATS[100], 999.0),            # BTC price below the sane range
    (100, SATS[100], 10_000_001.0),     # above it
    (100, SATS[100], float("nan")),
    (100, SATS[100], float("inf")),
    (100, SATS[100], True),
    (100, SATS[100], "77700"),
    (100, SATS[100], None),
    (100, 0, BTC), (100, -SATS[100], BTC), (100, float(SATS[100]), BTC),
    (100, True, BTC), (100, None, BTC),
    (200, 257402, BTC), (True, SATS[100], BTC), (100.0, SATS[100], BTC),
])
def test_sats_check_refuses_odd_values(usd, sats, btc):
    assert not hc.tip_sats_ok(usd, sats, btc)


def test_a_cheap_btc_price_cannot_inflate_a_tip():
    # Even a self-consistent answer is refused below the sane price range:
    # $1,000 at $999/BTC would be ~1 BTC.
    assert not hc.tip_sats_ok(1000, math.ceil(1000 * 1e8 / 999), 999)
    assert hc.tip_sats_ok(1000, math.ceil(1000 * 1e8 / 1000), 1000)


# ── client: what is sent, what is accepted ──────────────────────────────────

@pytest.mark.parametrize("bad", [99, 1001, 2000, 0, -100, "100", True, 100.0,
                                 None, [100], SATS[100]])
def test_create_tip_rejects_other_amounts_without_a_request(fake_http, bad):
    h = fake_http()
    with pytest.raises(ValueError):
        hc.create_tip(bad)
    assert h.calls == []


@pytest.mark.parametrize("n", [100, 500, 1000])
def test_create_tip_sends_only_the_usd_tier(fake_http, n):
    h = fake_http(_R(200, _created(n)))
    body = hc.create_tip(n)
    assert body["amount_usd"] == n and body["amount_sats"] == SATS[n]
    method, url, kw = h.calls[0]
    assert method == "post" and url == hc.AGENTHC_API + hc.TIP_PATH
    assert kw["json"] == {"amount_usd": n}
    assert set(kw) == {"json", "timeout"}  # no headers: no API key, no pass token


def test_create_tip_accepts_a_price_without_a_quote_block(fake_http):
    fake_http(_R(200, {k: v for k, v in _created(500).items() if k != "quote"}))
    assert hc.create_tip(500)["amount_sats"] == SATS[500]


@pytest.mark.parametrize("change", [
    {"amount_usd": 1000},                                  # another tier
    {"amount_usd": "500"}, {"amount_usd": 500.0}, {"amount_usd": True},
    {"payment_request": _sinv(SATS[500] + 1)},             # invoice ≠ amount_sats
    {"payment_request": _sinv(SATS[1000])},
    {"amount_sats": SATS[1000], "payment_request": _sinv(SATS[1000])},  # outside the band
    {"amount_sats": 643501.0},
    {"amount_sats": 0, "payment_request": _inv("")},
    {"btc_usd": 38850.0},                                  # sats don't match that price
    {"btc_usd": None}, {"btc_usd": "77700"}, {"btc_usd": 500.0},
    {"quote": {**QUOTE, "btc_usd": 76000.0}},              # btc_usd ≠ quote.btc_usd
    {"payment_request": _inv("")},                         # amountless invoice
    {"payment_request": "lntb6435010n1p" + "q" * 180},     # testnet
    {"payment_hash": "AB" * 32},                           # not lowercase hex
    {"payment_hash": "ab" * 31},
])
def test_create_tip_refuses_odd_answers(fake_http, change):
    fake_http(_R(200, {**_created(500), **change}))
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(500)
    assert e.value.code == "amount_mismatch" and str(e.value) == hc.TIP_MISMATCH


def test_create_tip_refuses_a_missing_amount(fake_http):
    body = _created(500)
    del body["amount_sats"]
    fake_http(_R(200, body), _R(200, ValueError("not json")), _R(200, ["x"]))
    for _ in range(3):
        with pytest.raises(hc.TipRefused):
            hc.create_tip(500)


@pytest.mark.parametrize("status", [404, 405])
def test_missing_tip_routes_mean_not_yet(fake_http, status):
    fake_http(_R(status), _R(status))
    with pytest.raises(hc.TipsUnavailable):
        hc.tip_options()
    with pytest.raises(hc.TipsUnavailable):
        hc.create_tip(100)


@pytest.mark.parametrize("code, msg", [
    ("price_unavailable", REASON_PRICE),
    ("tip_size_temporarily_unavailable", REASON_CAPACITY),
    ("node_unavailable", "Can't check our Lightning node right now. Try again in a minute."),
    ("tips_busy", "Too many unpaid tip invoices right now. Try again in a few minutes."),
])
def test_503_refusals_ask_for_fresh_tiers(fake_http, code, msg):
    fake_http(_R(503, {"detail": {"error": code, "message": msg}}))
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(1000)
    assert str(e.value) == msg and e.value.refresh is True and e.value.code == code


def test_invalid_amount_refusal_asks_for_fresh_tiers(fake_http):
    fake_http(_R(400, {"detail": {"error": "invalid_tip_amount",
                                  "message": "Pick $100, $500 or $1,000.",
                                  "options_usd": [100, 500, 1000]}}))
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(1000)
    assert str(e.value) == "Pick $100, $500 or $1,000." and e.value.refresh is True


def test_rate_limit_refusal_keeps_the_tiers(fake_http):
    fake_http(_R(429, {"detail": {"error": "too_many_tips", "message": "Wait."}}))
    with pytest.raises(hc.TipRefused) as e:
        hc.create_tip(100)
    assert e.value.refresh is False and str(e.value) == "Wait."


def test_options_keep_only_known_usd_tiers(fake_http):
    fake_http(_R(200, _options(tiers=[
        {"amount_usd": 100, "amount_sats": SATS[100], "available": True},
        {"amount_usd": 100, "amount_sats": SATS[100], "available": True},  # duplicate
        {"amount_usd": 2000, "amount_sats": 2574003, "available": True},   # not ours
        {"amount_usd": True, "available": True},
        {"amount_usd": "500", "available": True},
        {"amount_sats": 1000, "available": True},                          # v1 shape
        "junk",
    ])))
    body = hc.tip_options()
    assert [t["amount_usd"] for t in body["tiers"]] == [100]
    assert body["quote"] == QUOTE


def test_options_drop_sats_that_dont_match_the_quote(fake_http):
    fake_http(_R(200, _options(tiers=[
        {"amount_usd": 100, "amount_sats": SATS[1000], "available": True},
        {"amount_usd": 500, "amount_sats": "643501", "available": True},
        {"amount_usd": 1000, "amount_sats": SATS[1000], "available": True},
    ])))
    assert [t["amount_sats"] for t in hc.tip_options()["tiers"]] == [None, None, SATS[1000]]


@pytest.mark.parametrize("quote", [None, {"btc_usd": 0}, {"btc_usd": "77700"}, "x"])
def test_options_without_a_usable_quote_show_no_sats(fake_http, quote):
    body = _options()
    body["quote"] = quote
    fake_http(_R(200, body))
    out = hc.tip_options()
    assert out["quote"] is None
    assert all(t["amount_sats"] is None for t in out["tiers"])


def test_tip_status_checks_the_id_before_asking(fake_http):
    h = fake_http()
    for bad in ("../x", "AB" * 32, "ab" * 31, "", None, HASH + "/../../x"):
        with pytest.raises(ValueError):
            hc.tip_status(bad)
    assert h.calls == []


def test_tip_status_answers(fake_http):
    h = fake_http(_R(200, {"status": "expired"}), _R(200, {"status": "paid"}), _R(503))
    assert hc.tip_status(HASH) == {"status": "expired"}
    assert hc.tip_status(HASH)["status"] == "paid"
    with pytest.raises(RuntimeError):
        hc.tip_status(HASH)
    assert h.calls[0][1] == f"{hc.AGENTHC_API}{hc.TIP_PATH}/{HASH}"
    assert all(set(kw) == {"timeout"} for _, _, kw in h.calls)


@pytest.mark.parametrize("body", [
    {"detail": {"error": "tips_disabled", "message": "Tips are turned off right now."}},
    {"detail": {"error": "tip_not_found", "message": "No tip invoice with that id."}},
    {"detail": "Not Found"},
    ValueError("not json"),
])
def test_tip_status_404_is_never_expired(fake_http, body):
    """Turning tips off (or losing the tip row, or an older server) doesn't
    cancel the Lightning invoice: it may still be paid, so a 404 must never
    be read as "expired … nothing was charged"."""
    fake_http(_R(404, body))
    with pytest.raises(hc.TipStatusUnknown):
        hc.tip_status(HASH)


def test_status_route_404_keeps_polling_with_a_neutral_note(fake_http):
    fake_http(_R(404, {"detail": {"error": "tips_disabled"}}))
    r = webui._tip_status_route(None, {"payment_hash": HASH})
    assert r == {"ok": False, "retry": True, "unknown": True,
                 "error": hc.TIP_STATUS_UNKNOWN}
    assert "status" not in r and "charged" not in r["error"]


# ── local dashboard routes ──────────────────────────────────────────────────

def test_options_route_labels_tiers(fake_http, monkeypatch):
    monkeypatch.setitem(webui._tip_offered, "usd", ())
    fake_http(_R(200, _options()))
    r = webui._tip_options_route(None)
    assert r["ok"] is True and r["valid_seconds"] == 3600
    assert r["quote"] == {"btc_usd": BTC, "age_s": 412, "source": "fmp"}
    assert r["tiers"] == [
        {"amount_usd": 100, "amount_sats": 128701, "available": True, "reason": None,
         "label": "$100 (~128,700 sats)"},
        {"amount_usd": 500, "amount_sats": 643501, "available": True, "reason": None,
         "label": "$500 (~643,500 sats)"},
        # Today's channels: $1,000 doesn't fit one channel.
        {"amount_usd": 1000, "amount_sats": 1287002, "available": False,
         "reason": REASON_CAPACITY, "label": "$1,000 (~1,287,000 sats)"},
    ]
    assert webui._tip_offered["usd"] == (100, 500, 1000)


def test_options_route_without_a_price(fake_http):
    tiers = [{"amount_usd": n, "amount_sats": None, "available": False,
              "reason": REASON_PRICE} for n in (100, 500, 1000)]
    fake_http(_R(200, _options(tiers=tiers, quote=None)))
    r = webui._tip_options_route(None)
    assert r["quote"] is None
    assert [(t["label"], t["available"], t["reason"]) for t in r["tiers"]] == [
        ("$100", False, REASON_PRICE), ("$500", False, REASON_PRICE),
        ("$1,000", False, REASON_PRICE)]


def test_options_route_never_offers_a_tier_without_checked_sats(fake_http):
    # AgentHC says available, but its sats don't match its own quote.
    fake_http(_R(200, _options(tiers=[
        {"amount_usd": 100, "amount_sats": SATS[1000], "available": True, "reason": None}])))
    t = webui._tip_options_route(None)["tiers"][0]
    assert t["available"] is False and t["amount_sats"] is None
    assert t["reason"] == webui._TIP_SATS_ODD and t["label"] == "$100"


def test_options_route_not_yet(monkeypatch):
    def gone():
        raise hc.TipsUnavailable(hc.TIPS_NOT_YET)
    monkeypatch.setattr(hc, "tip_options", gone)
    assert webui._tip_options_route(None) == {"ok": False, "unavailable": True,
                                              "error": hc.TIPS_NOT_YET}


@pytest.mark.parametrize("bad", [99, "100", True, 100.0, None, 2000, SATS[100]])
def test_create_route_rejects_other_amounts(monkeypatch, bad):
    monkeypatch.setitem(webui._tip_offered, "usd", (100, 500, 1000, 2000))
    called = []
    monkeypatch.setattr(hc, "create_tip", lambda n: called.append(n))
    r = webui._tip_create(None, {"amount_usd": bad})
    assert r == {"ok": False, "error": "Pick one of the tip amounts shown."}
    assert called == []


def test_create_route_rejects_the_v1_body(monkeypatch):
    monkeypatch.setattr(hc, "create_tip", lambda n: pytest.fail("no request"))
    assert webui._tip_create(None, {"amount_sats": 500})["ok"] is False


def test_create_route_only_sends_tiers_agenthc_offered(monkeypatch):
    monkeypatch.setitem(webui._tip_offered, "usd", (100, 500))
    monkeypatch.setattr(hc, "create_tip", lambda n: pytest.fail("no request"))
    r = webui._tip_create(None, {"amount_usd": 1000})
    assert r == {"ok": False, "error": webui._TIP_NOT_OFFERED, "refresh_tiers": True}
    monkeypatch.setitem(webui._tip_offered, "usd", ())  # tiers never read
    assert webui._tip_create(None, {"amount_usd": 100})["refresh_tiers"] is True


def test_create_route_returns_the_invoice(fake_http, monkeypatch):
    fake_http(_R(200, _created(500)))
    monkeypatch.setattr(webui, "_qr_matrix", lambda data: [[1, 0], [0, 1]])
    r = webui._tip_create(None, {"amount_usd": 500})
    assert r == {"ok": True, "bolt11": _sinv(SATS[500]), "qr": [[1, 0], [0, 1]],
                 "payment_hash": HASH, "amount_usd": 500, "amount_sats": 643501,
                 "btc_usd": BTC, "expires_in": 3600}


def test_create_route_refuses_a_mismatched_invoice(fake_http):
    fake_http(_R(200, _created(500, invoice=_sinv(SATS[500] + 1))))
    r = webui._tip_create(None, {"amount_usd": 500})
    assert r == {"ok": False, "error": hc.TIP_MISMATCH, "refresh_tiers": False}


@pytest.mark.parametrize("market, shown", [
    (BTC, True), (BTC * 1.049, True), (BTC * 0.955, True),
    (BTC * 1.06, False), (BTC * 0.94, False),
    (0.0, True),   # no independent price to be had: AgentHC's checked price stands
])
def test_create_route_cross_checks_the_btc_price(fake_http, monkeypatch, market, shown):
    fake_http(_R(200, _created(1000)))
    monkeypatch.setattr(webui, "_btc_usd", lambda: market)
    r = webui._tip_create(None, {"amount_usd": 1000})
    if shown:
        assert r["ok"] is True and r["amount_sats"] == SATS[1000]
    else:
        assert r == {"ok": False, "error": webui._TIP_PRICE_ODD, "refresh_tiers": True}


def test_create_route_passes_refusals_on(monkeypatch):
    def refuse(n):
        raise hc.TipRefused(REASON_PRICE, code="price_unavailable", refresh=True)
    monkeypatch.setattr(hc, "create_tip", refuse)
    r = webui._tip_create(None, {"amount_usd": 100})
    assert r == {"ok": False, "error": REASON_PRICE, "refresh_tiers": True}

    def gone(n):
        raise hc.TipsUnavailable(hc.TIPS_NOT_YET)
    monkeypatch.setattr(hc, "create_tip", gone)
    assert webui._tip_create(None, {"amount_usd": 100})["unavailable"] is True


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
    """The money rule: no tip code path can pay anything or write anywhere,
    whatever the amount (every tier is far above max_autopay_sats)."""
    fns = (webui._tip_label, webui._tip_options_route, webui._tip_create,
           webui._tip_status_route, hc.tip_options, hc.create_tip, hc.tip_status,
           hc.bolt11_amount_msat, hc._is_tier, hc._detail_message,
           hc.tip_sats_ok, hc._btc_price, hc._tip_quote)
    for fn in fns:
        src = inspect.getsource(fn)
        for banned in ("lightning_wallet", "wallet_from_cfg", "pay_invoice", "pay(",
                       "trades.jsonl", "cfg[", "cfg.get", "save(", "state[",
                       "headers=", "open(", "max_autopay"):
            assert banned not in src, (fn.__name__, banned)


# ── the Host/Origin guard and the login cookie cover the tip routes ─────────

@pytest.fixture
def tip_server(home, monkeypatch):
    monkeypatch.setattr(hc, "create_tip", lambda n: _created(n))
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
    body = '{"amount_usd": 100}'
    assert _post(tip_server, "/api/tip/create", body)[0] == 401
    assert _post(tip_server, "/api/tip/create", body, cookie=cookie,
                 origin="http://evil.example.com")[0] == 403
    status, text = _post(tip_server, "/api/tip/create", body, cookie=cookie,
                         origin=f"http://127.0.0.1:{tip_server}")
    assert status == 200 and '"ok": true' in text and HASH in text
    assert '"amount_sats": 128701' in text
