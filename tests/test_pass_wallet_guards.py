"""Dashboard money guards: a second click must never lose something paid for.

  - Asking for a second pass invoice keeps the first one on file: whichever
    the owner pays, the agent finds the pass.
  - "Create my agent's wallet" never replaces a wallet that is already set up
    unless the request says so — and then the old wallet's keys are saved to
    the agent's private folder first.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

import lightning_wallet
import webui
from sources import agenthc_day_trade_ideas as hc


class Server:
    """Scripted AgentHC pass shop: each buy makes a new token + invoice."""

    def __init__(self):
        self.bought, self.status = [], {}

    def buy_pass(self, days, extend_token=None):
        n = len(self.bought) + 1
        token = f"L402 PASS{n}:paid"
        self.bought.append((days, extend_token, token))
        self.status[token] = "pending"
        return {"pass_token": token, "payment_request": f"lnbc{days}0n1invoice{n}",
                "amount_sats": 1000 * days, "amount_usd_approx": days * 10,
                "access_until": "2030-01-01T00:00:00+00:00"}

    def pass_status(self, token):
        return {"status": self.status.get(token, "invalid"), "days_left": 5,
                "access_until": "2030-01-02T00:00:00+00:00"}


@pytest.fixture
def shop(monkeypatch):
    srv = Server()
    monkeypatch.setattr(hc, "buy_pass", srv.buy_pass)
    monkeypatch.setattr(hc, "pass_status", srv.pass_status)
    monkeypatch.setattr(webui, "_qr_matrix", lambda text: [[1]])
    return srv


def _saver():
    saves = []
    return saves, lambda: saves.append(1)


# ── P5: every unpaid invoice stays on file until it is paid or expires ──────

def test_paying_the_first_invoice_after_asking_for_a_second_keeps_the_pass(shop):
    cfg = {}
    saves, save = _saver()
    a = webui._pass_buy(cfg, save, {"days": 5})
    b = webui._pass_buy(cfg, save, {"days": 20})      # changed their mind…
    assert a["ok"] and b["ok"] and a["bolt11"] != b["bolt11"]
    tok_a, tok_b = shop.bought[0][2], shop.bought[1][2]
    # the agent still checks both invoices' passes
    assert set(hc._pass_tokens(cfg)) == {tok_a, tok_b}
    shop.status[tok_a] = "active"                      # …then paid the first
    r = webui._pass_status(cfg, save)
    assert r["ok"] and r["status"] == "active"
    assert cfg["feed_pass"]["token"] == tok_a          # the paid pass is the pass
    assert cfg["feed_pass_pending"]["token"] == tok_b  # the other can still be paid
    assert set(hc._pass_tokens(cfg)) == {tok_a, tok_b}


def test_double_click_gives_the_same_invoice_back(shop):
    cfg = {}
    saves, save = _saver()
    a = webui._pass_buy(cfg, save, {"days": 5})
    b = webui._pass_buy(cfg, save, {"days": 5})
    assert len(shop.bought) == 1                       # one invoice was created
    assert b == a
    assert hc._pass_tokens(cfg) == [shop.bought[0][2]]


def test_same_size_again_after_the_invoice_got_old_makes_a_new_one(shop, monkeypatch):
    cfg = {}
    saves, save = _saver()
    webui._pass_buy(cfg, save, {"days": 5})
    cfg["feed_pass_pending"]["bought_at"] -= 55 * 60   # 55 minutes ago
    webui._pass_buy(cfg, save, {"days": 5})
    assert len(shop.bought) == 2
    assert len(hc._pass_tokens(cfg)) == 2              # the old one is still watched


def test_both_invoices_paid_keeps_both_passes(shop):
    cfg = {}
    saves, save = _saver()
    webui._pass_buy(cfg, save, {"days": 5})
    webui._pass_buy(cfg, save, {"days": 20})
    tok_a, tok_b = shop.bought[0][2], shop.bought[1][2]
    shop.status[tok_b] = "active"
    assert webui._pass_status(cfg, save)["status"] == "active"
    assert cfg["feed_pass"]["token"] == tok_b
    shop.status[tok_a] = "active"                      # the older one is paid too
    assert webui._pass_status(cfg, save)["status"] == "active"
    assert set(hc._pass_tokens(cfg)) == {tok_a, tok_b}  # neither paid pass is lost
    assert "feed_pass_pending" not in cfg
    for _ in range(3):                                 # and it stays that way
        webui._pass_status(cfg, save)
        assert set(hc._pass_tokens(cfg)) == {tok_a, tok_b}
    # when the main pass is finished, the other paid one takes over
    main = cfg["feed_pass"]["token"]
    other = tok_a if main == tok_b else tok_b
    shop.status[main] = "used_up"
    r = webui._pass_status(cfg, save)
    assert r["status"] == "active" and cfg["feed_pass"]["token"] == other
    assert hc._pass_tokens(cfg) == [other]


def test_unpaid_invoices_are_dropped_once_they_expire(shop):
    cfg = {}
    saves, save = _saver()
    webui._pass_buy(cfg, save, {"days": 5})
    webui._pass_buy(cfg, save, {"days": 20})
    assert webui._pass_status(cfg, save)["status"] == "pending"
    assert len(hc._pass_tokens(cfg)) == 2              # both still payable
    for token in list(shop.status):
        shop.status[token] = "expired"
    assert webui._pass_status(cfg, save)["status"] == "unpaid"
    assert hc._pass_tokens(cfg) == []
    assert "feed_pass_pending" not in cfg and not cfg.get("feed_pass_extra")


def test_renewal_still_replaces_the_pass_it_extends(shop):
    """The ordinary case is unchanged: buying more days while a pass runs
    extends that pass; once paid, the new token replaces the old one."""
    old = "L402 OLD:paid"
    shop.status[old] = "active"
    cfg = {"feed_pass": {"token": old, "days": 5}}
    saves, save = _saver()
    webui._pass_buy(cfg, save, {"days": 20})
    assert shop.bought[0][1] == old                    # asked to extend it
    new = shop.bought[0][2]
    shop.status[new], shop.status[old] = "active", "superseded"
    assert webui._pass_status(cfg, save)["status"] == "active"
    assert hc._pass_tokens(cfg) == [new]
    assert "feed_pass_pending" not in cfg and not cfg.get("feed_pass_extra")


def test_pending_invoice_from_the_previous_version_is_not_lost(shop):
    """config.json written before this version: one pending purchase, no
    invoice text stored with it. Asking for another keeps it on file."""
    legacy = "L402 LEGACY:paid"
    shop.status[legacy] = "pending"
    import time
    cfg = {"feed_pass_pending": {"token": legacy, "days": 5,
                                 "bought_at": time.time() - 60,
                                 "access_until": None}}
    saves, save = _saver()
    webui._pass_buy(cfg, save, {"days": 5})
    assert len(shop.bought) == 1                       # no invoice text to give back
    assert set(hc._pass_tokens(cfg)) == {legacy, shop.bought[0][2]}
    shop.status[legacy] = "active"
    assert webui._pass_status(cfg, save)["status"] == "active"
    assert cfg["feed_pass"]["token"] == legacy


def test_status_check_running_while_another_pass_is_requested_loses_nothing(
        shop, monkeypatch):
    """The dashboard checks the invoice every few seconds; the owner clicks
    another size while a check is on its way to AgentHC. The check must not
    write back its older view of the list over the new invoice."""
    import threading
    cfg = {}
    saves, save = _saver()
    webui._pass_buy(cfg, save, {"days": 5})
    real_status, started = shop.pass_status, []

    def slow_status(token):
        if not started:
            t = threading.Thread(target=webui._pass_buy, args=(cfg, save, {"days": 20}))
            started.append(t)
            t.start()
            t.join(0.3)              # give the click every chance to get in
        return real_status(token)
    monkeypatch.setattr(hc, "pass_status", slow_status)
    assert webui._pass_status(cfg, save)["status"] == "pending"
    started[0].join(5)
    assert not started[0].is_alive()
    assert len(shop.bought) == 2
    assert set(hc._pass_tokens(cfg)) == {shop.bought[0][2], shop.bought[1][2]}


def test_two_create_wallet_clicks_at_once_make_one_wallet(home, lnbits, monkeypatch):
    import threading
    cfg, replies = {}, []
    saves, save = _saver()
    gate = threading.Event()
    real_create = lightning_wallet.create_wallet

    def slow_create(*a, **kw):
        gate.wait(5)                 # the first click is still talking to LNbits
        return real_create(*a, **kw)
    monkeypatch.setattr(lightning_wallet, "create_wallet", slow_create)
    clicks = [threading.Thread(
        target=lambda: replies.append(webui._wallet_create(cfg, save, {})))
        for _ in range(2)]
    for t in clicks:
        t.start()
    clicks[0].join(0.2)
    gate.set()
    for t in clicks:
        t.join(5)
    assert len(lnbits) == 1
    assert sorted(r["ok"] for r in replies) == [False, True]
    assert cfg["lnbits_admin_key"] == "adminkey1" * 4


def test_setup_command_keeps_the_wallet_unless_told_to_replace(
        home, lnbits, monkeypatch, capsys):
    cfg = {"lnbits_url": "https://demo.lnbits.com", "lnbits_admin_key": "oldkey" * 6,
           "lnbits_wallet_page": "https://demo.lnbits.com/wallet?usr=old"}
    answers = iter([""])                                # just presses Enter
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    out = lightning_wallet.wallet_setup(dict(cfg))
    assert out == cfg and lnbits == [] and _old_wallet_files(home) == []
    assert "already has a Lightning wallet" in capsys.readouterr().out
    answers = iter(["REPLACE", "1", ""])               # replace, create, don't fund
    out = lightning_wallet.wallet_setup(dict(cfg))
    assert out["lnbits_admin_key"] == "adminkey1" * 4
    assert out["lnbits_wallet_page"].endswith("usr=u1&wal=w1")
    files = _old_wallet_files(home)
    assert len(files) == 1
    with open(os.path.join(str(home), files[0]), encoding="utf-8") as f:
        assert json.load(f)["lnbits_admin_key"] == "oldkey" * 6
    assert files[0] in capsys.readouterr().out


# ── P6: creating a wallet never strands the one already set up ──────────────

@pytest.fixture
def lnbits(monkeypatch):
    made = []

    def create_wallet(instance_url=None, name="agentic-trader"):
        n = len(made) + 1
        made.append(n)
        return ("https://demo.lnbits.com", f"adminkey{n}" * 4,
                f"https://demo.lnbits.com/wallet?usr=u{n}&wal=w{n}")
    monkeypatch.setattr(lightning_wallet, "create_wallet", create_wallet)
    return made


def _old_wallet_files(home):
    return sorted(n for n in os.listdir(home) if n.startswith("old-wallet-"))


def test_second_click_on_create_wallet_does_not_replace_the_wallet(home, lnbits):
    cfg = {}
    saves, save = _saver()
    first = webui._wallet_create(cfg, save, {})
    assert first["ok"] and len(lnbits) == 1
    key, page = cfg["lnbits_admin_key"], cfg["lnbits_wallet_page"]
    again = webui._wallet_create(cfg, save, {})
    assert again["ok"] is False and again["exists"] is True
    assert "already has a wallet" in again["error"]
    assert again["page"] == page                       # where the sats are
    assert len(lnbits) == 1                            # nothing was created
    assert cfg["lnbits_admin_key"] == key and cfg["lnbits_wallet_page"] == page
    assert _old_wallet_files(home) == []
    # anything but an explicit yes is a no
    for data in ({"replace": "true"}, {"replace": 1}, {"replace": False}, None):
        assert webui._wallet_create(cfg, save, data)["ok"] is False
    assert len(lnbits) == 1 and cfg["lnbits_admin_key"] == key


def test_replacing_a_wallet_keeps_the_old_keys_in_the_private_folder(home, lnbits):
    cfg = {}
    saves, save = _saver()
    webui._wallet_create(cfg, save, {})
    old = {k: cfg[k] for k in ("lnbits_url", "lnbits_admin_key", "lnbits_wallet_page")}
    r = webui._wallet_create(cfg, save, {"replace": True})
    assert r["ok"] and len(lnbits) == 2
    assert cfg["lnbits_admin_key"] != old["lnbits_admin_key"]
    files = _old_wallet_files(home)
    assert len(files) == 1
    with open(os.path.join(str(home), files[0]), encoding="utf-8") as f:
        kept = json.load(f)
    assert {k: kept[k] for k in old} == old
    assert r["old_wallet_file"] == os.path.join(str(home), files[0])
    assert r["old_wallet_page"] == old["lnbits_wallet_page"]
    assert "old wallet" in r["note"]
    # a second replacement keeps the second wallet too, without overwriting
    second = dict(cfg)
    webui._wallet_create(cfg, save, {"replace": True})
    files = _old_wallet_files(home)
    assert len(files) == 2
    keys = set()
    for name in files:
        with open(os.path.join(str(home), name), encoding="utf-8") as f:
            keys.add(json.load(f)["lnbits_admin_key"])
    assert keys == {old["lnbits_admin_key"], second["lnbits_admin_key"]}


def test_wallet_is_not_replaced_when_the_old_keys_cannot_be_saved(
        home, lnbits, monkeypatch):
    cfg = {}
    saves, save = _saver()
    webui._wallet_create(cfg, save, {})
    key = cfg["lnbits_admin_key"]

    def broken(cfg):
        raise OSError("disk full")
    monkeypatch.setattr(lightning_wallet, "keep_old_wallet", broken)
    before, n_saves = dict(cfg), len(saves)
    r = webui._wallet_create(cfg, save, {"replace": True})
    assert r["ok"] is False and "was not replaced" in r["error"]
    assert "still uses the wallet it had" in r["error"]
    assert cfg["lnbits_admin_key"] == key
    # the new wallet was made (empty, and never used); nothing else changed
    assert len(lnbits) == 2 and cfg == before and len(saves) == n_saves


def test_failed_wallet_creation_leaves_no_file_calling_the_wallet_replaced(
        home, lnbits, monkeypatch):
    """The owner confirms the replacement while the wallet server is down,
    three times. The wallet was never replaced, so no old-wallet file (a
    copy of a key that can spend it) may be left behind."""
    cfg = {}
    saves, save = _saver()
    webui._wallet_create(cfg, save, {})
    before, n_saves = dict(cfg), len(saves)

    def down(instance_url=None, name="agentic-trader"):
        raise lightning_wallet.WalletError("could not reach https://demo.lnbits.com")
    monkeypatch.setattr(lightning_wallet, "create_wallet", down)
    for _ in range(3):
        r = webui._wallet_create(cfg, save, {"replace": True})
        assert r["ok"] is False and "could not reach" in r["error"]
        assert "current wallet was not changed" in r["error"]
    assert _old_wallet_files(home) == []
    assert cfg == before and len(saves) == n_saves


def test_old_wallet_is_filed_after_the_new_one_exists_and_before_the_switch(
        home, lnbits, monkeypatch):
    cfg = {}
    saves, save = _saver()
    webui._wallet_create(cfg, save, {})
    old_key = cfg["lnbits_admin_key"]
    real_keep, seen = lightning_wallet.keep_old_wallet, []

    def keep(c):
        seen.append((len(lnbits), c["lnbits_admin_key"]))
        return real_keep(c)
    monkeypatch.setattr(lightning_wallet, "keep_old_wallet", keep)
    assert webui._wallet_create(cfg, save, {"replace": True})["ok"]
    assert seen == [(2, old_key)]      # new wallet made; agent still on the old one
    assert cfg["lnbits_admin_key"] != old_key


def test_wallet_exists_refusal_says_how_to_replace_it():
    """The wallet may be gone (a reset test server): "just add sats to it"
    alone would be a dead end."""
    text = webui._WALLET_EXISTS
    assert "already has a wallet" in text and "add sats" in text
    assert "python agent.py setup" in text and "REPLACE" in text
    assert "saved to a file first" in text


def _wizard_fn(name):
    """One top-level function from the built-in wizard page's script."""
    m = re.search(r"^async function " + name + r"\(.*?^}$", webui._WIZARD_HTML,
                  re.M | re.S)
    assert m, name
    return m.group(0)


