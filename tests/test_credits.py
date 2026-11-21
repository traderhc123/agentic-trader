"""Prepaid trade credits (AgentHC per-entry pricing), the agent's half.

The money rules under test:
  - the balance the agent shows is ALWAYS AgentHC's last answer: nothing is
    counted locally;
  - a read with credits asks for exactly the entries the agent would take
    (none while paused, main only with other trades off), and no older than
    the agent would act on, so AgentHC never charges for one it would drop;
  - an entry AgentHC served is kept even when the same reply says the
    balance is now used up (it was paid with the last credits);
  - an active pass (signal days already paid for) is used before credits;
  - auto top-up is OFF by default, owner-only, capped by the autopay caps,
    saved before paying, never twice while one is unpaid, only on market
    days while LIVE when the balance can't cover the next entry;
  - a server that doesn't sell credits leaves the day-pass flow unchanged.
No network and no real invoices: every answer is scripted.
"""

import json
import types

import pytest

import agent
import lightning_wallet
import setup_plan
import webui
from sources import agenthc_day_trade_ideas as hc
from test_daypass_payments import WED_1030, Clock, Resp, bolt11

TOKEN = "L402 CREDMAC:paid"
PASS = "L402 PASSMAC:paid"
OFFER = {"pricing": "credits", "options_usd": [20, 50, 100], "paused": False,
         "topup_prices_sats": {"20": 20_000, "50": 50_000, "100": 100_000},
         "entry_prices_usd": {"main": 10.0, "other": 2.0},
         "max_topup_usd": 225, "max_entry_age_s": 600}
FAR = 4_000_000_000.0
HASH_T = "ef" * 32


def ev(event="ENTERED", track="main", ticker="SPY", at="2026-07-15T14:29:00+00:00"):
    e = {"event": event, "ticker": ticker, "expiry": "2026-07-17", "strike": 600,
         "type": "C", "occurred_at": at}
    if track != "main":
        e["track"] = track
    return e


def credits_block(balance=2400, tracks=("main", "other"), exits_only=False,
                  top_up=None, note=None):
    b = {"balance_usd": balance / 100, "balance_cents": balance,
         "entry_prices_usd": {"main": 10.0, "other": 2.0},
         "entries": ["main"], "entry_tracks": list(tracks),
         "exits_only": exits_only, "charged": [], "charged_usd": 0.0,
         "withheld": [], "not_sold": {"stale": 0, "closed": 0, "not_requested": 0},
         "max_entry_age_s": 270, "top_up": top_up}
    if note:
        b["note"] = note
    return b


def body(events=(), **block):
    return {"product": "x", "access": "credits", "events": list(events),
            "count": len(events), "timestamp": WED_1030.isoformat(),
            "credits": credits_block(**block)}


def cfg_(**kw):
    return {"source": "agenthc", "feed_credits": {"token": TOKEN},
            "feed_mode": "live", "max_event_age_s": 300, **kw}


def state_(balance=2400, status="active", tracks=("main", "other"), **kw):
    return {"positions": {}, "pass_status": {hc._token_id(TOKEN): {
        "status": status, "kind": "credits", "balance_cents": balance,
        "entry_tracks": list(tracks), "entry_prices_cents": {"main": 1000, "other": 200},
        "until_ts": FAR, "checked": 0, "first_seen": 0, "days_left": None}}, **kw}


@pytest.fixture
def clock(monkeypatch):
    c = Clock(WED_1030)
    monkeypatch.setattr(hc, "_now_et", lambda: c.now)
    monkeypatch.setattr(hc, "time", types.SimpleNamespace(time=c.time, strftime=c.strftime))
    return c


@pytest.fixture
def alerts(monkeypatch):
    import notifications
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    return sent


class FeedC:
    """Scripted feed: answers every read with `reply` (a body or Resp)."""

    def __init__(self, reply):
        self.reply, self.reads = reply, []

    def __call__(self, cfg, params, headers=None, mode=None):
        self.reads.append({"params": dict(params), "headers": dict(headers or {}),
                           "mode": mode})
        r = self.reply
        return r if isinstance(r, Resp) else Resp(r)


@pytest.fixture
def feed(monkeypatch):
    f = FeedC(body())
    monkeypatch.setattr(hc, "_feed_get", f)
    return f


class Http:
    """hc._http stand-in: scripted POST / GET answers, every call kept."""

    def __init__(self, post=None, get=None):
        self.posts, self.gets = [], []
        self._post, self._get = post, get

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return self._post(url, json) if callable(self._post) else self._post

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params, headers))
        return self._get(url, params, headers) if callable(self._get) else self._get


def _offer_on(monkeypatch, offer=OFFER):
    monkeypatch.setattr(hc, "_credit_cache",
                        {"at": float("inf"), "offer": hc._parse_credit_offer(offer)})


# ── 1. the offer ─────────────────────────────────────────────────────────────

def test_credit_offer_parses_the_probe_and_caches_it(monkeypatch):
    http = Http(post=Resp({"detail": {"error": "invalid_topup", **OFFER}}, 400))
    monkeypatch.setattr(hc, "_http", http)
    monkeypatch.setattr(hc, "_credit_cache", {"at": 0.0, "offer": None})
    got = hc.credit_offer()
    assert http.posts == [(hc.AGENTHC_API + hc.CREDITS_PATH, {"usd": 0})]
    assert got["options_usd"] == [20, 50, 100] and got["paused"] is False
    assert got["topup_prices_sats"] == {20: 20_000, 50: 50_000, 100: 100_000}
    assert got["entry_prices_cents"] == {"main": 1000, "other": 200}
    assert got["max_topup_usd"] == 225 and got["max_entry_age_s"] == 600
    # cached: credit_prices asks nothing more within the TTL
    assert hc.credit_prices() == got and len(http.posts) == 1


