"""Audit PR3: Lightning / pass payments.

  1. A Premium API key with an empty balance never makes the wallet buy a
     day-pass (that pass would never be sent with the key): nothing is paid
     and the owner is told to top up the key.
  2. A prepaid-pass invoice past its lifetime is only "unpaid" when AgentHC
     says so: one the owner paid while the agent wasn't checking is used,
     and while AgentHC can't be asked the wallet buys nothing.
  3. LNbits' routing-fee reserve: the low-balance warning counts it, and
     LNbits' "You must reserve at least ..." refusal reads as "add sats".
"""

import types

import pytest

import lightning_wallet
import notifications
from sources import agenthc_day_trade_ideas as agenthc
from test_daypass_payments import Clock, Feed, Wallet, WED_1030, _setup


@pytest.fixture
def clock(monkeypatch):
    c = Clock(WED_1030)
    monkeypatch.setattr(agenthc, "_now_et", lambda: c.now)
    monkeypatch.setattr(agenthc, "time",
                        types.SimpleNamespace(time=c.time, strftime=c.strftime))
    return c


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    return sent


# ── 1. Premium key + wallet: the wallet never buys a day-pass ──────────────

class _KeyFeed:
    """AgentHC answering an API key whose balance is empty: 402 + a
    day-pass invoice. Any L402 token gets the events."""

    def __init__(self):
        self.sent = []

    def get(self, url, params=None, headers=None, timeout=None):
        headers = dict(headers or {})
        self.sent.append(headers)
        ok = str(headers.get("Authorization", "")).startswith("L402 ")

        class R:
            status_code = 200 if ok else 402
            content = b"{}"
            text = "{}"

            def json(self):
                return ({"events": []} if ok else
                        {"payment": {"payment_request": "lnbc1x", "macaroon": "mac",
                                     "amount_sats": 25_000}})

            def raise_for_status(self):
                if not ok:
                    raise RuntimeError("402 Payment Required")
        return R()


def test_key_with_empty_balance_never_buys_a_wallet_day_pass(monkeypatch, clock, alerts):
    paid = []

    class PayingWallet:
        def pay_invoice(self, inv):
            paid.append(inv)
            return "ab" * 32

        def balance_sats(self):
            return 10 ** 6
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: PayingWallet())
    monkeypatch.setattr(agenthc, "wallet_fingerprint", lambda cfg: "w")
    monkeypatch.setattr(agenthc, "_invoice_sats", lambda inv, claimed: 25_000)
    monkeypatch.setattr(agenthc, "bolt11_payment_hash", lambda inv: "cd" * 32)
    feed = _KeyFeed()
    monkeypatch.setattr(agenthc.requests, "get", feed.get)
    cfg = {"source": "agenthc", "agenthc_api_key": "k",
           "lnbits_url": "https://w", "lnbits_admin_key": "x",
           "day_pass_recurring": True}
    state = {}
    for _ in range(3):
        assert agenthc.poll(cfg, state, lambda s: None) == []
        assert agenthc.exit_loss_reason() == "feed_refused"
        clock.tick(minutes=10)
    assert paid == []                              # no sats spent
    assert "l402" not in state and "daypass_pending" not in state
    # the key only (and the agent's mode: only LIVE uses a day)
    assert all(h == {"X-API-Key": "k", "X-Agent-Mode": "live"} for h in feed.sent)
    # the owner hears it once a day, in plain words
    assert len(alerts) == 1 and "Top up your key" in alerts[0]
    assert "nothing was paid" in alerts[0]
    assert state["pass_notice"] == agenthc.KEY_BALANCE_EMPTY_TEXT
    clock.tick(days=1)
    agenthc.poll(cfg, state, lambda s: None)
    assert len(alerts) == 2 and paid == []


def test_purchase_gate_is_closed_while_a_key_is_set(monkeypatch, clock):
    class Funded:   # the wallet answers before a purchase (_wallet_can_pay)
        def balance_sats(self):
            return 1_000_000
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: Funded())
    cfg = {"agenthc_api_key": "k", "lnbits_url": "https://w",
           "lnbits_admin_key": "x", "day_pass_recurring": True}
    assert agenthc._pass_purchase_allowed(cfg, {}, lambda s: None) is False
    cfg.pop("agenthc_api_key")
    assert agenthc._pass_purchase_allowed(cfg, {}, lambda s: None) is True


# ── 2. prepaid pass: "unpaid" only on AgentHC's word ───────────────────────

TOK = "L402 PASS5:tok"