def test_classic_page_asks_before_it_replaces_a_wallet():
    js = _wizard_fn("makeWallet")
    assert "r.exists" in js and "confirm(" in js and "{replace:true}" in js
    assert js.index("api('/api/wallet/create',{})") < js.index("{replace:true}")


@pytest.mark.parametrize("answer, posts, shown", [
    (True, [{}, {"replace": True}], "created"),
    (False, [{}], "Kept your current wallet"),
])
def test_classic_page_replaces_a_wallet_only_on_a_yes(answer, posts, shown):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = """
const posted = [], asked = [], mw = {textContent: '', innerHTML: ''};
const document = {getElementById: () => mw};
const esc = (s) => String(s);
const confirm = (q) => { asked.push(q); return %s; };
const showWallet = (page, label) => { mw.innerHTML = label + ' ' + page; };
const api = async (path, body) => {
  posted.push(body);
  return body.replace === true
    ? {ok: true, page: 'https://demo.lnbits.com/wallet?usr=new', warning: '',
       note: 'New wallet created. The old wallet is saved in old-wallet-1.json.'}
    : {ok: false, exists: true, error: 'Your agent already has a wallet'};
};
%s
makeWallet().then(() => console.log(JSON.stringify(
  {posted, asked, text: mw.textContent + mw.innerHTML})));
""" % ("true" if answer else "false", _wizard_fn("makeWallet"))
    # Keep the parent environment (node.exe needs SystemRoot on Windows);
    # decode as UTF-8 on every platform.
    r = subprocess.run([node, "-e", js], capture_output=True, text=True,
                       encoding="utf-8", timeout=30, env={**os.environ})
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    out = json.loads(r.stdout)
    assert out["posted"] == posts and len(out["asked"]) == 1
    question = out["asked"][0]
    assert "Replace it" in question and "NOT moved" in question
    assert "Cancel = keep it" in question
    assert shown in out["text"]
    if answer:
        assert "old-wallet-1.json" in out["text"]      # where the old one went