@pytest.mark.parametrize("answer", [
    Resp({"detail": "Not Found"}, 404),
    Resp({"detail": {"error": "credits_not_enabled"}}, 404),
    # an older server that answers the path with something else
    Resp({"detail": {"error": "invalid_days", "options": [1, 5]}}, 400),
    Resp({"detail": {"pricing": "credits", "options_usd": [20]}}, 400),  # no prices
])
def test_no_credit_offer_from_a_server_that_doesnt_sell_them(monkeypatch, answer):
    monkeypatch.setattr(hc, "_http", Http(post=answer))
    monkeypatch.setattr(hc, "_credit_cache", {"at": 0.0, "offer": "old"})
    assert hc.credit_offer() is None
    assert hc._credit_cache["offer"] is None and hc.credit_prices() is None


def test_an_unreachable_server_keeps_what_was_known(monkeypatch):
    def boom(url, payload):
        raise OSError("down")
    monkeypatch.setattr(hc, "_http", Http(post=boom))
    _offer_on(monkeypatch)
    assert hc.credit_offer() is None
    assert hc.credit_prices()["options_usd"] == [20, 50, 100]


def test_pass_offer_is_untouched_by_credits(monkeypatch):
    http = Http(post=Resp({"detail": {"error": "invalid_days", "options": [1, 5],
                                      "prices_usd": {"1": 50.0, "5": 225.0}}}, 400))
    monkeypatch.setattr(hc, "_http", http)
    assert hc.pass_offer()["options"] == [1, 5]
    assert http.posts == [(hc.AGENTHC_API + hc.PASS_PATH, {"days": 0})]


# ── 2. buying a top-up ───────────────────────────────────────────────────────

INVOICE = {"kind": "credits", "topup_usd": 20, "new_account": True, "amount_sats": 20_000,
           "payment_request": "lnbc200u1x", "payment_hash": HASH_T,
           "credit_token": TOKEN, "balance_usd": 0.0, "balance_cents": 0}


def test_buy_topup_payload_with_and_without_the_account(monkeypatch):
    http = Http(post=Resp(INVOICE))
    monkeypatch.setattr(hc, "_http", http)
    assert hc.buy_topup(20)["credit_token"] == TOKEN
    assert hc.buy_topup(50, TOKEN)
    assert [p for _u, p in http.posts] == [{"usd": 20}, {"usd": 50, "account": TOKEN}]


@pytest.mark.parametrize("resp, exc", [
    (Resp({"detail": {"error": "credits_not_enabled"}}, 404), hc.PassUnavailable),
    (Resp({"detail": {"error": "invalid_topup", "options_usd": [20, 50]}}, 400),
     hc.PassSizeUnavailable),
    (Resp({"detail": {"error": "invalid_account"}}, 400), hc.CreditAccountInvalid),
    (Resp({"detail": {"error": "account_pending", "payment_request": "lnbc1p",
                      "payment_hash": HASH_T, "amount_sats": 20_000,
                      "topup_usd": 20}}, 409), hc.CreditAccountPending),
    (Resp({"detail": {"error": "pass_size_temporarily_unavailable"}}, 503),
     hc.PassSizeRefilling),
    (Resp({"detail": {"error": "too_many_pending_topups"}}, 429), RuntimeError),
    (Resp({"detail": {"error": "too_many_purchases"}}, 429), RuntimeError),
    (Resp({"detail": {"error": "invoice_unavailable"}}, 503), RuntimeError),
])
def test_buy_topup_maps_every_server_error(monkeypatch, resp, exc):
    monkeypatch.setattr(hc, "_http", Http(post=resp))
    with pytest.raises(exc) as info:
        hc.buy_topup(20, TOKEN)
    if exc is hc.CreditAccountPending:
        assert info.value.invoice["payment_request"] == "lnbc1p"
    if exc is RuntimeError:
        assert "429" in str(info.value) or "503" in str(info.value)


# ── 3. what a credits read asks for ──────────────────────────────────────────

def test_feed_params_say_nothing_new_for_a_pass_read(clock):
    p = hc._feed_params(cfg_(), {})
    assert set(p) == {"limit", "track"}


def test_feed_params_ask_only_for_the_entries_the_agent_takes(clock):
    cfg = cfg_()
    assert hc._feed_params(cfg, {}, credits=True)["entries"] == "main"
    # other trades off, but an other-trades position is held: track=all is
    # read for its exit, and still only main entries are asked for
    held = {"positions": {"QQQ|2026-07-17|500.0|P": {"track": "other"}}}
    p = hc._feed_params(cfg, held, credits=True)
    assert p["track"] == "all" and p["entries"] == "main"
    p = hc._feed_params({**cfg, "include_other_trades": True}, {}, credits=True)
    assert p["track"] == "all" and p["entries"] == "all"
    hc.set_entry_wish("none")
    assert hc._feed_params({**cfg, "include_other_trades": True}, {},
                           credits=True)["entries"] == "none"
    hc.set_entry_wish(None)
    assert hc._feed_params(cfg, {}, credits=True)["entry_max_age"] == 270
    assert hc._feed_params({**cfg, "max_event_age_s": 40}, {},
                           credits=True)["entry_max_age"] == 30


def test_the_agent_loop_says_no_entries_while_paused(monkeypatch):
    import inspect
    assert 'source.set_entry_wish("none" if _paused(cfg) else None)' in \
        inspect.getsource(agent)
    seen = []
    monkeypatch.setattr(hc, "set_entry_wish", lambda v: seen.append(v))
    # the loop's own expression, as agent.py runs it before every poll
    for cfg, want in (({"paused": True}, "none"), ({}, None)):
        hc.set_entry_wish("none" if agent._paused(cfg) else None)
        assert seen[-1] == want


# ── 4. a credits read ────────────────────────────────────────────────────────

def test_a_read_stores_the_servers_balance_and_never_counts(clock, feed, alerts):
    feed.reply = body([ev()], balance=1400)
    state = state_(balance=2400)
    events = hc.poll(cfg_(), state)
    assert [e["event"] for e in events] == ["ENTERED"]
    assert feed.reads[0]["headers"] == {"Authorization": TOKEN}
    assert feed.reads[0]["params"]["entries"] == "main"
    rec = state["pass_status"][hc._token_id(TOKEN)]
    assert rec["balance_cents"] == 1400 and rec["status"] == "active"
    assert hc.had_access() and hc.had_exit_access()
    # never a signal day: nothing for the pass-day P&L
    assert "paid_days" not in state and "pass_charged_day" not in state


