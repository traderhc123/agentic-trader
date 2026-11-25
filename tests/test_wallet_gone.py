"""The agent's LNbits wallet when its server no longer knows it (a public
test server was reset, or the wallet's keys were changed), can't be reached,
or holds too little — 2026-11-25: an owner's "add sats" failed with
`invoice creation failed HTTP 404: {"detail":"Wallet not found."}`, and his
agent let AgentHC make a day-pass invoice every 5 minutes that it could
never pay.

  - every wallet call (balance, funding invoice, pay, payment status) tells
    "gone" (WalletGone) from "out of reach" (WalletUnreachable) from other
    failures, with the owner's words and never a web page's HTML;
  - the dashboard, the setup page, the classic page and the CLI say it the
    same way and offer the one step that fixes it (a new wallet, the old
    one's details saved to a file first);
  - no AgentHC day-pass invoice is asked for while the wallet can't pay
    (gone, out of reach, or short of the price plus LNbits' reserve), and a
    payment already started is never decided "failed" on a gone wallet;
  - the owner hears about a gone wallet at agent start, not the morning a
    purchase fails;
  - the funding amounts count LNbits' reserve and the server's service fee.
Hermetic: no wallet server, AgentHC or network."""

import json
import time as _time
from datetime import datetime

import pytest

import agent
import lightning_wallet
import webui
from sources import agenthc_day_trade_ideas as agenthc

from test_daypass_payments import (  # noqa: F401  (fixtures)
    Feed, Wallet, _poll, alerts, clock)
from test_v2_settings_api import BASE, app, read_cfg, write_cfg  # noqa: F401
from test_v2_parity import _running

DEMO = "https://demo.lnbits.com"
KEY = "fakeADMINkey0000aaaa"
GONE_BODY = {"detail": "Wallet not found."}


class R:
    """A requests.Response stand-in: JSON `body`, or `text` (a web page)."""

    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def _server(monkeypatch, get=None, post=None):
    """The wallet's server, answered here; returns what was asked."""
    asked = []

    def g(url, headers=None, timeout=None, **kw):
        asked.append(("GET", url, None))
        return get(url) if callable(get) else get

    def p(url, headers=None, json=None, timeout=None, **kw):
        asked.append(("POST", url, json))
        return post(url, json) if callable(post) else post
    monkeypatch.setattr(lightning_wallet.requests, "get", g)
    monkeypatch.setattr(lightning_wallet.requests, "post", p)
    return asked


# ── 1. lightning_wallet: what each answer means ──────────────────────────────

@pytest.mark.parametrize("status, detail", [
    (404, "Wallet not found."),       # LNbits 1.7: the key matches no wallet
    (403, "Invalid adminkey."),       # an invoice key where an admin key belongs
    (401, "No Api Key provided."),
])
def test_a_key_the_server_does_not_know_is_wallet_gone_on_every_call(
        monkeypatch, status, detail):
    _server(monkeypatch, get=R(status, {"detail": detail}),
            post=lambda url, body: R(status, {"detail": detail}))
    w = lightning_wallet.LNbitsWallet(DEMO, KEY)
    for call in (w.balance_sats, lambda: w.create_invoice(50_000)):
        with pytest.raises(lightning_wallet.WalletGone) as exc:
            call()
        text = str(exc.value)
        assert "no longer exists on demo.lnbits.com" in text
        assert "public test servers like this one get reset" in text
        assert "Create a new wallet" in text and "HTTP" not in text
    with pytest.raises(lightning_wallet.WalletKeyRejected) as exc:
        w.pay_invoice("lnbc1fake")
    # nothing was paid (PaymentNotSent), and it is the same "gone"
    assert isinstance(exc.value, lightning_wallet.PaymentNotSent)
    assert isinstance(exc.value, lightning_wallet.WalletGone)
    assert lightning_wallet.wallet_health(
        {"lnbits_url": DEMO, "lnbits_admin_key": KEY})["status"] == "gone"


def test_a_404_that_is_not_lnbits_is_not_called_gone(monkeypatch):
    page = "<!doctype html><html><body><h1>404 Not Found</h1></body></html>"
    _server(monkeypatch, get=R(404, text=page))
    w = lightning_wallet.LNbitsWallet("https://my.node.example/lnbits", KEY)
    with pytest.raises(lightning_wallet.WalletError) as exc:
        w.balance_sats()
    assert not isinstance(exc.value, lightning_wallet.WalletGone)
    assert "<" not in str(exc.value) and "web page" in str(exc.value)