def _server(monkeypatch, answer):
    asked = []

    def pass_status(token):
        asked.append(token)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)
    monkeypatch.setattr(agenthc, "pass_status", pass_status)
    return asked


ACTIVE = {"status": "active", "days_left": 5,
          "access_until": "2026-07-25T20:00:00+00:00"}


def test_pass_paid_while_the_agent_slept_is_asked_about_and_used(monkeypatch, clock, alerts):
    feed, wallet = Feed(sats=10_000), Wallet("ok")
    _setup(monkeypatch, feed, wallet)
    asked = _server(monkeypatch, ACTIVE)
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w",
           "lnbits_admin_key": "k", "feed_pass": {"token": TOK}}
    now = clock.time()
    # seen pending 70 minutes ago, then the PC slept; the owner paid meanwhile
    state = {"positions": {}, "pass_status": {agenthc._token_id(TOK): {
        "status": "pending", "first_seen": now - 70 * 60, "checked": now - 69 * 60}}}
    events = agenthc.poll(cfg, state, lambda s: None)
    assert asked == [TOK]
    assert state["pass_status"][agenthc._token_id(TOK)]["status"] == "active"
    assert wallet.paid == []                      # no day-pass on top
    assert feed.gets == [TOK] and len(events) == 1


def test_expired_invoice_that_cant_be_checked_buys_nothing(monkeypatch, clock, alerts):
    feed, wallet = Feed(sats=10_000), Wallet("ok")
    _setup(monkeypatch, feed, wallet)
    asked = _server(monkeypatch, ConnectionError("AgentHC down"))
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w",
           "lnbits_admin_key": "k", "feed_pass_pending": {"token": TOK}}
    now = clock.time()
    state = {"positions": {}, "pass_status": {agenthc._token_id(TOK): {
        "status": "pending", "first_seen": now - 70 * 60, "checked": now - 69 * 60}}}
    for _ in range(3):
        assert agenthc.poll(cfg, state, lambda s: None) == []
        assert agenthc.exit_loss_reason() == "pass_unchecked"
        clock.tick(minutes=2)
    assert len(asked) == 3
    assert wallet.paid == [] and feed.gets == []
    assert state["pass_status"][agenthc._token_id(TOK)]["status"] == "pending"
    assert agenthc.payment_pending(cfg, state)    # keeps asking every loop
    # AgentHC answers: the invoice was never paid -> unpaid, the wallet may buy
    _server(monkeypatch, {"status": "pending"})
    events = agenthc.poll(cfg, state, lambda s: None)
    assert state["pass_status"][agenthc._token_id(TOK)]["status"] == "unpaid"
    assert len(wallet.paid) == 1 and len(events) == 1


def test_unpaid_saved_from_elapsed_time_alone_is_asked_again(monkeypatch, clock, alerts):
    """An older version wrote "unpaid" without asking AgentHC."""
    feed, wallet = Feed(sats=10_000), Wallet("ok")
    _setup(monkeypatch, feed, wallet)
    asked = _server(monkeypatch, ACTIVE)
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w",
           "lnbits_admin_key": "k", "feed_pass": {"token": TOK}}
    now = clock.time()
    state = {"positions": {}, "pass_status": {agenthc._token_id(TOK): {
        "status": "unpaid", "first_seen": now - 5 * 3600,
        "checked": now - 5 * 3600 + 3000}}}
    events = agenthc.poll(cfg, state, lambda s: None)
    assert asked == [TOK] and wallet.paid == []
    assert feed.gets == [TOK] and len(events) == 1


def test_unpaid_pass_that_became_the_main_pass_is_asked_again(monkeypatch, clock, alerts):
    feed, wallet = Feed(sats=10_000), Wallet("ok")
    _setup(monkeypatch, feed, wallet)
    asked = _server(monkeypatch, ACTIVE)
    now = clock.time()
    # AgentHC called it unpaid after its invoice expired, while it was only
    # an extra invoice; the dashboard has since made it the main pass
    state = {"positions": {}, "pass_status": {agenthc._token_id(TOK): {
        "status": "unpaid", "first_seen": now - 3 * 3600,
        "checked": now - 600, "main": False}}}
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w",
           "lnbits_admin_key": "k", "feed_pass": {"token": TOK}}
    assert agenthc.payment_pending(cfg, state)
    events = agenthc.poll(cfg, state, lambda s: None)
    assert asked == [TOK] and wallet.paid == [] and len(events) == 1
    assert state["pass_status"][agenthc._token_id(TOK)]["main"] is True