def test_active_or_exits_only_is_judged_against_the_tracks_the_agent_takes(clock, feed, alerts):
    # $5: covers an other-trades entry, not a main pick
    feed.reply = body(balance=500, tracks=("other",))
    st = state_()
    hc.poll(cfg_(), st)
    assert st["pass_status"][hc._token_id(TOKEN)]["status"] == "exits_only"
    assert not hc.had_access() and hc.had_exit_access()
    st = state_()
    hc.poll(cfg_(include_other_trades=True), st)
    assert st["pass_status"][hc._token_id(TOKEN)]["status"] == "active"
    assert hc.had_access()


def test_an_entry_bought_with_the_last_credits_is_kept(clock, feed, alerts):
    feed.reply = body([ev(), ev("EXITED", ticker="QQQ")], balance=0, tracks=(),
                      exits_only=True)
    events = hc.poll(cfg_(), state_())
    assert sorted(e["event"] for e in events) == ["ENTERED", "EXITED"]
    assert hc.had_access()          # an entry did arrive on this read


def test_an_entry_on_a_track_the_agent_doesnt_take_is_dropped(clock, feed, alerts):
    feed.reply = body([ev(track="other"), ev("EXITED", track="other", ticker="QQQ")])
    events = hc.poll(cfg_(), state_())
    assert [e["event"] for e in events] == ["EXITED"]


def test_credit_notices_come_once_a_day(clock, feed, alerts):
    st = state_()
    feed.reply = body(balance=0, tracks=(), exits_only=True,
                      note="One main-pick entry was held back.")
    hc.poll(cfg_(), st)
    hc.poll(cfg_(), st)
    assert len(alerts) == 1 and alerts[0].startswith("Trade credits used up")
    assert "held back" in alerts[0]
    feed.reply = body(balance=1500)          # below 2 × $10
    st2 = state_()
    hc.poll(cfg_(), st2)
    hc.poll(cfg_(), st2)
    assert len(alerts) == 2 and "Trade credits low: $15.00 left" in alerts[1]
    clock.tick(days=1)
    hc.poll(cfg_(), st2)
    assert len(alerts) == 3


# ── 5. which access is used ──────────────────────────────────────────────────

@pytest.mark.parametrize("pass_status, credit_status, want", [
    ("active", "active", PASS),          # signal days already paid: first
    ("exits_only", "active", TOKEN),     # entries come from the credits
    ("exits_only", "exits_only", PASS),
    (None, "exits_only", TOKEN),
])
def test_an_active_pass_comes_before_credits(clock, monkeypatch, pass_status,
                                             credit_status, want):
    # used-up credits behind a used-up pass are asked again: still used up
    monkeypatch.setattr(hc, "pass_status", lambda tok: {
        "kind": "credits", "status": "exits_only", "balance_cents": 0,
        "entry_tracks": [], "entry_prices_usd": {"main": 10.0, "other": 2.0},
        "access_until": "2036-01-01T00:00:00+00:00"})
    cfg = cfg_(feed_pass={"token": PASS})
    st = state_(status=credit_status)
    if pass_status:
        st["pass_status"][hc._token_id(PASS)] = {"status": pass_status, "until_ts": FAR,
                                                 "days_left": 3}
    else:
        st["pass_status"][hc._token_id(PASS)] = {"status": "expired", "until_ts": FAR,
                                                 "checked": WED_1030.timestamp()}
    assert hc._pass_tokens(cfg) == [PASS, TOKEN]
    assert hc._refresh_pass(cfg, st, lambda s: None) == want


def test_the_status_check_reads_credits_from_the_pass_route(clock, monkeypatch):
    monkeypatch.setattr(hc, "pass_status", lambda tok: {
        "kind": "credits", "status": "active", "balance_cents": 500,
        "entry_tracks": ["other"], "entry_prices_usd": {"main": 10.0, "other": 2.0},
        "access_until": "2036-01-01T00:00:00+00:00"})
    st = {"pass_status": {}}
    assert hc._refresh_pass(cfg_(), st, lambda s: None) == TOKEN
    rec = st["pass_status"][hc._token_id(TOKEN)]
    # AgentHC counts every track; this agent takes main picks only
    assert rec["status"] == "exits_only" and rec["balance_cents"] == 500
    assert rec["entry_prices_cents"] == {"main": 1000, "other": 200}


# ── 6. credits never buy a wallet day-pass ───────────────────────────────────

def test_used_up_credits_never_buy_a_wallet_day_pass(clock, feed, alerts, monkeypatch):
    monkeypatch.setattr(hc, "_wallet_over_exits_only",
                        lambda *a: pytest.fail("bought a day-pass over credits"))
    monkeypatch.setattr(hc, "_buy_day_pass",
                        lambda *a, **k: pytest.fail("bought a day-pass over credits"))
    feed.reply = body([ev("EXITED")], balance=0, tracks=(), exits_only=True)
    cfg = cfg_(lnbits_url="https://wallet.example", lnbits_admin_key="k",
               day_pass_recurring=True)
    events = hc.poll(cfg, state_(balance=0, status="exits_only", tracks=()))
    assert [e["event"] for e in events] == ["EXITED"]


def test_a_server_without_credits_leaves_the_day_pass_flow_as_it_was(clock, monkeypatch):
    # no feed_credits in config: nothing about credits is asked or sent
    monkeypatch.setattr(hc, "credit_offer", lambda: pytest.fail("asked for credits"))
    assert hc._credit_token({}) is None
    assert hc._pass_tokens({"feed_pass": {"token": PASS}}) == [PASS]
    assert "auto_usd" not in hc.autopay_caps({})
    assert hc.pass_summary({"lnbits_url": "x"}, {}) == "wallet — recurring day-pass OFF"


# ── 7. auto top-up ───────────────────────────────────────────────────────────