def test_no_connection_or_a_failing_server_is_unreachable_and_never_raw(monkeypatch):
    import requests

    def refused(url):
        raise requests.ConnectionError("Max retries exceeded (NewConnectionError)")
    _server(monkeypatch, get=refused,
            post=lambda url, body: R(502, text="<html>Bad gateway</html>"))
    w = lightning_wallet.LNbitsWallet(DEMO, KEY)
    with pytest.raises(lightning_wallet.WalletUnreachable):
        w.balance_sats()
    with pytest.raises(lightning_wallet.WalletUnreachable) as exc:
        w.create_invoice(1000)
    assert "<html>" not in str(exc.value) and "502" in str(exc.value)


def test_an_answer_that_is_not_json_is_a_short_wallet_error(monkeypatch):
    _server(monkeypatch, get=R(200, text="<html>Just a moment…</html>"),
            post=lambda url, body: R(200, text="<html>Just a moment…</html>"))
    w = lightning_wallet.LNbitsWallet(DEMO, KEY)
    for call in (w.balance_sats, lambda: w.create_invoice(1000)):
        with pytest.raises(lightning_wallet.WalletError) as exc:
            call()
        assert "<html>" not in str(exc.value)


def test_paying_names_the_invoice_as_payment_request_and_bolt11(monkeypatch):
    """LNbits 1.7 reads payment_request (bolt11 is its deprecated name, still
    read by older servers): the same invoice under both."""
    asked = _server(monkeypatch, get=R(200, {"paid": True, "preimage": "ab" * 32}),
                    post=lambda url, body: R(201, {"payment_hash": "cd" * 32}))
    w = lightning_wallet.LNbitsWallet(DEMO, KEY)
    assert w.pay_invoice("lnbc10n1fake") == "ab" * 32
    body = asked[0][2]
    assert body == {"out": True, "payment_request": "lnbc10n1fake",
                    "bolt11": "lnbc10n1fake"}


def test_payment_status_tells_a_gone_wallet_from_an_unknown_payment(monkeypatch):
    w = lightning_wallet.LNbitsWallet(DEMO, KEY)
    _server(monkeypatch, get=R(404, GONE_BODY))
    assert w.payment_status("ab" * 32) == ("wallet_gone", "")
    _server(monkeypatch, get=R(404, {"detail": "Payment does not exist."}))
    assert w.payment_status("ab" * 32) == ("not_found", "")


def test_the_reserve_counts_the_demo_servers_service_fee():
    """demo.lnbits.com keeps max(2, 1%) for routing plus a 5% service fee
    (at most 1,000 sats): an 86,000-sat pass needs 87,860 sats there —
    exactly LNbits' own "You must reserve at least (1860 sat)"."""
    assert lightning_wallet.fee_reserve_sats(86_000, DEMO) == 1_860
    assert lightning_wallet.fee_reserve_sats(86_000, "https://my.node") == 860
    assert lightning_wallet.fee_reserve_sats(86_000) == 860
    assert lightning_wallet.fee_reserve_sats(10_000, DEMO) == 100 + 500
    assert lightning_wallet.reserve_from_refusal(
        "payment failed HTTP 520: You must reserve at least (1860  sat) to "
        "cover potential routing fees.") == 1_860
    assert lightning_wallet.reserve_from_refusal("Insufficient balance.") is None


def test_the_test_server_warning_says_it_is_wiped():
    text = lightning_wallet.demo_warning(DEMO)
    assert "wiped without notice" in text and "AGENTIC_TRADER_LNBITS" in text
    assert lightning_wallet.demo_warning("https://my.node") == ""


def test_wallet_health_is_asked_in_the_background_and_cached(monkeypatch):
    monkeypatch.setattr(lightning_wallet, "HEALTH_BACKGROUND", True)
    asked = _server(monkeypatch, get=R(404, GONE_BODY))
    cfg = {"lnbits_url": DEMO, "lnbits_admin_key": KEY}
    assert lightning_wallet.wallet_health(cfg) is None      # never waits
    for _ in range(200):
        if lightning_wallet.wallet_health(cfg, max_age=3600):
            break
        _time.sleep(0.01)
    h = lightning_wallet.wallet_health(cfg, max_age=3600)
    assert h["status"] == "gone" and h["site"] == "demo.lnbits.com"
    n = len(asked)
    assert lightning_wallet.wallet_health(cfg, max_age=3600)["status"] == "gone"
    assert len(asked) == n == 1                             # cached