def test_unpaid_pass_is_asked_again_hourly_not_on_every_poll(monkeypatch, clock, alerts):
    feed, wallet = Feed(sats=10_000), Wallet("ok")
    _setup(monkeypatch, feed, wallet)
    asked = _server(monkeypatch, {"status": "pending"})
    now = clock.time()
    state = {"positions": {}, "pass_status": {agenthc._token_id(TOK): {
        "status": "unpaid", "first_seen": now - 3 * 3600,
        "checked": now - 600, "main": True}}}
    cfg = {"feed_pass": {"token": TOK}}
    for _ in range(5):
        agenthc.poll(cfg, state, lambda s: None)
        clock.tick(minutes=5)
    assert asked == []
    clock.tick(minutes=40)
    agenthc.poll(cfg, state, lambda s: None)
    assert asked == [TOK] and wallet.paid == []


# ── 3. LNbits routing-fee reserve ──────────────────────────────────────────

def test_fee_reserve_is_lnbits_default():
    assert lightning_wallet.fee_reserve_sats(100) == 2
    assert lightning_wallet.fee_reserve_sats(250) == 3
    assert lightning_wallet.fee_reserve_sats(10_000) == 100
    assert lightning_wallet.fee_reserve_sats(60_500) == 605


class _R:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = str(payload)

    def json(self):
        return self._p


def test_fee_reserve_shortfall_is_warned_the_day_before_and_reads_add_sats(
        monkeypatch, clock, alerts):
    """LNbits (1.x over HTTP): a wallet that holds the pass price but not the
    price + its fee reserve is refused 'You must reserve at least ...'."""
    feed = Feed(sats=60_500)
    balance = {"sats": 121_500}          # two passes + 500: short on day 2
    paid = set()

    def lget(url, headers=None, timeout=None, params=None):
        if url.endswith("/api/v1/wallet"):
            return _R(200, {"balance": balance["sats"] * 1000})
        if "/api/v1/payments/" in url:
            h = url.rsplit("/", 1)[1]
            if h in paid:
                return _R(200, {"paid": True, "preimage": "ab" * 32, "status": "success"})
            return _R(404, {"detail": "Payment does not exist."})
        return feed.get(url, params=params, headers=headers, timeout=timeout)

    def lpost(url, headers=None, json=None, timeout=None):
        amt = agenthc.bolt11_amount_msat(json["bolt11"]) // 1000
        reserve = max(2, amt // 100)
        if balance["sats"] < amt + reserve:
            return _R(520, {"detail": f"You must reserve at least ({reserve}  sat) "
                                      "to cover potential routing fees.",
                            "status": "failed"})
        balance["sats"] -= amt
        paid.add(agenthc.bolt11_payment_hash(json["bolt11"]))
        return _R(201, {"payment_hash": agenthc.bolt11_payment_hash(json["bolt11"]),
                        "status": "success"})
    monkeypatch.setattr(lightning_wallet.requests, "get", lget)
    monkeypatch.setattr(agenthc.requests, "get", lget)
    monkeypatch.setattr(lightning_wallet.requests, "post", lpost)
    monkeypatch.setattr(lightning_wallet.time, "sleep", lambda s: None)
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w.example",
           "lnbits_admin_key": "k", "max_autopay_sats": 70_000}
    state = {"positions": {}}
    agenthc.poll(cfg, state, lambda s: None)            # day 1: pays
    assert balance["sats"] == 61_000                    # < 60,500 + 605
    assert state.get("daypass_alert") == "daypass_low_balance_notified"
    assert len(alerts) == 1 and "61,000 sats left" in alerts[0]
    assert "about 61,105 sats" in alerts[0]
    clock.tick(days=1)                                  # Thursday 10:30
    with pytest.raises(RuntimeError):
        agenthc.poll(cfg, state, lambda s: None)
    assert balance["sats"] == 61_000                    # nothing paid
    assert state["daypass_retry_why"] == "day_pass_short"
    assert agenthc.exit_loss_reason() == "day_pass_short"
    assert "does not hold enough sats" in state["pass_notice"]
    assert "did not make the payment" not in state["pass_notice"]