class TopupWallet:
    """Fake wallet: records the order of saves and payments."""

    def __init__(self, log, mode="ok"):
        self.log, self.mode, self.paid = log, mode, []
        self.status = {}

    def pay_invoice(self, invoice):
        self.log.append("pay")
        if self.mode == "refuse":
            raise lightning_wallet.PaymentRefused("Insufficient balance")
        if self.mode == "not_sent":
            raise lightning_wallet.PaymentNotSent("unreachable")
        self.paid.append(invoice)
        h = hc.bolt11_payment_hash(invoice)
        if self.mode == "hang":
            self.status[h] = ("pending", "")
            raise lightning_wallet.WalletError("no answer in time")
        self.status[h] = ("paid", "pre")
        return "pre"

    def payment_status(self, payment_hash):
        return self.status.get(payment_hash, ("not_found", ""))


class Shop:
    """AgentHC's credits counter: top-up invoices and the top-up list."""

    def __init__(self, sats=20_000, token=TOKEN):
        self.sats, self.token, self.bought, self.rows = sats, token, [], {}

    def buy_topup(self, usd, account_token=None):
        n = len(self.bought) + 1
        h = f"{n:064x}"
        self.bought.append((usd, account_token))
        self.rows[h] = "pending"
        return {**INVOICE, "topup_usd": usd, "new_account": False,
                "payment_request": bolt11(self.sats, h), "payment_hash": h,
                "amount_sats": self.sats, "credit_token": self.token}

    def credit_status(self, token, entries="all"):
        return {"kind": "credits", "status": "active", "balance_cents": 2400,
                "entry_tracks": ["main", "other"],
                "entry_prices_usd": {"main": 10.0, "other": 2.0},
                "topups": [{"payment_hash": h, "status": s} for h, s in self.rows.items()]}


AUTO = {"lnbits_url": "https://wallet.example", "lnbits_admin_key": "k",
        "credit_autotopup": True, "credit_topup_usd": 50}


@pytest.fixture
def shop(monkeypatch, clock):
    s = Shop()
    log = []
    w = TopupWallet(log)
    monkeypatch.setattr(hc, "buy_topup", s.buy_topup)
    monkeypatch.setattr(hc, "credit_status", s.credit_status)
    monkeypatch.setattr(hc, "wallet_from_cfg", lambda cfg: w)
    _offer_on(monkeypatch)
    s.wallet, s.log = w, log
    return s


def _low(state, cents=400):
    state["pass_status"][hc._token_id(TOKEN)]["balance_cents"] = cents
    return state


def _cr(balance=400):
    return credits_block(balance=balance, tracks=("other",) if balance >= 200 else (),
                         top_up={"options_usd": [20, 50, 100],
                                 "prices_sats": {"20": 20_000, "50": 50_000,
                                                 "100": 100_000}})


def test_auto_top_up_is_off_by_default(shop, alerts):
    st = _low(state_())
    cfg = cfg_(lnbits_url="https://wallet.example", lnbits_admin_key="k")
    assert hc._maybe_autotopup(cfg, st, lambda s: None, _cr()) is None
    assert shop.bought == []


def test_auto_top_up_buys_the_chosen_size_when_the_balance_cant_cover_an_entry(shop, alerts):
    shop.sats = 50_000
    st = _low(state_())
    saves = []

    def save(s):
        saves.append(json.loads(json.dumps(s.get("topup_pending"))))
        shop.log.append("save")
    assert hc._maybe_autotopup(cfg_(**AUTO), st, save, _cr()) == "bought"
    assert shop.bought == [(50, TOKEN)]
    # saved BEFORE the wallet was asked to pay
    assert shop.log.index("save") < shop.log.index("pay")
    assert saves[0]["usd"] == 50 and saves[0]["amount_sats"] == 50_000
    assert st["autopay_spent"]["sats"] == 50_000
    # a second read while it is unpaid: never another one
    assert hc._maybe_autotopup(cfg_(**AUTO), st, save, _cr()) == "pending"
    assert len(shop.bought) == 1
    # AgentHC credits it: done, balance from AgentHC's answer
    shop.rows[next(iter(shop.rows))] = "credited"
    st["pass_status"][hc._token_id(TOKEN)]["balance_cents"] = 400
    hc._maybe_autotopup(cfg_(**AUTO), st, save, _cr())
    assert "topup_pending" not in st
    assert st["pass_status"][hc._token_id(TOKEN)]["balance_cents"] == 2400


def test_no_top_up_while_the_balance_covers_the_next_entry(shop, alerts):
    st = state_(balance=1000)                    # exactly one main pick
    assert hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr(1000)) is None
    # with other trades on, the dearest wanted track (main) still decides
    st = state_(balance=900)
    hc._maybe_autotopup(cfg_(include_other_trades=True, **AUTO), st, lambda s: None, _cr(900))
    assert shop.bought == [(50, TOKEN)]


@pytest.mark.parametrize("caps, why", [
    ({"max_autopay_sats": 40_000}, "one payment"),
    ({"max_autopay_sats": 60_000, "max_autopay_sats_per_day": 60_000}, "one day"),
])
def test_auto_top_up_over_a_cap_is_refused_with_an_alert(shop, alerts, caps, why):
    shop.sats = 50_000
    st = _low(state_())
    if why == "one day":
        st["autopay_spent"] = {"day": "2026-07-15", "sats": 20_000, "count": 1}
    assert hc._maybe_autotopup(cfg_(**AUTO, **caps), st, lambda s: None, _cr()) == "failed"
    assert shop.wallet.paid == [] and "topup_pending" not in st
    assert len(alerts) == 1 and "over your autopay cap" in alerts[0] and why in alerts[0]
    # held off: the next read doesn't make another invoice
    hc._maybe_autotopup(cfg_(**AUTO, **caps), st, lambda s: None, _cr())
    assert len(shop.bought) == 1


def test_the_top_up_alert_leaves_once_the_balance_covers_entries_again(shop, feed, alerts):
    shop.wallet.mode = "refuse"
    st = _low(state_())
    hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr())
    assert st["daypass_alert"] == "credit_topup_failed_notified" and st["pass_notice"]
    feed.reply = body(balance=5000)               # the owner topped up by hand
    hc.poll(cfg_(**AUTO), st)
    assert "daypass_alert" not in st and "pass_notice" not in st