# ── 2. the day-pass: no AgentHC invoice while the wallet can't pay ───────────

WCFG = {"day_pass_recurring": True, "lnbits_url": DEMO, "lnbits_admin_key": KEY}


class LNbitsLike(Wallet):
    """The test feed's wallet, with a balance read: `balance` sats, or the
    error it raises (`balance` an exception)."""

    def __init__(self, balance=1_000_000, mode="ok"):
        super().__init__(mode)
        self.balance, self.asked = balance, 0

    def balance_sats(self):
        self.asked += 1
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance


def _feed_and(monkeypatch, wallet, sats=10_000):
    feed = Feed(sats=sats)
    monkeypatch.setattr(agenthc.requests, "get", feed.get)
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    return feed


def _gone():
    return lightning_wallet.WalletGone(url=DEMO)


def test_a_gone_wallet_asks_agenthc_for_no_invoice_and_says_how_to_fix_it(
        monkeypatch, clock, alerts):
    wallet = LNbitsLike(balance=_gone())
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    assert not _poll(dict(WCFG), state)
    assert feed.minted == 0 and wallet.paid == []          # no invoice made
    assert agenthc.exit_loss_reason() == "day_pass_wallet_gone"
    (text,) = alerts
    assert "no longer exists on demo.lnbits.com" in text
    assert "Create a new wallet" in text and "Settings → Signal source" in text
    assert state["daypass_alert"] == "daypass_wallet_gone_notified"
    # every poll of the day: no invoice; the wallet re-asked every 5 minutes
    for _ in range(10):
        clock.tick(minutes=1)
        _poll(dict(WCFG), state)
    assert feed.minted == 0 and len(alerts) == 1
    assert wallet.asked == 3                     # at 0, 5 and 10 minutes
    # the owner made a new wallet (another key) and funded it: bought at once
    new = LNbitsLike(balance=500_000)
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: new)
    cfg = {**WCFG, "lnbits_admin_key": "fakeNEWkey"}
    assert len(_poll(cfg, state)) == 1
    assert feed.minted == 1 and len(new.paid) == 1
    assert "daypass_alert" not in state            # the alert went with it


def test_a_wallet_that_answers_again_buys_again(monkeypatch, clock, alerts):
    """Keys changed back (or a server hiccup that looked like "gone"): the
    next check that reads a balance clears it — no restart needed."""
    wallet = LNbitsLike(balance=_gone())
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    _poll(dict(WCFG), state)
    wallet.balance = 200_000
    clock.tick(minutes=5)
    assert len(_poll(dict(WCFG), state)) == 1 and feed.minted == 1


def test_a_gone_wallet_found_by_the_payment_itself_stops_the_retries(
        monkeypatch, clock, alerts):
    class GoneAtPay(LNbitsLike):
        def pay_invoice(self, invoice):
            raise lightning_wallet.WalletKeyRejected(
                "the wallet's server did not accept the request, HTTP 404: "
                "Wallet not found.", url=DEMO)
    wallet = GoneAtPay()
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    assert not _poll(dict(WCFG), state)
    assert feed.minted == 1 and "daypass_pending" not in state   # nothing paid
    assert "no longer exists" in alerts[0] and "Nothing was paid" in alerts[0]
    assert agenthc.exit_loss_reason() == "day_pass_wallet_gone"
    # the wallet now says gone on its balance too: no more invoices all day
    wallet.balance = _gone()
    for _ in range(12):
        clock.tick(minutes=5)
        _poll(dict(WCFG), state)
    assert feed.minted == 1