def test_wallet_setup_says_low_when_only_the_fee_reserve_is_missing(
        monkeypatch, capsys):
    monkeypatch.setattr(lightning_wallet.LNbitsWallet, "balance_sats",
                        lambda self: 60_500)
    monkeypatch.setattr(agenthc, "day_pass_price",
                        lambda: {"usd": 50, "sats": 60_500})
    answers = iter(["2", "https://w.example", "k", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    cfg = lightning_wallet.wallet_setup({})
    assert cfg["lnbits_admin_key"] == "k"
    assert "Balance looks low" in capsys.readouterr().out


# ── review fixups ──────────────────────────────────────────────────────────

def test_key_balance_notice_reaches_the_dashboard_of_a_key_user():
    """A key user with no Discord/ntfy/Telegram still sees 'top up your key'
    on the dashboard; other pass notices stay hidden with a key."""
    import view_model as vm
    cfg = {"source": "agenthc", "agenthc_api_key": "k"}
    signals = {"pass": {"kind": "subscription", "status": "active"},
               "entry_access": False}
    state = {"pass_notice": agenthc.KEY_BALANCE_EMPTY_TEXT}
    note = vm._pass_notice(cfg, state, signals)
    assert note and note["code"] == "pass" and "Top up your key" in note["text"]
    # read with entry access since: the notice is no longer true
    assert vm._pass_notice(cfg, state, {**signals, "entry_access": True}) is None
    # a prepaid-pass lapse notice is still not shown to a key user
    assert vm._pass_notice(cfg, {"pass_notice": agenthc.PASS_RUN_OUT_TEXT},
                           signals) is None


def _doctor_wallet(monkeypatch, bal, day=50_000):
    import agent

    class W:
        def balance_sats(self):
            return bal
    monkeypatch.setattr(agent, "wallet_from_cfg", lambda cfg: W())
    monkeypatch.setattr(agent, "_day_pass_price", lambda: (50.0, day))
    got = []
    agent._feed_access_check({"lnbits_url": "https://w", "lnbits_admin_key": "x"},
                             {}, lambda *a, **k: got.append(a))
    return got[0]


def test_doctor_wallet_funded_counts_the_lnbits_fee_reserve(monkeypatch):
    # exactly one day-pass: LNbits would refuse it ("You must reserve ...")
    cid, ok, label, detail = _doctor_wallet(monkeypatch, 50_000)[:4]
    assert not ok and "below one" in detail and "fee reserve" in detail
    cid, ok, label, detail = _doctor_wallet(monkeypatch, 50_500)[:4]
    assert ok and "~1 more market day" in detail
    # two passes' price is not two passes once each needs its reserve
    assert "~1 more market day" in _doctor_wallet(monkeypatch, 100_000)[3]


def test_funding_hint_suggests_the_day_pass_plus_the_fee_reserve(monkeypatch):
    import webui
    monkeypatch.setattr(agenthc, "_price_cache", {"at": float("inf"),
                                                  "usd": {1: 50.0, 5: 225.0},
                                                  "sats": {1: 50_000, 5: 225_000}})
    monkeypatch.setattr(webui, "_btc_usd", lambda: 100_000.0)
    q = webui._fund_quote(0)
    assert q["day_pass_sats"] == 50_000
    assert q["suggested_sats"] == 50_500
    # funding the suggested amount passes the agent's own low-balance test
    assert q["suggested_sats"] >= 50_000 + lightning_wallet.fee_reserve_sats(50_000)
    assert webui._fund_quote(0, invoice_cap=50_200)["suggested_sats"] == 50_200


def test_entries_refused_while_an_overdue_invoice_cant_be_checked(clock):
    """A wallet day-pass ends before the close and the only prepaid pass is
    an invoice past its lifetime AgentHC hasn't answered for: the renewal
    treats it as unknown (buys nothing), and entries are refused (it may
    never take over, and no exit signal would arrive)."""
    from test_daypass_payments import _et, _running
    close_ts = _et(16, 0).timestamp()
    now = clock.time()
    cfg = {"feed_pass_pending": {"token": TOK}}
    state = {**_running(_et(12, 0)), "pass_status": {agenthc._token_id(TOK): {
        "status": "pending", "first_seen": now - 70 * 60, "checked": now - 69 * 60}}}
    assert agenthc._prepaid_may_take_over(cfg, state) is True      # no purchase
    assert agenthc.entry_cover(cfg, state, close_ts)[0] is False
    # an old time-only "unpaid": same
    state["pass_status"][agenthc._token_id(TOK)] = {
        "status": "unpaid", "first_seen": now - 5 * 3600, "checked": now - 4 * 3600}
    assert agenthc._prepaid_may_take_over(cfg, state) is True
    assert agenthc.entry_cover(cfg, state, close_ts)[0] is False
    # a pass never checked yet still counts as taking over (by design)
    state["pass_status"] = {}
    assert agenthc.entry_cover(cfg, state, close_ts)[0] is True