def test_a_wallet_that_refuses_pays_nothing_and_counts_nothing(shop, alerts):
    shop.wallet.mode = "refuse"
    st = _low(state_())
    assert hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr()) == "failed"
    assert "topup_pending" not in st and "autopay_spent" not in st
    assert "does not hold enough sats" in alerts[0]


def test_an_expired_unpaid_top_up_gives_the_budget_back(shop, alerts, clock):
    shop.wallet.mode = "hang"
    st = _low(state_())
    assert hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr()) == "pending"
    assert st["autopay_spent"]["sats"] == 20_000
    h = st["topup_pending"]["payment_hash"]
    shop.rows[h] = "expired"
    shop.wallet.status[h] = ("failed", "")
    clock.tick(hours=2)
    assert hc._settle_topup(cfg_(**AUTO), st, lambda s: None) == "failed"
    assert "topup_pending" not in st and st["autopay_spent"]["sats"] == 0


def test_a_paid_top_up_agenthc_never_credited_stops_auto_top_up(shop, alerts, clock):
    shop.wallet.mode = "hang"
    st = _low(state_())
    hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr())
    h = st["topup_pending"]["payment_hash"]
    shop.rows[h] = "expired"
    shop.wallet.status[h] = ("paid", "pre")
    clock.tick(hours=2)
    assert hc._settle_topup(cfg_(**AUTO), st, lambda s: None) == "failed"
    assert "topup_stop" in st and "Contact AgentHC" in alerts[-1]
    assert hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr()) is None
    assert len(shop.bought) == 1
    # the owner switches it off and on again: it may buy again
    clock.tick(minutes=1)
    on_at = clock.now.timestamp()
    hc._maybe_autotopup(cfg_(**AUTO, credit_autotopup_on_at=on_at), st,
                        lambda s: None, _cr())
    assert len(shop.bought) == 2


@pytest.mark.parametrize("change", ["paused", "practice", "closed", "unknown", "key",
                                    "no_wallet"])
def test_no_auto_top_up_when_it_must_not(shop, alerts, clock, change):
    cfg, st = cfg_(**AUTO), _low(state_())
    if change == "paused":
        hc.set_entry_wish("none")
    elif change == "practice":
        cfg["feed_mode"] = "dry_run"
    elif change == "closed":
        clock.now = clock.now.replace(hour=17)
    elif change == "unknown":
        rec = st["pass_status"][hc._token_id(TOKEN)]
        rec.update(status="pending", first_seen=0)    # an invoice past its lifetime
    elif change == "key":
        cfg["agenthc_api_key"] = "k"
    elif change == "no_wallet":
        cfg.pop("lnbits_url")
    assert hc._maybe_autotopup(cfg, st, lambda s: None, _cr()) is None
    assert shop.bought == []


def test_a_paused_offer_buys_nothing_and_says_so(shop, alerts):
    cr = _cr()
    cr["top_up"]["options_usd"] = []
    st = _low(state_())
    assert hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, cr) is None
    assert shop.bought == [] and "isn't selling" in alerts[0]


def test_a_top_up_for_another_account_is_never_paid(shop, alerts):
    shop.token = "L402 OTHER:paid"
    st = _low(state_())
    assert hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr()) == "failed"
    assert shop.wallet.paid == []


def test_the_size_falls_back_to_the_smallest_offered(shop, alerts):
    cr = _cr()
    cr["top_up"]["options_usd"] = [20, 100]
    hc._maybe_autotopup(cfg_(**AUTO), _low(state_()), lambda s: None, cr)
    assert shop.bought == [(20, TOKEN)]


def test_a_read_settles_the_top_up_and_payment_pending_says_so(shop, feed, alerts):
    shop.wallet.mode = "hang"
    st = _low(state_())
    hc._maybe_autotopup(cfg_(**AUTO), st, lambda s: None, _cr())
    assert hc.payment_pending(cfg_(**AUTO), st) is True
    shop.rows[st["topup_pending"]["payment_hash"]] = "credited"
    feed.reply = body(balance=2400)
    hc.poll(cfg_(**AUTO), st)
    assert "topup_pending" not in st


# ── 8. the caps follow the top-up price ──────────────────────────────────────

def test_auto_caps_follow_the_chosen_top_up(monkeypatch):
    _offer_on(monkeypatch)
    caps = hc.autopay_caps(cfg_(credit_topup_usd=50))
    # 50,000 × 1.10 = 55,000
    assert caps["per_invoice"] == 55_000 and caps["per_day"] == 110_000
    assert caps["auto_basis"] == "credits_topup" and caps["auto_usd"] == 50
    assert hc.autopay_cap_words(cfg_(credit_topup_usd=50)) == \
        "Auto: follows the $50 top-up price (now ≈ 55,000 sats)"
    # $100 at 100,000 sats: 110,000; never above the credits ceiling
    big = {**OFFER, "topup_prices_sats": {"20": 20_000, "50": 50_000, "100": 900_000}}
    _offer_on(monkeypatch, big)
    assert hc.autopay_caps(cfg_(credit_topup_usd=100))["per_invoice"] == 300_000


def test_an_owners_fixed_caps_are_used_exactly_in_credits_mode(monkeypatch):
    _offer_on(monkeypatch)
    caps = hc.autopay_caps(cfg_(max_autopay_sats=45_000, max_autopay_sats_per_day=70_000))
    assert caps["per_invoice"] == 45_000 and caps["per_day"] == 70_000
    assert hc.autopay_cap_words(cfg_(max_autopay_sats=45_000)) == "Fixed: 45,000 sats"


def _day_price(monkeypatch, sats=50_000):
    monkeypatch.setattr(hc, "_price_cache",
                        {"at": float("inf"), "usd": {1: 50.0}, "sats": {1: sats}})