def test_a_short_wallet_is_not_offered_an_invoice_until_it_holds_enough(
        monkeypatch, clock, alerts):
    """Price 10,000 sats on demo.lnbits.com: 10,000 + 100 reserve + 500
    service fee = 10,600 needed. 10,300 is short: no invoice, the exact
    top-up named; topped up, the pass is bought within a minute."""
    monkeypatch.setattr(agenthc, "day_pass_price",
                        lambda: {"usd": 50.0, "sats": 10_000})
    wallet = LNbitsLike(balance=10_300)
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    assert not _poll(dict(WCFG), state)
    assert feed.minted == 0 and agenthc.exit_loss_reason() == "day_pass_short"
    (text,) = alerts
    assert "10,300 sats" in text and "about 10,600 sats" in text
    assert "Add at least 300 sats" in text
    wallet.balance = 10_600
    clock.tick(seconds=61)
    assert len(_poll(dict(WCFG), state)) == 1 and feed.minted == 1


def test_lnbits_own_reserve_figure_is_used_after_a_refusal(monkeypatch, clock, alerts):
    """No price known beforehand; the wallet refuses for the reserve: the
    sats LNbits named become the bar, so the next tries make no invoice
    until the wallet holds them."""
    class Reserve(LNbitsLike):
        def pay_invoice(self, invoice):
            raise lightning_wallet.PaymentRefused(
                "payment failed HTTP 520: You must reserve at least (1860  sat)"
                " to cover potential routing fees.")
    wallet = Reserve(balance=10_500)
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    assert not _poll(dict(WCFG), state)
    assert feed.minted == 1
    assert "about 11,860 sats" in alerts[0] and "Nothing was paid" in alerts[0]
    assert agenthc.exit_loss_reason() == "day_pass_short"
    for _ in range(6):                        # past the 5-minute retry wait
        clock.tick(minutes=2)
        _poll(dict(WCFG), state)
    assert feed.minted == 1                   # still short: no new invoice
    wallet.balance = 12_000
    wallet.pay_invoice = lambda invoice: Wallet.pay_invoice(wallet, invoice)
    clock.tick(minutes=2)
    assert len(_poll(dict(WCFG), state)) == 1 and feed.minted == 2


def test_an_unreachable_wallet_makes_no_invoice_and_is_told_after_a_while(
        monkeypatch, clock, alerts):
    wallet = LNbitsLike(balance=lightning_wallet.WalletUnreachable("no route"))
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    _poll(dict(WCFG), state)
    assert feed.minted == 0 and alerts == []           # a blip: not yet
    for _ in range(4):
        clock.tick(minutes=5)
        _poll(dict(WCFG), state)
    assert feed.minted == 0 and len(alerts) == 1
    assert "can't reach its wallet's server (demo.lnbits.com)" in alerts[0]
    wallet.balance = 100_000
    clock.tick(minutes=5)
    assert len(_poll(dict(WCFG), state)) == 1
    assert "daypass_alert" not in state


def test_a_gone_wallet_is_reported_at_agent_start_outside_buying_hours(
        monkeypatch, clock, alerts):
    """3 AM: nothing would be bought for hours, but the owner hears now
    (once a day), and the wallet is asked at most every 15 minutes."""
    clock.now = datetime(2026, 7, 15, 3, 0, tzinfo=agenthc.MARKET_TZ)
    wallet = LNbitsLike(balance=_gone())
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    _poll(dict(WCFG), state)
    assert len(alerts) == 1 and "no longer exists" in alerts[0]
    assert feed.minted == 0 and wallet.asked == 1
    clock.tick(minutes=10)
    _poll(dict(WCFG), state)
    assert wallet.asked == 1 and len(alerts) == 1
    # recurring off, or a key: the wallet is never asked
    other = LNbitsLike(balance=_gone())
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: other)
    _poll({**WCFG, "day_pass_recurring": False}, {})
    _poll({**WCFG, "agenthc_api_key": "k"}, {})
    assert other.asked == 0


def test_a_payment_started_before_the_wipe_stays_undecided_and_is_explained(
        monkeypatch, clock, alerts):
    """The pay request got no answer, then the server forgot the wallet: the
    payment may have gone out. It is never decided "failed" (that could pay
    twice); the owner is told why."""
    class Wiped(LNbitsLike):
        def pay_invoice(self, invoice):
            raise lightning_wallet.WalletError("no answer from the wallet while paying")

        def payment_status(self, payment_hash):
            return "wallet_gone", ""
    wallet = Wiped()
    feed = _feed_and(monkeypatch, wallet)
    state = {}
    _poll(dict(WCFG), state)
    assert isinstance(state.get("daypass_pending"), dict)
    for _ in range(4):
        clock.tick(minutes=5)
        _poll(dict(WCFG), state)
    assert feed.minted == 1 and isinstance(state.get("daypass_pending"), dict)
    assert any("stopped accepting the wallet's key" in a for a in alerts)
    assert agenthc.exit_loss_reason() == "day_pass_undecided"