def test_keep_old_wallet_writes_the_details_to_the_private_folder(home):
    cfg = {"lnbits_url": "https://demo.lnbits.com", "lnbits_admin_key": "oldkey" * 6,
           "lnbits_wallet_page": "https://demo.lnbits.com/wallet?usr=u&wal=w"}
    path = lightning_wallet.keep_old_wallet(cfg)
    assert os.path.dirname(path) == str(home)
    with open(path, encoding="utf-8") as f:
        kept = json.load(f)
    assert kept["lnbits_admin_key"] == "oldkey" * 6
    assert "move" in kept["note"].lower()              # says what the file is for
    assert lightning_wallet.keep_old_wallet({}) == ""  # no wallet: nothing to keep


PASTED_OLD = {"lnbits_url": "https://demo.lnbits.com", "lnbits_admin_key": "oldkey" * 6,
              "lnbits_wallet_page": "https://demo.lnbits.com/wallet?usr=u&wal=w"}


def test_pasting_a_different_wallet_key_keeps_the_old_one_on_file(home):
    cfg = dict(PASTED_OLD)
    assert webui._use_pasted_wallet(cfg, "https://my.lnbits.example", "newkey" * 6) == ""
    assert cfg["lnbits_url"] == "https://my.lnbits.example"
    assert cfg["lnbits_admin_key"] == "newkey" * 6
    assert "lnbits_wallet_page" not in cfg             # that was the old wallet's page
    assert cfg["max_autopay_sats"] == 30_000
    files = _old_wallet_files(home)
    assert len(files) == 1
    with open(os.path.join(str(home), files[0]), encoding="utf-8") as f:
        kept = json.load(f)
    assert {k: kept[k] for k in PASTED_OLD} == PASTED_OLD