def test_a_credits_account_never_shrinks_the_day_pass_cap(monkeypatch):
    _offer_on(monkeypatch)
    _day_price(monkeypatch)                         # 50,000 × 1.10 = 55,000
    cfg = cfg_(credit_topup_usd=20)                 # a $20 top-up: 22,000
    day = hc.autopay_caps(cfg, "daypass")
    assert day["per_invoice"] == day["per_day"] == 55_000
    assert day["auto_basis"] == "server_sats" and "auto_usd" not in day
    top = hc.autopay_caps({**cfg, "day_pass_recurring": True}, "topup")
    assert top["per_invoice"] == 22_000 and top["auto_basis"] == "credits_topup"
    # what the owner is shown: the top-up price, unless recurring day-pass is ON
    assert hc.autopay_caps(cfg)["auto_basis"] == "credits_topup"
    assert hc.autopay_caps({**cfg, "day_pass_recurring": True})["per_invoice"] == 55_000
    # a credits account is not a prepaid pass: the owner's re-buy choice stands
    assert not hc._one_pass_a_day(cfg_(max_autopay_sats_per_day=120_000))
    assert hc._one_pass_a_day(cfg_(max_autopay_sats_per_day=120_000,
                                   feed_pass={"token": PASS}))


def test_an_unpaid_top_up_never_blocks_the_recurring_day_pass(monkeypatch, clock, alerts):
    from test_daypass_payments import Feed, Wallet
    _offer_on(monkeypatch)
    _day_price(monkeypatch)
    feed, wallet = Feed(sats=50_000), Wallet()
    monkeypatch.setattr(hc.requests, "get", feed.get)
    monkeypatch.setattr(hc, "wallet_from_cfg", lambda cfg: wallet)
    cfg = cfg_(day_pass_recurring=True, credit_topup_usd=20,
               lnbits_url="https://wallet.example", lnbits_admin_key="k")
    now = clock.time()
    # the owner asked for a $20 top-up in the dashboard and hasn't paid it
    st = state_(status="pending", balance=0, tracks=())
    st["pass_status"][hc._token_id(TOKEN)].update(checked=now, first_seen=now)
    hc.poll(cfg, st)
    assert len(wallet.paid) == 1 and st["autopay_spent"]["sats"] == 50_000


def test_used_up_credits_behind_a_used_up_pass_are_asked_again(clock, monkeypatch):
    now = clock.time()
    cfg = cfg_(feed_pass={"token": PASS})
    st = state_(balance=0, status="exits_only", tracks=())
    st["pass_status"][hc._token_id(TOKEN)]["checked"] = now
    st["pass_status"][hc._token_id(PASS)] = {
        "status": "exits_only", "until_ts": now + 3 * 86400, "checked": now,
        "first_seen": now - 9e5}
    asked = []

    def status(tok):
        asked.append(tok)
        assert tok == TOKEN
        return {"kind": "credits", "status": "active", "balance_cents": 5000,
                "entry_tracks": ["main", "other"],
                "entry_prices_usd": {"main": 10.0, "other": 2.0},
                "access_until": "2036-11-21T00:00:00+00:00"}

    monkeypatch.setattr(hc, "pass_status", status)
    assert hc._refresh_pass(cfg, st, lambda s: None) == PASS
    assert asked == []                              # just checked: not yet
    clock.tick(minutes=2)                           # the owner topped up meanwhile
    assert hc._refresh_pass(cfg, st, lambda s: None) == TOKEN
    assert asked == [TOKEN]
    # while a pass still has days left, used-up credits cost no request
    st["pass_status"][hc._token_id(TOKEN)].update(status="exits_only", checked=0)
    st["pass_status"][hc._token_id(PASS)]["status"] = "active"
    assert hc._refresh_pass(cfg, st, lambda s: None) == PASS
    assert asked == [TOKEN]


def test_reading_with_used_up_credits_needs_no_extra_status_check(clock, feed, alerts,
                                                                  monkeypatch):
    monkeypatch.setattr(hc, "pass_status",
                        lambda tok: pytest.fail("asked again while reading"))
    feed.reply = body([ev("EXITED")], balance=0, tracks=(), exits_only=True)
    st = state_(balance=0, status="exits_only", tracks=())
    for _ in range(3):
        clock.tick(minutes=2)
        hc.poll(cfg_(), st)
    assert len(feed.reads) == 3


# ── 9. what the owner reads ──────────────────────────────────────────────────

def test_pass_summary_says_the_servers_balance():
    assert hc.pass_summary(cfg_(), state_()) == \
        "trade credits · $24.00 left (main $10 / other $2 per entry)"
    assert hc.pass_summary(cfg_(), state_(balance=0, status="exits_only", tracks=())) == \
        "trade credits used up — exit signals only; top up"
    assert hc.pass_summary(cfg_(), {}) == "trade credits · waiting for the first payment"
    assert agent._feed_access(cfg_(), state_()).startswith("trade credits · $24.00")


def _doctor(cfg, monkeypatch, status):
    monkeypatch.setattr(hc, "credit_status", lambda tok, entries="all": status)
    out = []
    agent._feed_access_check(cfg, {}, lambda cid, ok, label, detail="", fix="", who="agent":
                             out.append({"id": cid, "ok": ok, "detail": detail}))
    return {c["id"]: c for c in out}


def test_doctor_credit_cases(monkeypatch):
    base = {"kind": "credits", "entry_prices_usd": {"main": 10.0, "other": 2.0}}
    got = _doctor(cfg_(include_other_trades=True), monkeypatch, {
        **base, "status": "active", "balance_cents": 2400, "entry_tracks": ["main", "other"],
        "entries_left": {"main": 2, "other": 12}})
    assert got["feed_access"]["ok"] is True
    assert got["feed_access"]["detail"] == "AgentHC trade credits: $24.00 (2 main / 12 other entries)"
    assert got["credit_autotopup"]["detail"].startswith("off")
    got = _doctor(cfg_(), monkeypatch, {**base, "status": "exits_only", "balance_cents": 100,
                                        "entry_tracks": [], "entries_left": {}})
    assert got["feed_access"]["ok"] is False and "top up (exits still free)" in got["feed_access"]["detail"]
    got = _doctor(cfg_(), monkeypatch, {**base, "status": "pending", "balance_cents": 0,
                                        "entry_tracks": [], "topups": []})
    assert got["feed_access"]["ok"] is False and "isn't paid yet" in got["feed_access"]["detail"]