# ── 3. the dashboard and setup pages ─────────────────────────────────────────

@pytest.fixture
def gone_lnbits(monkeypatch):
    """The wallet's server forgot the agent's wallet; making a new one works."""
    log = {"made": [], "invoices": []}

    class GoneWallet:
        def __init__(self, url, key):
            self.url, self.key = url, key

        def balance_sats(self, timeout=None):
            if self.key.startswith("fakeNEW"):
                return 0
            lightning_wallet._note_health(self.url, self.key, "gone")
            raise lightning_wallet.WalletGone(url=self.url)

        def create_invoice(self, sats, memo=""):
            if not self.key.startswith("fakeNEW"):
                raise lightning_wallet.WalletGone(url=self.url)
            log["invoices"].append(sats)
            return f"lnbc{sats}n1fakeinvoice"

    def create_wallet(instance_url=None, name="agentic-trader"):
        log["made"].append(1)
        return (DEMO, "fakeNEWadminKEY0001", f"{DEMO}/wallet?usr=u1&wal=w1")
    monkeypatch.setattr(lightning_wallet, "LNbitsWallet", GoneWallet)
    monkeypatch.setattr(lightning_wallet, "create_wallet", create_wallet)
    monkeypatch.setattr(webui, "_qr_matrix", lambda data: None)
    return log


def test_adding_sats_to_a_gone_wallet_says_so_and_shows_no_invoice(
        home, app, gone_lnbits):
    write_cfg(home, {**BASE, "lnbits_url": DEMO, "lnbits_admin_key": KEY})
    c = _running(app)
    for r in (c.post("/api/wallet/fund", {"sats": 90_000}),
              c.get("/api/wallet/balance"),
              c.post("/api/v2/wallet/fund", {"sats": 90_000})):   # the review step
        assert r["ok"] is False and r["wallet_gone"] is True
        assert "no longer exists on demo.lnbits.com" in r["error"]
        assert "Create a new wallet" in r["error"]
        assert "bolt11" not in r and "needs_confirm" not in r
        assert "HTTP" not in r["error"] and '"detail"' not in r["error"]
    assert gone_lnbits["invoices"] == []
    # the panel says it too, from what the wallet's server last said
    health = c.get("/api/v2/settings")["signals"]["wallet"]["health"]
    assert health["status"] == "gone" and "Create a new wallet" in health["message"]
    # the one step: a new wallet, the old one's details kept in a file
    first = c.post("/api/v2/wallet/create", {"replace": True})
    done = c.post("/api/v2/wallet/create",
                  {"replace": True, "confirm": first["preview"]["token"]})
    assert done["ok"] is True and done["old_wallet_file"]
    kept = json.loads(open(done["old_wallet_file"], encoding="utf-8").read())
    assert kept["lnbits_admin_key"] == KEY
    assert read_cfg(home)["lnbits_admin_key"] == "fakeNEWadminKEY0001"
    r = c.post("/api/wallet/fund", {"sats": 90_000})
    assert r["ok"] is True and r["bolt11"].startswith("lnbc")
    assert "Cash App" in r["hint"]


def test_the_funding_suggestion_counts_the_servers_fees(monkeypatch):
    from sources import agenthc_day_trade_ideas as hc
    monkeypatch.setattr(hc, "day_pass_price", lambda: {"usd": 50.0, "sats": 86_000})
    monkeypatch.setattr(webui, "_btc_usd", lambda: 58_000.0)
    q = webui._fund_quote(0, 0, DEMO)
    assert q["suggested_sats"] == 87_860 and q["reserve_sats"] == 1_860
    assert webui._fund_quote(0, 0, "https://my.node")["suggested_sats"] == 86_860


