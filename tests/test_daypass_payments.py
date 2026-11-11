"""Wallet day-pass payments: what the agent's own wallet may pay, and when.

The money rules under test:
  - the amount is read from the Lightning invoice itself, never from a JSON
    field next to it;
  - one paid day-pass per day unless the owner set max_autopay_sats_per_day;
  - a payment whose outcome is unknown is remembered (saved before paying)
    and reused when the wallet confirms it — never paid for twice;
  - a payment that failed does not use up the day's budget;
  - no pass is bought when the market is closed for the pass's whole life.
"""

import json
import types
from datetime import datetime, timedelta

import pytest

import lightning_wallet
from sources import agenthc_day_trade_ideas as agenthc

_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
HASH_A = "ab" * 32
HASH_B = "cd" * 32
WED_1030 = datetime(2026, 7, 15, 10, 30, tzinfo=agenthc.MARKET_TZ)


def bolt11(sats, payment_hash=HASH_A, expiry=None):
    """Invoice-shaped text for `sats` (None = no amount) carrying
    `payment_hash`: a real amount prefix and a real `p` field (and an `x`
    field when `expiry` seconds are given); the signature is filler (nothing
    here verifies one)."""
    bits = int(payment_hash, 16) << 4
    p_field = "".join(_ALPHABET[(bits >> (5 * i)) & 31] for i in reversed(range(52)))
    amount = "" if sats is None else f"{sats * 10}n"
    x_field = ""
    if expiry is not None:
        groups = []
        while expiry:
            groups.insert(0, expiry & 31)
            expiry >>= 5
        x_field = ("x" + _ALPHABET[len(groups) // 32] + _ALPHABET[len(groups) % 32]
                   + "".join(_ALPHABET[g] for g in groups))
    return "lnbc" + amount + "1" + "q" * 7 + "pp5" + p_field + x_field + "q" * 110


class Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


EVENT = {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-07-17",
         "strike": 600, "type": "C", "occurred_at": "2026-07-15T14:00:00+00:00"}


class Feed:
    """Scripted AgentHC feed: 402 + invoice without a token, events with
    one. `refuse` makes it answer every token with that status."""

    def __init__(self, sats=10_000, claimed="same", refuse=None, expiry=None):
        self.sats, self.claimed, self.refuse = sats, claimed, refuse
        self.gets, self.minted, self.expiry = [], 0, expiry

    def invoice(self):
        self.minted += 1
        return bolt11(self.sats, f"{self.minted:064x}", self.expiry)

    def get(self, url, params=None, headers=None, timeout=None):
        auth = (headers or {}).get("Authorization")
        self.gets.append(auth)
        if auth and not self.refuse:
            return Resp({"events": [EVENT]})
        if auth and self.refuse == 401:
            return Resp({"detail": "invalid token"}, status=401)
        payment = {"payment_request": self.invoice(), "macaroon": f"MAC{self.minted}"}
        if self.claimed == "same":
            payment["amount_sats"] = self.sats
        elif self.claimed != "missing":
            payment["amount_sats"] = self.claimed
        return Resp({"payment": payment}, status=402)


class Wallet:
    """Fake LNbits wallet. `mode`: "ok" pays; "refuse" answers with an error
    and files nothing (e.g. not enough sats); "hang" sends the payment but
    gives no answer in time (it stays pending until `settle()`); "noproof"
    pays — the sats leave — but never has the preimage (the receipt)."""

    def __init__(self, mode="ok"):
        self.mode, self.paid, self.status = mode, [], {}

    def pay_invoice(self, invoice):
        h = agenthc.bolt11_payment_hash(invoice)
        if self.mode == "refuse":
            raise lightning_wallet.PaymentRefused(
                "payment failed HTTP 520: Insufficient balance")
        self.paid.append(invoice)
        if self.mode == "hang":
            self.status[h] = ("pending", "")
            raise lightning_wallet.WalletError(
                "payment sent but preimage not available within 60s")
        if self.mode == "noproof":
            self.status[h] = ("paid_no_proof", "")
            raise lightning_wallet.WalletError(
                "payment sent but preimage not available within 60s")
        self.status[h] = ("paid", "pre" + h[-4:])
        return "pre" + h[-4:]

    def payment_status(self, payment_hash):
        return self.status.get(payment_hash, ("not_found", ""))

    def settle(self):
        for h, (st, _) in list(self.status.items()):
            if st == "pending":
                self.status[h] = ("paid", "pre" + h[-4:])


class Clock:
    """One clock for the module's ET 'now' and its epoch seconds."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now.timestamp()

    def strftime(self, fmt):
        return self.now.strftime(fmt)

    def tick(self, **kw):
        self.now = (self.now + timedelta(**kw)).astimezone(agenthc.MARKET_TZ)


@pytest.fixture
def clock(monkeypatch):
    c = Clock(WED_1030)
    monkeypatch.setattr(agenthc, "_now_et", lambda: c.now)
    monkeypatch.setattr(agenthc, "time",
                        types.SimpleNamespace(time=c.time, strftime=c.strftime))
    return c


@pytest.fixture
def alerts(monkeypatch):
    import notifications
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    return sent


def _setup(monkeypatch, feed, wallet):
    monkeypatch.setattr(agenthc.requests, "get", feed.get)
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)


def _poll(cfg, state, saves=None):
    """One heartbeat's feed read; errors are swallowed the way the agent's
    run loop swallows them ("heartbeat error (will retry)")."""
    save = (lambda s: saves.append(json.loads(json.dumps(s)))) if saves is not None \
        else (lambda s: None)
    try:
        return agenthc.poll(cfg, state, save)
    except RuntimeError:        # the agent's own "did not pay" / feed errors
        return None


CFG = {"day_pass_recurring": True}      # defaults: 30,000-sat caps


# ── P1: the amount comes from the invoice ───────────────────────────────────

def test_bolt11_payment_hash_reads_the_p_field():
    assert agenthc.bolt11_payment_hash(bolt11(9_500, HASH_B)) == HASH_B
    assert agenthc.bolt11_amount_msat(bolt11(9_500, HASH_B)) == 9_500_000
    for junk in (None, "", "lnbc1...", "lnbc95u1" + "q" * 120, 42):
        assert agenthc.bolt11_payment_hash(junk) is None


def test_bolt11_payment_hash_skips_the_fields_before_it():
    """Laid out like the BOLT11 spec's examples: timestamp, a 52-char `s`
    field, the `p` field, a description, then signature + checksum."""
    invoice = ("lnbc20m1" + "pvjluez" + "sp5" + "zyg3" * 12 + "zygs"
               + "pp5" + "qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypq"
               + "dq5xysxxatsyp3k7enxv4js" + "q" * 110)
    assert agenthc.bolt11_payment_hash(invoice) == (
        "0001020304050607080900010203040506070809000102030405060708090102")
    assert agenthc.bolt11_amount_msat(invoice) == 2_000_000_000


@pytest.mark.parametrize("claimed", ["missing", 0, 9_500, "9500", None])
def test_oversized_invoice_is_never_paid_whatever_the_json_says(
        monkeypatch, clock, claimed):
    """A 5,000,000-sat invoice labelled as cheap (or not labelled at all)
    must not be paid under a 30,000-sat cap — on any poll."""
    feed, wallet = Feed(sats=5_000_000, claimed=claimed), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {}
    for _ in range(20):
        _poll(CFG, state)
        state.pop("daypass_retry_at", None)   # as if the retry wait had passed
        state.pop("l402", None)
    assert wallet.paid == []
    assert (state.get("autopay_spent") or {}).get("sats", 0) == 0
    assert feed.minted == 20                  # it did ask, and refused, each time


def test_invoice_with_no_amount_is_refused(monkeypatch, clock):
    feed, wallet = Feed(sats=None, claimed=9_500), Wallet()
    _setup(monkeypatch, feed, wallet)
    with pytest.raises(RuntimeError, match="[Nn]othing was paid"):
        agenthc.poll(CFG, {}, lambda s: None)
    assert wallet.paid == []


def test_invoice_that_disagrees_with_amount_sats_is_refused(monkeypatch, clock):
    """Even under the cap: the two amounts must say the same thing."""
    feed, wallet = Feed(sats=12_000, claimed=9_500), Wallet()
    _setup(monkeypatch, feed, wallet)
    with pytest.raises(RuntimeError, match="12,000"):
        agenthc.poll(CFG, {}, lambda s: None)
    assert wallet.paid == []


def test_invoice_for_a_different_payment_than_the_pass_is_refused(monkeypatch, clock):
    """AgentHC names the payment its pass is tied to. An invoice for some
    other payment would be money spent on nothing."""
    wallet = Wallet()
    body = {"payment": {"payment_request": bolt11(10_000, HASH_A), "macaroon": "MAC",
                        "amount_sats": 10_000, "payment_hash": HASH_B}}
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: Resp(body, 402))
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    with pytest.raises(RuntimeError, match="Nothing was paid"):
        agenthc.poll(CFG, {}, lambda s: None)
    assert wallet.paid == []
    body["payment"]["payment_hash"] = HASH_A.upper()   # the same payment: fine
    state = {}
    _poll(CFG, state)
    assert len(wallet.paid) == 1 and state["l402"]["token"].startswith("L402 MAC:")


def test_daily_cap_counts_the_invoice_amount(monkeypatch, clock):
    """Owner allows 25,000 sats a day: two 12,000-sat passes fit, a third
    does not — counted from the invoices, not from amount_sats."""
    feed, wallet = Feed(sats=12_000, claimed="missing"), Wallet()
    _setup(monkeypatch, feed, wallet)
    cfg = {**CFG, "max_autopay_sats_per_day": 25_000}
    state = {}
    for _ in range(6):
        _poll(cfg, state)
        state.pop("l402", None)
        state.pop("daypass_retry_at", None)
    assert len(wallet.paid) == 2
    assert state["autopay_spent"] == {"day": "2026-07-15", "sats": 24_000, "count": 2}


# ── P2: one paid day-pass a day; an undecided payment is reused ─────────────

def test_wallet_only_owner_pays_for_one_day_pass_a_day(monkeypatch, clock):
    """Default caps, ~10,000-sat passes, a token that keeps getting lost:
    one payment, not three."""
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {}
    for _ in range(20):
        _poll(CFG, state)
        state.pop("l402", None)               # token lost / rejected
        state.pop("daypass_retry_at", None)
        clock.tick(minutes=5)
    assert len(wallet.paid) == 1
    assert state["autopay_spent"]["count"] == 1


def test_purchase_is_saved_before_the_wallet_is_asked_to_pay(monkeypatch, clock):
    feed = Feed(sats=10_000)
    saves = []

    class Checking(Wallet):
        def pay_invoice(self, invoice):
            assert saves, "nothing saved before paying"
            pend = saves[-1]["daypass_pending"]
            assert pend["invoice"] == invoice and pend["macaroon"] == "MAC1"
            assert pend["payment_hash"] == agenthc.bolt11_payment_hash(invoice)
            assert pend["amount_sats"] == 10_000 and pend["created_at"] == clock.time()
            return super().pay_invoice(invoice)

    wallet = Checking()
    _setup(monkeypatch, feed, wallet)
    state = {}
    assert len(agenthc.poll(CFG, state, lambda s: saves.append(
        json.loads(json.dumps(s))))) == 1
    assert len(wallet.paid) == 1
    assert "daypass_pending" not in state     # settled: nothing left pending


def test_payment_that_times_out_is_reused_when_it_settles(monkeypatch, clock, alerts):
    """pay_invoice gives up waiting, the payment settles later: the agent
    must use THAT pass — not buy another, however many polls go by."""
    feed, wallet = Feed(sats=10_000), Wallet(mode="hang")
    _setup(monkeypatch, feed, wallet)
    state, saves = {}, []
    for _ in range(10):                       # 50 minutes undecided
        assert not _poll(CFG, state, saves)
        clock.tick(minutes=5)
    assert len(wallet.paid) == 1 and feed.minted == 1
    assert saves[-1]["daypass_pending"]["macaroon"] == "MAC1"
    assert state["autopay_spent"]["count"] == 1     # holds its place in the budget
    assert len(alerts) == 1 and "will not pay again" in alerts[0]
    wallet.settle()
    events = agenthc.poll(CFG, state, lambda s: None)
    assert len(events) == 1
    assert state["l402"]["token"] == "L402 MAC1:pre" + ("%064x" % 1)[-4:]
    assert feed.gets[-1] == state["l402"]["token"]
    assert len(wallet.paid) == 1 and "daypass_pending" not in state
    assert state["autopay_spent"] == {"day": "2026-07-15", "sats": 10_000, "count": 1}


def test_crash_between_paying_and_saving_does_not_buy_twice(monkeypatch, clock):
    """The agent dies right after the wallet paid. On restart it reads the
    saved purchase, asks the wallet, and uses the pass it already paid for."""
    class Crash(BaseException):
        pass

    class Dying(Wallet):
        def pay_invoice(self, invoice):
            super().pay_invoice(invoice)
            raise Crash()

    feed, wallet = Feed(sats=10_000), Dying()
    _setup(monkeypatch, feed, wallet)
    saves = []
    with pytest.raises(Crash):
        agenthc.poll(CFG, {}, lambda s: saves.append(json.loads(json.dumps(s))))
    restarted = saves[-1]                      # what was on disk
    assert "l402" not in restarted
    clock.tick(minutes=2)
    events = agenthc.poll(CFG, restarted, lambda s: None)
    assert len(events) == 1 and len(wallet.paid) == 1 and feed.minted == 1
    assert restarted["l402"]["token"].startswith("L402 MAC1:pre")
    assert restarted["autopay_spent"]["count"] == 1


@pytest.mark.parametrize("status", [401, 402])
def test_refused_pass_paid_today_alerts_and_never_buys_again(
        monkeypatch, clock, alerts, status):
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {}
    assert len(agenthc.poll(CFG, state, lambda s: None)) == 1   # bought, works
    token = state["l402"]["token"]
    feed.refuse = status                       # AgentHC now refuses that pass
    for _ in range(40):                        # 20 minutes of 30 s polls
        assert _poll(CFG, state) == []
        clock.tick(seconds=30)
    assert len(wallet.paid) == 1
    assert state["l402"]["token"] == token     # kept: it is paid for
    assert len(alerts) == 1
    assert "refused" in alerts[0] and "NOT buy another" in alerts[0]
    assert state["pass_notice"] == alerts[0]   # and on the dashboard
    # it is offered again every couple of minutes, not on every poll …
    assert 8 <= feed.gets.count(token) - 1 <= 11
    feed.refuse = None                         # … and used again once accepted
    clock.tick(minutes=3)
    assert len(agenthc.poll(CFG, state, lambda s: None)) == 1
    assert len(wallet.paid) == 1
    assert "pass_notice" not in state and "refused_at" not in state["l402"]


def test_owner_who_allowed_more_sats_a_day_still_gets_a_second_pass(
        monkeypatch, clock):
    """max_autopay_sats_per_day set by the owner = "pay again rather than go
    without", up to that many sats (two 10,000-sat passes fit in 25,000)."""
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    cfg = {**CFG, "max_autopay_sats_per_day": 25_000}
    state = {}
    assert len(agenthc.poll(cfg, state, lambda s: None)) == 1
    feed.refuse = 402
    for _ in range(12):
        _poll(cfg, state)
        state.pop("daypass_retry_at", None)
        clock.tick(minutes=5)
    assert len(wallet.paid) == 2
    assert state["autopay_spent"] == {"day": "2026-07-15", "sats": 20_000, "count": 2}


def test_refused_pass_from_yesterday_is_replaced(monkeypatch, clock):
    """A pass bought yesterday afternoon and refused this morning: today is
    not paid for yet, so today's pass is bought (once)."""
    feed, wallet = Feed(sats=10_000, refuse=401), Wallet()
    _setup(monkeypatch, feed, wallet)
    bought = datetime(2026, 7, 14, 15, 0, tzinfo=agenthc.MARKET_TZ).timestamp()
    state = {"l402": {"token": "L402 OLD:pre", "expires_at": bought + 23 * 3600}}
    for _ in range(4):
        _poll(CFG, state)
        clock.tick(minutes=5)
    assert len(wallet.paid) == 1
    assert state["l402"]["token"].startswith("L402 MAC")


# ── P3: a payment that failed costs nothing ─────────────────────────────────

def test_unfunded_wallet_does_not_use_up_the_days_budget(monkeypatch, clock, alerts):
    feed, wallet = Feed(sats=10_000), Wallet(mode="refuse")
    _setup(monkeypatch, feed, wallet)
    state = {}
    for _ in range(5):                         # five tries, nothing paid
        assert not _poll(CFG, state)
        assert state["daypass_retry_at"] > clock.time()
        clock.tick(minutes=6)
    assert wallet.paid == []
    assert (state.get("autopay_spent") or {}).get("sats", 0) == 0
    assert "daypass_pending" not in state
    assert len(alerts) == 1 and "Nothing was paid" in alerts[0]
    wallet.mode = "ok"                         # the owner adds sats
    events = agenthc.poll(CFG, state, lambda s: None)
    assert len(events) == 1 and len(wallet.paid) == 1
    assert state["autopay_spent"] == {"day": "2026-07-15", "sats": 10_000, "count": 1}


def test_failed_purchase_waits_before_asking_for_another_invoice(monkeypatch, clock):
    """Each feed request without a pass makes AgentHC create an invoice: a
    wallet that can't pay must not cause one on every poll."""
    feed, wallet = Feed(sats=10_000), Wallet(mode="refuse")
    _setup(monkeypatch, feed, wallet)
    state = {}
    for _ in range(9):                         # 4.5 minutes of 30 s polls
        _poll(CFG, state)
        clock.tick(seconds=30)
    assert feed.minted == 1
    clock.tick(minutes=1)
    _poll(CFG, state)
    assert feed.minted == 2


def test_wallet_with_no_record_after_a_lost_answer_waits_for_the_invoice_to_expire(
        monkeypatch, clock, alerts):
    """No answer from the wallet and no record of the payment: the request
    may be stuck on its way and still go through. Only once the invoice can
    no longer be paid (1 h, plus 5 min for clocks) is "no record" the same
    as "not paid" — until then nothing else is bought."""
    class Lost(Wallet):
        def pay_invoice(self, invoice):
            raise lightning_wallet.WalletError("no answer from the wallet while paying")

    feed, wallet = Feed(sats=10_000), Lost()
    _setup(monkeypatch, feed, wallet)
    state = {}
    assert _poll(CFG, state) == []
    assert state["daypass_pending"]["payment_hash"] and state["autopay_spent"]["count"] == 1
    wallet.pay_invoice = lambda invoice: Wallet.pay_invoice(wallet, invoice)
    for _ in range(12):                        # an hour of polls: still waiting
        clock.tick(minutes=5)
        assert _poll(CFG, state) == [] and "daypass_pending" in state
    assert wallet.paid == [] and feed.minted == 1
    assert len(alerts) == 1 and "once the invoice has expired" in alerts[0]
    assert "the agent gets no new trades" in alerts[0]
    assert "position" not in alerts[0]
    clock.tick(minutes=6)                      # 66 minutes: the invoice is dead
    assert len(_poll(CFG, state)) == 1         # released, bought for real
    assert len(wallet.paid) == 1 and feed.minted == 2
    assert state["autopay_spent"] == {"day": "2026-07-15", "sats": 10_000, "count": 1}
    assert "pass_notice" not in state          # the alert is off the dashboard


def test_longer_lived_invoice_is_waited_out_in_full(monkeypatch, clock):
    """The wait follows the invoice's own expiry when it is longer than 1 h."""
    class Lost(Wallet):
        def pay_invoice(self, invoice):
            raise lightning_wallet.WalletError("no answer from the wallet while paying")

    feed, wallet = Feed(sats=10_000, expiry=3 * 3600), Lost()
    _setup(monkeypatch, feed, wallet)
    state = {}
    assert _poll(CFG, state) == []
    clock.tick(hours=3)
    assert _poll(CFG, state) == [] and "daypass_pending" in state
    clock.tick(minutes=6)
    wallet.pay_invoice = lambda invoice: Wallet.pay_invoice(wallet, invoice)
    assert len(_poll(CFG, state)) == 1 and feed.minted == 2


def test_bolt11_expiry_reads_the_x_field():
    assert agenthc.bolt11_expiry_s(bolt11(10_000)) == 3600          # the default
    assert agenthc.bolt11_expiry_s(bolt11(10_000, expiry=600)) == 600
    assert agenthc.bolt11_expiry_s(bolt11(10_000, expiry=3 * 3600)) == 10_800
    assert agenthc.bolt11_payment_hash(bolt11(10_000, HASH_B, expiry=7200)) == HASH_B
    for junk in (None, "", "not an invoice", 12):
        assert agenthc.bolt11_expiry_s(junk) is None


def test_pay_request_that_never_left_is_not_waited_for(monkeypatch, clock, alerts):
    """The wallet could not even be connected to: nothing was sent, so there
    is nothing to wait for — the agent says so and tries again in 5 minutes."""
    class Offline(Wallet):
        def pay_invoice(self, invoice):
            raise lightning_wallet.PaymentNotSent(
                "could not connect to the wallet (HTTPSConnectionPool(...)) — "
                "nothing was sent")

        def payment_status(self, payment_hash):
            return "unknown", ""               # it can't be asked either

    feed, wallet = Feed(sats=10_000), Offline()
    _setup(monkeypatch, feed, wallet)
    state = {}
    assert not _poll(CFG, state)
    assert "daypass_pending" not in state
    assert (state.get("autopay_spent") or {}).get("count", 0) == 0
    assert len(alerts) == 1 and "could not be reached" in alerts[0]
    assert "HTTPSConnectionPool" not in alerts[0]
    clock.tick(minutes=6)
    wallet.pay_invoice = lambda invoice: Wallet.pay_invoice(wallet, invoice)
    wallet.payment_status = lambda h: Wallet.payment_status(wallet, h)
    assert len(_poll(CFG, state)) == 1 and len(wallet.paid) == 1


# ── review: only the wallet that was asked to pay is believed ───────────────

WALLET_A = {"lnbits_url": "https://lnbits.example", "lnbits_admin_key": "key-a" * 6}
WALLET_B = {"lnbits_url": "https://lnbits.example", "lnbits_admin_key": "key-b" * 6}


def test_wallet_changed_while_a_payment_is_undecided_never_pays_twice(
        monkeypatch, clock, alerts):
    """Wallet A's payment hangs; the owner replaces the wallet. Wallet B has
    never heard of that payment — its "no such payment" must not be taken as
    "not paid", or B pays for a second pass while A's can still go through."""
    feed, wallet_a, wallet_b = Feed(sats=10_000), Wallet(mode="hang"), Wallet()
    by_key = {WALLET_A["lnbits_admin_key"]: wallet_a,
              WALLET_B["lnbits_admin_key"]: wallet_b}
    asked_b = []
    wallet_b.payment_status = lambda h: (asked_b.append(h), ("not_found", ""))[1]
    monkeypatch.setattr(agenthc.requests, "get", feed.get)
    monkeypatch.setattr(agenthc, "wallet_from_cfg",
                        lambda cfg: by_key[cfg["lnbits_admin_key"]])
    cfg, state, saves = dict(CFG, **WALLET_A), {}, []
    assert not _poll(cfg, state, saves)
    saved = saves[-1]["daypass_pending"]
    assert saved["wallet"] == lightning_wallet.wallet_fingerprint(cfg)
    assert WALLET_A["lnbits_admin_key"] not in json.dumps(saved)   # an id, not the key
    cfg = dict(CFG, **WALLET_B)                # the owner replaced the wallet
    for _ in range(40):                        # the rest of the session and more
        clock.tick(minutes=5)
        assert not _poll(cfg, state)
    assert wallet_b.paid == [] and asked_b == [] and feed.minted == 1
    assert state["daypass_pending"]["macaroon"] == "MAC1"   # A's pass is kept
    assert len(alerts) == 1 and "wallet was changed" in alerts[0]
    assert "the agent gets no new trades" in alerts[0]
    assert "position" not in alerts[0]
    wallet_a.settle()                          # A's payment went through after all
    cfg = dict(CFG, **WALLET_A)                # …and the owner switched back
    assert len(agenthc.poll(cfg, state, lambda s: None)) == 1
    assert state["l402"]["token"].startswith("L402 MAC1:pre")
    assert len(wallet_a.paid) == 1 and wallet_b.paid == []


def test_wallet_fingerprint_names_the_wallet_without_its_key():
    fp = lightning_wallet.wallet_fingerprint
    assert fp({}) == "" and fp({"lnbits_url": "https://x"}) == ""
    a, b = fp(WALLET_A), fp(WALLET_B)
    assert a and b and a != b and len(a) == 16
    assert fp(dict(WALLET_A, lnbits_url="https://lnbits.example/")) == a
    assert fp(dict(WALLET_A, lnbits_url="https://other.example")) != a
    assert "key-a" not in a


# ── review: a wallet that pays but has no receipt ───────────────────────────

def test_wallet_that_pays_without_a_receipt_is_not_used_again(
        monkeypatch, clock, alerts):
    """The wallet says "paid" but can never give the preimage: the sats are
    gone and no pass comes of it. Paying again every market day would lose
    the same sats each time — so the agent stops and tells the owner what
    really happened."""
    feed, wallet = Feed(sats=10_000), Wallet(mode="noproof")
    _setup(monkeypatch, feed, wallet)
    cfg, state = dict(CFG, **WALLET_A), {}
    for _ in range(3 * 24 * 12):               # Wed 10:30 → Sat 10:30, every 5 min
        assert not _poll(cfg, state)
        clock.tick(minutes=5)
    assert len(wallet.paid) == 1 and feed.minted == 1
    assert "daypass_pending" not in state
    assert len(alerts) == 3                    # Wed, then a reminder Thu and Fri
    for text in alerts:
        assert "sats left the wallet" in text and "NOT buy another" in text
        assert "10,000 sats" in text and "agent gets no new trades" in text
        assert "position" not in text
        assert "has not said yet" not in text
    assert state["pass_notice"] == alerts[0]   # and it stays on the dashboard


def test_no_receipt_stop_ends_when_recurring_is_switched_on_again(
        monkeypatch, clock, alerts):
    feed, wallet = Feed(sats=10_000), Wallet(mode="noproof")
    _setup(monkeypatch, feed, wallet)
    cfg, state = dict(CFG, **WALLET_A), {}
    for _ in range(4):                         # 10:30 … 10:45
        _poll(cfg, state)
        clock.tick(minutes=5)
    assert state["daypass_no_receipt"]["sats"] == 10_000
    clock.tick(days=1)                         # Thursday: still stopped
    assert not _poll(cfg, state) and len(wallet.paid) == 1
    # the wallet was fixed; the owner switches Recurring day-pass off and on
    wallet.mode = "ok"
    cfg["day_pass_recurring_on_at"] = clock.time()
    assert len(_poll(cfg, state)) == 1 and len(wallet.paid) == 2
    assert "daypass_no_receipt" not in state and "pass_notice" not in state


def test_no_receipt_stop_ends_when_a_different_wallet_is_connected(
        monkeypatch, clock, alerts):
    feed, wallet, other = Feed(sats=10_000), Wallet(mode="noproof"), Wallet()
    by_key = {WALLET_A["lnbits_admin_key"]: wallet, WALLET_B["lnbits_admin_key"]: other}
    monkeypatch.setattr(agenthc.requests, "get", feed.get)
    monkeypatch.setattr(agenthc, "wallet_from_cfg",
                        lambda cfg: by_key[cfg["lnbits_admin_key"]])
    cfg, state = dict(CFG, **WALLET_A), {}
    for _ in range(4):
        _poll(cfg, state)
        clock.tick(minutes=5)
    assert "daypass_no_receipt" in state
    cfg = dict(CFG, **WALLET_B)
    assert not _poll(cfg, state)               # today was paid for (by A): not twice
    assert other.paid == []
    clock.tick(days=1)
    assert len(_poll(cfg, state)) == 1 and len(other.paid) == 1
    assert len(wallet.paid) == 1 and "daypass_no_receipt" not in state


# ── review: every refusal reaches the owner, in plain words ─────────────────

def _bad_invoice_feed(kind):
    if kind == "amounts differ":
        return Feed(sats=12_000, claimed=9_500), "asks for 12,000 sats"
    if kind == "no amount":
        return Feed(sats=None, claimed=9_500), "no readable amount"
    if kind == "over the cap":
        return Feed(sats=45_000), "more than your auto-pay cap for one pass (30,000 sats)"
    feed = Feed(sats=10_000)
    real = feed.get

    def get(url, params=None, headers=None, timeout=None):
        resp = real(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 402:
            if kind == "another payment":
                resp.json()["payment"]["payment_hash"] = HASH_B
            else:                               # "no invoice"
                del resp.json()["payment"]["payment_request"]
        return resp
    feed.get = get
    return feed, ("not the one its day-pass belongs to" if kind == "another payment"
                  else "did not send a Lightning invoice")


@pytest.mark.parametrize("kind", ["amounts differ", "no amount", "over the cap",
                                  "another payment", "no invoice"])
def test_invoice_the_agent_refuses_is_reported_to_the_owner_once_a_day(
        monkeypatch, clock, alerts, kind):
    """A refused invoice means no pass, so no signals — including exits. The
    owner must hear about it, not just the console."""
    feed, words = _bad_invoice_feed(kind)
    wallet = Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {}
    for _ in range(20):                        # 10:30 → 12:10
        assert not _poll(CFG, state)
        clock.tick(minutes=5)
    assert wallet.paid == []
    assert len(alerts) == 1
    text = alerts[0]
    assert text.startswith("Your agent did not buy today's day-pass.")
    assert words in text and "Nothing was paid" in text
    assert "the agent gets no new trades" in text
    assert "position" not in text and "watch" not in text
    assert "8:30 AM ET on the next market day" in text
    assert "{" not in text                     # no raw server text
    assert state["pass_notice"] == text        # on the dashboard too
    clock.tick(days=1)
    assert not _poll(CFG, state)
    assert len(alerts) == 2                    # once more the next day


def test_failed_payment_alert_says_what_happens_next_in_plain_words(
        monkeypatch, clock, alerts, capsys):
    """An unfunded wallet shortly before the close: the alert must not
    promise "every 5 minutes" (buying stops at the close) and must not carry
    the wallet's raw error."""
    clock.now = datetime(2026, 7, 15, 15, 58, tzinfo=agenthc.MARKET_TZ)

    class Unfunded(Wallet):
        def pay_invoice(self, invoice):
            raise lightning_wallet.PaymentRefused(
                'payment failed HTTP 520: {"detail":"Insufficient balance."}')

    feed, wallet = Feed(sats=10_000), Unfunded()
    _setup(monkeypatch, feed, wallet)
    state = {}
    with pytest.raises(RuntimeError, match="HTTP 520"):   # the console keeps the detail
        agenthc.poll(CFG, state, lambda s: None)
    assert len(alerts) == 1
    text = alerts[0]
    assert "does not hold enough sats" in text and "Nothing was paid" in text
    assert "HTTP" not in text and "{" not in text and "detail" not in text
    assert "every 5 minutes until today's market close" in text
    assert "from 8:30 AM ET on the next market day" in text
    assert "the agent gets no new trades" in text
    assert "position" not in text and "watch" not in text
    # and that is what it does: funded at 4:10 PM, nothing is bought tonight
    wallet.pay_invoice = lambda invoice: Wallet.pay_invoice(wallet, invoice)
    clock.now = datetime(2026, 7, 15, 16, 10, tzinfo=agenthc.MARKET_TZ)
    assert not _poll(CFG, state) and wallet.paid == []
    clock.now = datetime(2026, 7, 16, 8, 31, tzinfo=agenthc.MARKET_TZ)
    assert len(_poll(CFG, state)) == 1 and len(wallet.paid) == 1


def test_failed_payment_alert_does_not_blame_the_balance_for_other_errors(
        monkeypatch, clock, alerts):
    class Broken(Wallet):
        def pay_invoice(self, invoice):
            raise lightning_wallet.PaymentRefused(
                'payment failed HTTP 520: {"detail":"Payment failed: no route"}')

    feed, wallet = Feed(sats=10_000), Broken()
    _setup(monkeypatch, feed, wallet)
    assert not _poll(CFG, {})
    assert "the wallet did not make the payment" in alerts[0]
    assert "enough sats" not in alerts[0] and "HTTP" not in alerts[0]


def test_wallet_that_no_longer_exists_is_reported_and_not_waited_for(
        monkeypatch, clock, alerts):
    """A reset test server: the wallet's key is no longer accepted, and the
    server answers every question about the payment with the same refusal.
    Nothing was paid — the agent must not sit "undecided" for a day."""
    class Gone(Wallet):
        def pay_invoice(self, invoice):
            raise lightning_wallet.WalletKeyRejected(
                "the wallet's server did not accept the request, HTTP 404: "
                '{"detail":"Wallet not found."}')

        def payment_status(self, payment_hash):
            return "unknown", ""               # a 404 that isn't about the payment

    feed, wallet = Feed(sats=10_000), Gone()
    _setup(monkeypatch, feed, wallet)
    state = {}
    assert not _poll(CFG, state)
    assert "daypass_pending" not in state
    assert (state.get("autopay_spent") or {}).get("count", 0) == 0
    assert len(alerts) == 1 and "did not accept the agent's wallet key" in alerts[0]
    assert "may no longer exist" in alerts[0] and "Nothing was paid" in alerts[0]
    assert "HTTP" not in alerts[0] and "{" not in alerts[0]


# ── P4: never buy a pass the market is closed for ───────────────────────────

@pytest.mark.parametrize("when, may_buy", [
    ((2026, 7, 17, 19, 0), False),    # Friday evening: covers no session
    ((2026, 7, 18, 12, 0), False),    # Saturday
    ((2026, 7, 19, 20, 0), False),    # Sunday night
    ((2026, 7, 20, 0, 5), False),     # Monday just after midnight: wait
    ((2026, 7, 20, 8, 29), False),
    ((2026, 7, 20, 8, 30), True),     # an hour before the open
    ((2026, 7, 20, 15, 59), True),
    ((2026, 7, 20, 16, 0), False),    # closed for the day
    ((2026, 11, 25, 19, 0), False),   # evening before Thanksgiving
    ((2026, 11, 26, 10, 0), False),   # Thanksgiving: market holiday
    ((2026, 11, 27, 12, 0), True),    # day after: open until 1 PM
    ((2026, 11, 27, 13, 30), False),  # early close has passed
    ((2026, 12, 25, 11, 0), False),   # Christmas (a Friday)
])
def test_day_pass_is_only_bought_when_it_covers_market_time(
        monkeypatch, clock, when, may_buy):
    clock.now = datetime(*when, tzinfo=agenthc.MARKET_TZ)
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    events = agenthc.poll(CFG, {}, lambda s: None)
    assert (len(wallet.paid) == 1) is may_buy
    if not may_buy:
        assert events == [] and feed.gets == []   # no request, no invoice made


def test_owner_listed_market_holiday_is_respected(monkeypatch, clock):
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    cfg = {**CFG, "market_holidays": ["2026-07-15"]}
    assert agenthc.poll(cfg, {}, lambda s: None) == []
    assert wallet.paid == []


def test_two_weeks_of_polling_buy_one_pass_per_market_day(monkeypatch, clock):
    """Start Friday after the close, poll every 5 minutes for two weeks
    (10 sessions, no holidays): exactly 10 passes, every one bought on a
    market day between 8:30 AM ET and the close."""
    clock.now = datetime(2026, 7, 10, 16, 30, tzinfo=agenthc.MARKET_TZ)
    feed, wallet = Feed(sats=10_000), Wallet()
    bought_at = []
    real_pay = wallet.pay_invoice

    def pay(invoice):
        bought_at.append(clock.now)
        return real_pay(invoice)
    wallet.pay_invoice = pay
    _setup(monkeypatch, feed, wallet)
    state = {}
    end = datetime(2026, 7, 24, 16, 30, tzinfo=agenthc.MARKET_TZ)
    while clock.now < end:
        agenthc.poll(CFG, state, lambda s: None)
        clock.tick(minutes=5)
    assert len(bought_at) == 10
    assert sorted({t.date().isoformat() for t in bought_at}) == [
        "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
        "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"]
    assert all(8 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60 for t in bought_at)


def test_closed_market_wait_is_explained_once_a_day(monkeypatch, clock, capsys):
    clock.now = datetime(2026, 7, 17, 19, 0, tzinfo=agenthc.MARKET_TZ)   # Fri
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {}
    for _ in range(3):
        agenthc.poll(CFG, state, lambda s: None)
    out = capsys.readouterr().out
    assert out.count("market is closed") == 1
    assert "Monday Jul 20" in out and "8:30 AM ET" in out


# ── an owner upgrading with a running pass ──────────────────────────────────

def test_running_pass_from_the_previous_version_keeps_working(monkeypatch, clock):
    """state.json written by the release before this one: the token is used
    as it is, nothing is bought, nothing is charged."""
    feed, wallet = Feed(sats=10_000), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {"l402": {"token": "L402 OLD:pre", "expires_at": clock.time() + 3600},
             "autopay_spent": {"day": "2026-07-15", "sats": 10_000, "count": 1}}
    assert len(agenthc.poll(CFG, state, lambda s: None)) == 1
    assert wallet.paid == [] and feed.gets == ["L402 OLD:pre"]


# ── the wallet's own answers (LNbits REST shapes) ───────────────────────────

class _Http:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("resp, expected", [
    (_Http(200, {"paid": True, "preimage": "ab" * 32}), ("paid", "ab" * 32)),
    (_Http(200, {"paid": True, "details": {"preimage": "cd" * 32}}), ("paid", "cd" * 32)),
    (_Http(200, {"paid": True, "preimage": "0" * 64}), ("paid_no_proof", "")),
    (_Http(200, {"paid": True}), ("paid_no_proof", "")),
    (_Http(200, {"details": {"paid": True, "preimage": ""}}), ("paid_no_proof", "")),
    (_Http(200, {"paid": False, "status": "pending"}), ("pending", "")),
    (_Http(200, {"paid": False}), ("pending", "")),
    (_Http(200, {"paid": False, "status": "failed"}), ("failed", "")),
    (_Http(200, {"paid": False, "details": {"status": "PaymentState.FAILED"}}), ("failed", "")),
    (_Http(404, {"detail": "Payment does not exist."}), ("not_found", "")),
    # a 404 that is not LNbits' own answer says nothing about the payment
    (_Http(404, None, "<html><h1>404 Not Found</h1>nginx</html>"), ("unknown", "")),
    (_Http(404, {"detail": "Not Found"}), ("unknown", "")),
    (_Http(404, {"message": "no such app"}), ("unknown", "")),
    (_Http(404, ["odd"]), ("unknown", "")),
    (_Http(500, None, "boom"), ("unknown", "")),
    (_Http(200, None, "<html>"), ("unknown", "")),
    (_Http(200, ["odd"]), ("unknown", "")),
])
def test_wallet_payment_status_reads_lnbits_answers(monkeypatch, resp, expected):
    seen = []

    def get(url, headers=None, timeout=None):
        seen.append((url, headers["X-Api-Key"]))
        return resp
    monkeypatch.setattr(lightning_wallet.requests, "get", get)
    wallet = lightning_wallet.LNbitsWallet("https://lnbits.example/", "adminkey")
    assert wallet.payment_status(HASH_A) == expected
    assert seen == [(f"https://lnbits.example/api/v1/payments/{HASH_A}", "adminkey")]


def test_wallet_payment_status_never_raises_when_the_wallet_is_unreachable(monkeypatch):
    def get(*a, **kw):
        raise lightning_wallet.requests.ConnectionError("no route")
    monkeypatch.setattr(lightning_wallet.requests, "get", get)
    wallet = lightning_wallet.LNbitsWallet("https://lnbits.example", "k")
    assert wallet.payment_status(HASH_A) == ("unknown", "")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_wallet_key_that_is_not_accepted_means_nothing_was_sent(monkeypatch, status):
    wallet = lightning_wallet.LNbitsWallet("https://lnbits.example", "k")
    monkeypatch.setattr(
        lightning_wallet.requests, "post",
        lambda *a, **kw: _Http(status, None, '{"detail":"Wallet not found."}'))
    with pytest.raises(lightning_wallet.WalletKeyRejected, match="Wallet not found"):
        wallet.pay_invoice(bolt11(10_000))
    assert issubclass(lightning_wallet.WalletKeyRejected, lightning_wallet.PaymentNotSent)
    assert not issubclass(lightning_wallet.WalletKeyRejected,
                          lightning_wallet.PaymentRefused)


def test_wallet_says_refused_only_when_it_answered_no(monkeypatch):
    wallet = lightning_wallet.LNbitsWallet("https://lnbits.example", "k")
    monkeypatch.setattr(lightning_wallet.requests, "post",
                        lambda *a, **kw: _Http(520, None, "Insufficient balance"))
    with pytest.raises(lightning_wallet.PaymentRefused, match="Insufficient balance"):
        wallet.pay_invoice(bolt11(10_000))

    def timeout(*a, **kw):
        raise lightning_wallet.requests.Timeout("read timed out")
    monkeypatch.setattr(lightning_wallet.requests, "post", timeout)
    with pytest.raises(lightning_wallet.WalletError) as err:
        wallet.pay_invoice(bolt11(10_000))
    assert not isinstance(err.value, lightning_wallet.PaymentRefused)
    assert "may or may not have been sent" in str(err.value)


def _connect_error(reason_cls, *args):
    """A requests error shaped the way requests raises it: the urllib3
    reason is wrapped in MaxRetryError, wrapped in ConnectionError."""
    from urllib3 import exceptions as u3
    reason = getattr(u3, reason_cls)(*args)
    return lightning_wallet.requests.ConnectionError(
        u3.MaxRetryError(None, "/api/v1/payments", reason))


@pytest.mark.parametrize("exc, never_left", [
    (_connect_error("NewConnectionError", None, "connection refused"), True),
    (_connect_error("ConnectTimeoutError", None, "connect timed out"), True),
    (lightning_wallet.requests.ConnectTimeout("connect timed out"), True),
    # sent, then no answer: the wallet may be paying
    (lightning_wallet.requests.ReadTimeout("read timed out"), False),
    (lightning_wallet.requests.ConnectionError(
        "Connection aborted.", ConnectionResetError(104, "reset by peer")), False),
    (_connect_error("ProtocolError", "Connection aborted."), False),
])
def test_wallet_says_not_sent_only_when_the_request_never_left(
        monkeypatch, exc, never_left):
    def post(*a, **kw):
        raise exc
    monkeypatch.setattr(lightning_wallet.requests, "post", post)
    wallet = lightning_wallet.LNbitsWallet("https://lnbits.example", "k")
    with pytest.raises(lightning_wallet.WalletError) as err:
        wallet.pay_invoice(bolt11(10_000))
    assert isinstance(err.value, lightning_wallet.PaymentNotSent) is never_left
    assert not isinstance(err.value, lightning_wallet.PaymentRefused)
    assert ("nothing was sent" in str(err.value)) is never_left