def test_pasting_the_same_wallet_key_again_writes_nothing(home):
    cfg = dict(PASTED_OLD, max_autopay_sats=20_000)
    assert webui._use_pasted_wallet(cfg, PASTED_OLD["lnbits_url"], "oldkey" * 6) == ""
    assert _old_wallet_files(home) == []
    assert cfg == dict(PASTED_OLD, max_autopay_sats=20_000)   # page link kept too
    first = {}                                         # and a first wallet: no file
    assert webui._use_pasted_wallet(first, "https://my.lnbits.example", "k" * 32) == ""
    assert first["lnbits_admin_key"] == "k" * 32 and _old_wallet_files(home) == []


def test_pasted_wallet_is_not_used_when_the_old_keys_cannot_be_saved(home, monkeypatch):
    cfg = dict(PASTED_OLD)

    def broken(cfg):
        raise OSError("disk full")
    monkeypatch.setattr(lightning_wallet, "keep_old_wallet", broken)
    problem = webui._use_pasted_wallet(cfg, "https://my.lnbits.example", "newkey" * 6)
    assert "was not changed" in problem and "disk full" in problem
    assert cfg == PASTED_OLD                           # nothing moved


def test_switching_recurring_on_records_when(monkeypatch):
    """The time it was last switched ON is what lets the owner restart a
    wallet the agent stopped buying with (it paid, but gave no receipt)."""
    import time
    cfg = {}
    saves, save = _saver()
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    assert webui._set_recurring(cfg, save, True) == {"ok": True, "on": True}
    assert cfg == {"day_pass_recurring": True,
                   "day_pass_recurring_on_at": 1_800_000_000.0}
    monkeypatch.setattr(time, "time", lambda: 1_800_000_500.0)
    assert webui._set_recurring(cfg, save, False) == {"ok": True, "on": False}
    assert cfg["day_pass_recurring"] is False
    assert cfg["day_pass_recurring_on_at"] == 1_800_000_000.0   # OFF doesn't move it
    assert len(saves) == 2