def test_the_pages_offer_create_a_new_wallet_where_the_error_shows():
    page = webui._WIZARD_HTML
    assert "function walletTrouble(r)" in page and "r.wallet_gone" in page
    assert "Create a new wallet" in page and 'id="fund-hint"' in page
    import glob
    import os
    dist = os.path.join(os.path.dirname(webui.__file__), "webui_dist", "assets")
    built = "".join(open(p, encoding="utf-8").read()
                    for p in glob.glob(os.path.join(dist, "*.js")))
    # the setup wizard and /v2 (built from webui_app/src)
    assert built.count("wallet_gone") >= 2 and "Create a new wallet" in built


# ── 4. the command line ──────────────────────────────────────────────────────

def _cfg_file(home, cfg):
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_fund_on_the_command_line_explains_a_gone_wallet(home, monkeypatch, capsys):
    _cfg_file(home, {"lnbits_url": DEMO, "lnbits_admin_key": KEY})
    _server(monkeypatch, post=lambda url, body: R(404, GONE_BODY))
    with pytest.raises(SystemExit) as exc:
        agent.cmd_fund(50_000)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "no longer exists on demo.lnbits.com" in out
    assert "python agent.py wallet new" in out and "Traceback" not in out


def test_status_and_doctor_survive_a_wallet_server_that_is_down(home, monkeypatch, capsys):
    import requests

    def down(url):
        raise requests.ConnectionError("Name or service not known")
    _cfg_file(home, {"source": "agenthc", "lnbits_url": DEMO,
                     "lnbits_admin_key": KEY, "day_pass_recurring": True})
    _server(monkeypatch, get=down)
    agent.cmd_status()
    assert "can't reach its wallet's server (demo.lnbits.com)" in capsys.readouterr().out


def test_wallet_new_replaces_it_in_one_step_and_keeps_the_old_one(
        home, monkeypatch, capsys):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    _cfg_file(home, {"source": "agenthc", "lnbits_url": DEMO, "lnbits_admin_key": KEY,
                     "lnbits_wallet_page": f"{DEMO}/wallet?usr=old"})
    monkeypatch.setattr(lightning_wallet, "create_wallet", lambda *a, **k: (
        DEMO, "fakeNEWadminKEY0002", f"{DEMO}/wallet?usr=new"))
    # Enter keeps it
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    agent.cmd_wallet(["new"])
    assert json.loads((home / "config.json").read_text())["lnbits_admin_key"] == KEY
    # REPLACE: switched, the old wallet's details saved first
    monkeypatch.setattr("builtins.input", lambda prompt="": "REPLACE")
    agent.cmd_wallet(["new"])
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["lnbits_admin_key"] == "fakeNEWadminKEY0002"
    assert cfg["lnbits_wallet_page"] == f"{DEMO}/wallet?usr=new"
    (old,) = [p for p in home.iterdir() if p.name.startswith("old-wallet-")]
    kept = json.loads(old.read_text())
    assert kept["lnbits_admin_key"] == KEY and kept["lnbits_wallet_page"].endswith("usr=old")
    out = capsys.readouterr().out
    assert "New wallet made" in out and old.name in out and "fund" in out


def test_v2_shows_the_dashboard_place_in_its_own_words():
    """The agent's messages name the classic page's place; /v2's v2Words
    turns it into "Signals & pass" (no half-translated place)."""
    import os
    import re
    src = open(os.path.join(os.path.dirname(webui.__file__), "webui_app", "src",
                            "v2", "lib", "settings.ts"), encoding="utf-8").read()
    assert r"\bin the dashboard\s*\(Settings${to}Signal\s+source\)" in src
    for text in (lightning_wallet.NEW_WALLET_DASHBOARD, agenthc.WALLET_WHERE):
        assert re.search(r"(in )?the dashboard \(Settings → Signal source\)", text)


def test_the_new_cause_is_named_the_same_everywhere():
    import os
    import re
    import view_model
    assert "day_pass_wallet_gone" in view_model.EXIT_LOSS_CAUSES
    assert "day_pass_wallet_gone" in agent._SIGNAL_LOSS_WHY
    assert "create a new wallet" in agent._signal_loss_fix({}, "day_pass_wallet_gone")
    copy_ts = open(os.path.join(os.path.dirname(webui.__file__), "webui_app", "src",
                                "v2", "lib", "copy.ts"), encoding="utf-8").read()
    m = re.search(r'day_pass_wallet_gone: "([^"]+)"', copy_ts)
    assert m and m.group(1) == agent._SIGNAL_LOSS_WHY["day_pass_wallet_gone"]