def test_doctor_says_auto_top_up_with_its_cap(monkeypatch):
    _offer_on(monkeypatch)
    got = _doctor(cfg_(credit_autotopup=True, credit_topup_usd=20), monkeypatch, {
        "kind": "credits", "status": "active", "balance_cents": 2400,
        "entry_tracks": ["main"], "entry_prices_usd": {"main": 10.0}})
    assert got["credit_autotopup"]["detail"].startswith("on — buys $20")
    assert "22,000 sats" in got["credit_autotopup"]["detail"]


def test_the_pause_note_says_a_paused_read_uses_no_credits(monkeypatch):
    monkeypatch.setattr(agent, "_move_exits_only", lambda: False)
    monkeypatch.setattr(agent, "_real_positions", lambda st: True)
    monkeypatch.setattr(agent, "_no_pass_exits_note", lambda *a: "")
    note = agent._pause_note(cfg_(paused=True), {})
    assert "uses no trade credits" in note


# ── 10. the dashboard ────────────────────────────────────────────────────────

class Counter:
    def __init__(self):
        self.calls, self.status = [], {"kind": "credits", "status": "pending",
                                       "balance_cents": 0, "topups": []}
        self.reply = dict(INVOICE)
        self.raise_once = None

    def buy_topup(self, usd, account_token=None):
        self.calls.append((usd, account_token))
        if self.raise_once is not None:
            exc, self.raise_once = self.raise_once, None
            raise exc
        return {**self.reply, "topup_usd": usd, "new_account": account_token is None,
                "payment_request": f"lnbc{usd}u1topup{len(self.calls)}",
                "payment_hash": f"{len(self.calls):064x}"}

    def credit_status(self, token, entries="all"):
        return self.status


@pytest.fixture
def counter(monkeypatch):
    c = Counter()
    monkeypatch.setattr(hc, "buy_topup", c.buy_topup)
    monkeypatch.setattr(hc, "credit_status", c.credit_status)
    monkeypatch.setattr(webui, "_qr_matrix", lambda text: [[1]])
    return c


def test_buying_a_top_up_opens_the_account_and_keeps_the_invoice(counter):
    cfg, saves = {}, []
    r = webui._pass_buy(cfg, lambda: saves.append(1), {"topup_usd": 20})
    assert r["ok"] and r["kind"] == "credits" and r["topup_usd"] == 20
    assert cfg["feed_credits"]["token"] == TOKEN and cfg["source"] == "agenthc"
    assert cfg["feed_credits"]["first_payment_hash"] == f"{1:064x}"
    assert cfg["feed_credits_pending"][0]["usd"] == 20 and saves
    # the same size again (a double click): the same invoice, no new one
    again = webui._pass_buy(cfg, lambda: None, {"topup_usd": 20})
    assert again["bolt11"] == r["bolt11"] and len(counter.calls) == 1
    # another size names the account
    webui._pass_buy(cfg, lambda: None, {"topup_usd": 50})
    assert counter.calls[-1] == (50, TOKEN)
    assert [p["usd"] for p in cfg["feed_credits_pending"]] == [50, 20]


def test_an_unpaid_first_top_up_is_shown_again(counter):
    cfg = {"feed_credits": {"token": TOKEN}}
    counter.raise_once = hc.CreditAccountPending("unpaid", {
        "payment_request": "lnbc200u1first", "payment_hash": HASH_T,
        "amount_sats": 20_000, "topup_usd": 20})
    r = webui._pass_buy(cfg, lambda: None, {"topup_usd": 50})
    assert r["ok"] and r["bolt11"] == "lnbc200u1first" and "isn't paid yet" in r["note"]
    assert cfg["feed_credits_pending"][0]["payment_hash"] == HASH_T


def test_an_account_agenthc_doesnt_know_is_put_aside_and_a_new_one_opened(counter):
    cfg = {"feed_credits": {"token": "L402 OLD:paid"}}
    counter.raise_once = hc.CreditAccountInvalid("no")
    r = webui._pass_buy(cfg, lambda: None, {"topup_usd": 20})
    assert r["ok"] and counter.calls == [(20, "L402 OLD:paid"), (20, None)]
    assert cfg["feed_credits"]["token"] == TOKEN
    assert cfg["feed_credits_retired"][0]["token"] == "L402 OLD:paid"   # never thrown away


def test_pass_status_reports_the_balance_and_files_credited_top_ups(counter, monkeypatch):
    monkeypatch.setattr(hc, "pass_status", lambda tok: {"status": "invalid"})
    cfg = {}
    webui._pass_buy(cfg, lambda: None, {"topup_usd": 20})
    h = cfg["feed_credits_pending"][0]["payment_hash"]
    counter.status = {"kind": "credits", "status": "active", "balance_cents": 2000,
                      "entry_tracks": ["main", "other"], "entries_left": {"main": 2, "other": 10},
                      "entry_prices_usd": {"main": 10.0, "other": 2.0},
                      "topups": [{"payment_hash": h, "status": "credited"}]}
    r = webui._pass_status(cfg, lambda: None)
    assert r["credits"]["balance_usd"] == 20.0 and r["credits"]["kind"] == "credits"
    assert r["credits"]["topup"] == {"payment_hash": h, "usd": 20, "status": "credited"}
    assert r["credits"]["entries_left"] == {"main": 2, "other": 10}
    assert "feed_credits_pending" not in cfg and cfg["feed_credits"]["token"] == TOKEN


def test_an_account_whose_first_top_up_expired_unpaid_is_put_aside(counter, monkeypatch):
    monkeypatch.setattr(hc, "pass_status", lambda tok: {"status": "invalid"})
    cfg = {}
    webui._pass_buy(cfg, lambda: None, {"topup_usd": 20})
    h = cfg["feed_credits"]["first_payment_hash"]
    counter.status = {"kind": "credits", "status": "pending", "balance_cents": 0,
                      "topups": [{"payment_hash": h, "status": "expired"}]}
    r = webui._pass_status(cfg, lambda: None)
    assert r["credits"]["status"] == "none" and "feed_credits" not in cfg
    assert cfg["feed_credits_retired"][0]["token"] == TOKEN


from test_v2_settings_api import BASE, app, read_cfg, write_cfg  # noqa: E402,F401


def test_v2_routes_for_credits(home, app, counter, monkeypatch):
    monkeypatch.setattr(hc, "pass_offer", lambda: {"options": [1, 5], "prices_usd": None,
                                                   "prices_sat": None, "prices_sats": None})
    monkeypatch.setattr(hc, "credit_offer", lambda: hc._parse_credit_offer(OFFER))
    write_cfg(home, BASE)
    c = app()
    opts = c.get("/api/pass/options")
    assert opts["options"] == [1, 5]
    assert opts["credits"]["topup_prices_sats"] == {"20": 20_000, "50": 50_000, "100": 100_000}
    # a top-up through the /v2 two steps
    first = c.post("/api/v2/pass/buy", {"topup_usd": 20})
    assert first["needs_confirm"] and first["preview"]["changes"][0]["key"] == "credit_topup"
    assert counter.calls == []                      # nothing asked before the confirm
    inv = c.post("/api/v2/pass/buy", {"topup_usd": 20, "confirm": first["preview"]["token"]})
    assert inv["ok"] and inv["kind"] == "credits"
    assert read_cfg(home)["feed_credits"]["token"] == TOKEN
    # days still work, both at once doesn't
    assert c.post("/api/v2/pass/buy", {"days": 1})["needs_confirm"]
    assert c.post("/api/v2/pass/buy", {"days": 1, "topup_usd": 20})["ok"] is False
    # the settings say it (never the token)
    view = c.get("/api/v2/settings")["signals"]["credits"]
    assert view["account"] is True and view["autotopup"] is False
    assert TOKEN not in json.dumps(c.get("/api/v2/settings"))


def test_auto_top_up_switch_is_two_steps_and_never_a_phone_route(home, app):
    start = {**BASE, "lnbits_url": "https://wallet.example", "lnbits_admin_key": "k"}
    write_cfg(home, start)
    c = app()
    assert "/api/v2/credits/autotopup" not in webui.REMOTE_POST
    assert "/api/credits/autotopup" not in webui.REMOTE_POST
    first = c.post("/api/v2/credits/autotopup", {"on": True, "usd": 50})
    assert first["needs_confirm"] and read_cfg(home) == start
    keys = [ch["key"] for ch in first["preview"]["changes"]]
    assert keys == ["credit_autotopup", "credit_topup_usd"]
    done = c.post("/api/v2/credits/autotopup",
                  {"on": True, "usd": 50, "confirm": first["preview"]["token"]})
    assert done["ok"] and done["saved"]
    saved = read_cfg(home)
    assert saved["credit_autotopup"] is True and saved["credit_topup_usd"] == 50
    assert saved["credit_autotopup_on_at"]
    # changing only the size while it is on does not restart a stopped
    # auto top-up: only switching it off and on again does
    on_at = saved["credit_autotopup_on_at"] = "2026-01-01T00:00:00Z"
    write_cfg(home, saved)
    c.post("/api/credits/autotopup", {"on": True, "usd": 100})
    assert read_cfg(home)["credit_topup_usd"] == 100
    assert read_cfg(home)["credit_autotopup_on_at"] == on_at
    c.post("/api/credits/autotopup", {"on": False})
    assert read_cfg(home)["credit_autotopup_on_at"] == on_at
    c.post("/api/credits/autotopup", {"on": True})
    assert read_cfg(home)["credit_autotopup_on_at"] != on_at
    # bad sizes and values change nothing
    for bad in ({"on": "yes"}, {"on": True, "usd": 30}, {"on": True, "usd": True}):
        assert c.post("/api/v2/credits/autotopup", bad)["ok"] is False
    # the classic switch, without a wallet: refused
    write_cfg(home, BASE)
    assert c.post("/api/credits/autotopup", {"on": True})["ok"] is False
    assert "credit_autotopup" not in read_cfg(home)


def test_the_settings_fingerprint_covers_the_auto_top_up():
    assert {"credit_autotopup", "credit_topup_usd"} <= set(webui._FP_KEYS)


def test_the_fund_quote_counts_top_ups_in_credits_mode(monkeypatch):
    _offer_on(monkeypatch)
    monkeypatch.setattr(webui, "_btc_usd", lambda: 100_000.0)
    q = webui._fund_quote(100_000, credits_cfg=cfg_(credit_topup_usd=50))
    assert q["topups"] == 2 and q["topup_usd"] == 50 and q["topup_sats"] == 50_000
    assert "topups" not in webui._fund_quote(100_000)


# ── 11. the view-model ───────────────────────────────────────────────────────

def test_view_model_credits_pass():
    import guardian_states as gs
    import view_model as vm
    s = gs.status("credits-active")
    assert vm.validate(s) == []
    p = s["signals"]["pass"]
    assert p["kind"] == "credits" and p["status"] == "active"
    assert p["balance_usd"] == 24.0
    assert p["entries_left_main"] == 2 and p["entries_left_other"] == 12
    assert p["auto_topup"] is False
    used = gs.status("credits-used-up")["signals"]["pass"]
    assert used["kind"] == "credits" and used["status"] == "exits_only"


# ── 12. a setup plan can never turn spending on ──────────────────────────────

@pytest.mark.parametrize("plan", [
    {"source": {"type": "agenthc", "credit_autotopup": True}},
    {"feed_access": {"credit_autotopup": True}},
    {"credit_topup_usd": 100},
])
def test_setup_plan_refuses_auto_top_up(home, plan):
    new, report = setup_plan.apply(plan, {}, True, str(home))
    assert new is None and report["errors"]
    assert "credit_autotopup" not in setup_plan.ALLOWED_TOP
