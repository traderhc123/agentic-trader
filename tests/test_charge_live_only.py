"""Only LIVE uses signal days: practice and off never buy or use a pass.

The contract with AgentHC: every read of the day-trade-ideas feed says the
agent's mode as the query param `mode` AND the header X-Agent-Mode (the
same value): "live", "dry_run" (practice) or "off" (the owner paused the
agent). AgentHC charges a signal day only for "live". On this side:
  - the mode is what the orders use (agent._feed_mode: Pause → off,
    practice → dry_run, else live), worked out on every read;
  - it is sent on every feed request: a normal read, a prepaid pass's read,
    a Premium key's read, an ended day-pass's exits-only read, the renewal
    and buy-over-exits-only reads, and the free /closed preview;
  - the wallet buys a day-pass only when live; in practice / off it says so
    plainly and buys nothing (the practice preview is the free fallback);
  - a paused agent that still reads exits for a real position says "off".
"""
import json
import time
from datetime import timedelta

import pytest

import agent
import dry_run_preview
from sources import agenthc_day_trade_ideas as hc
from test_daypass_payments import (  # noqa: F401  (fixtures: clock)
    EVENT, WED_1030, Feed, Resp, Wallet, bolt11, clock)
import test_main_loop_events as loop
from test_main_loop_events import KEY, run_loop
from test_main_loop_events import sent  # noqa: F401  (fixture)

MODES = ("live", "dry_run", "off")
TOKEN = "L402 MACRUN:pre"
PASS_TOK = "L402 MACPASS:paid"


class Recorder(Feed):
    """The scripted feed of test_daypass_payments, also keeping every
    request's url, params and headers."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append((url, dict(params or {}), dict(headers or {})))
        if url.endswith("/pass"):
            return Resp({"status": "active",
                         "access_until": "2030-01-02T00:00:00+00:00",
                         "trading_days_left": 4})
        return super().get(url, params=params, headers=headers, timeout=timeout)

    def feed_reads(self):
        return [(p, h) for u, p, h in self.requests
                if u.endswith("/day-trade-ideas")]


@pytest.fixture
def loop_clock(monkeypatch):
    """test_main_loop_events' clock (agent's datetime.now), under its own
    name here: `clock` is the source's clock above."""
    c = loop.Clock()

    class Fake(loop.datetime):
        @classmethod
        def now(cls, tz=None):
            return c.t.astimezone(tz) if tz else c.t.replace(tzinfo=None)
    monkeypatch.setattr(agent, "datetime", Fake)
    return c


def _setup(monkeypatch, feed, wallet=None):
    monkeypatch.setattr(hc.requests, "get", feed.get)
    monkeypatch.setattr(hc, "wallet_from_cfg", lambda cfg: wallet)


def _says(reads, mode):
    """Every read carried `mode` as the param and the header."""
    assert reads, "the feed was not read"
    for params, headers in reads:
        assert params.get("mode") == mode
        assert headers.get(hc.MODE_HEADER) == mode


def _running_token(clock, paid_day=None):
    """A wallet day-pass bought today that runs past today's close."""
    return {"token": TOKEN, "expires_at": clock.time() + 20 * 3600,
            "paid_day": paid_day or WED_1030.strftime("%Y-%m-%d")}


# ── the mode is sent on every read ──────────────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
def test_a_running_day_pass_read_says_the_mode(monkeypatch, clock, mode):
    feed = Recorder()
    _setup(monkeypatch, feed, Wallet())
    state = {"l402": _running_token(clock)}
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w", "feed_mode": mode}
    assert [e["event"] for e in hc.poll(cfg, state, lambda s: None)] == ["ENTERED"]
    _says(feed.feed_reads(), mode)
    assert feed.feed_reads()[0][1]["Authorization"] == TOKEN


@pytest.mark.parametrize("mode", MODES)
def test_a_prepaid_pass_is_still_sent_and_says_the_mode(monkeypatch, clock, mode):
    """Practice keeps sending the prepaid token: AgentHC does not charge a
    day for dry_run / off."""
    feed = Recorder()
    _setup(monkeypatch, feed, Wallet())
    cfg = {"source": "agenthc", "feed_pass": {"token": PASS_TOK}, "feed_mode": mode}
    hc.poll(cfg, {}, lambda s: None)
    reads = feed.feed_reads()
    _says(reads, mode)
    assert all(h["Authorization"] == PASS_TOK for _p, h in reads)


@pytest.mark.parametrize("mode", MODES)
def test_a_premium_key_read_says_the_mode(monkeypatch, clock, mode):
    feed = Recorder()
    _setup(monkeypatch, feed)
    hc.poll({"agenthc_api_key": "k", "feed_mode": mode}, {}, lambda s: None)
    reads = feed.feed_reads()
    _says(reads, mode)
    assert all(h["X-API-Key"] == "k" for _p, h in reads)


def test_mode_is_added_without_changing_the_read_params(monkeypatch, clock):
    feed = Recorder()
    _setup(monkeypatch, feed)
    state = {"positions": {}}
    params = hc._feed_params({}, state)
    hc.poll({"agenthc_api_key": "k", "feed_mode": "dry_run"}, state, lambda s: None)
    (sent_params, _h), = feed.feed_reads()
    assert sent_params == {**params, "mode": "dry_run"}


def test_without_the_agents_mode_the_settings_decide():
    """A caller that isn't the agent's loop: paused → off, dry_run → dry_run,
    else live (an older caller keeps today's behaviour)."""
    assert hc.feed_mode({"paused": True, "dry_run": False}) == "off"
    assert hc.feed_mode({"dry_run": True}) == "dry_run"
    assert hc.feed_mode({"dry_run": False}) == "live"
    assert hc.feed_mode({}) == "live"
    assert hc.feed_mode({"feed_mode": "dry_run", "dry_run": False}) == "dry_run"
    assert hc.feed_mode({"feed_mode": "bogus", "dry_run": True}) == "dry_run"
    assert hc.mode_params("off") == ({"mode": "off"}, {"X-Agent-Mode": "off"})


# ── the wallet buys a day-pass only when LIVE ───────────────────────────────

@pytest.mark.parametrize("mode", ["dry_run", "off"])
def test_practice_and_off_never_buy_a_day_pass(monkeypatch, clock, capsys, mode):
    feed, wallet = Recorder(), Wallet()
    _setup(monkeypatch, feed, wallet)
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w", "feed_mode": mode}
    state = {}
    for _ in range(3):
        assert hc.poll(cfg, state, lambda s: None) == []
        clock.tick(minutes=10)
    assert wallet.paid == []
    assert feed.minted == 0 and feed.requests == []   # not even an invoice asked for
    assert "l402" not in state and "daypass_pending" not in state
    assert hc.exit_loss_reason() == "day_pass_not_live"
    out = capsys.readouterr().out
    want = hc.OFF_NO_PASS_TEXT if mode == "off" else hc.PRACTICE_NO_PASS_TEXT
    assert out.count(want) == 1                       # said once a day
    assert state["pass_notice"] == want
    assert "only LIVE uses days" in want


def test_live_still_buys_its_day_pass(monkeypatch, clock):
    feed, wallet = Recorder(), Wallet()
    _setup(monkeypatch, feed, wallet)
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w", "feed_mode": "live"}
    state = {}
    assert [e["event"] for e in hc.poll(cfg, state, lambda s: None)] == ["ENTERED"]
    assert len(wallet.paid) == 1
    _says(feed.feed_reads(), "live")


def test_practice_402_says_practice_buys_no_pass(monkeypatch, clock, capsys):
    """A day-pass from yesterday that AgentHC now answers with 402: live
    would buy today's, practice says plainly it doesn't and buys nothing."""
    feed, wallet = Recorder(refuse=402), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {"l402": {"token": TOKEN, "expires_at": clock.time() + 3 * 3600,
                      "paid_day": "2026-07-14"}}
    cfg = {"day_pass_recurring": True, "lnbits_url": "https://w",
           "feed_mode": "dry_run"}
    assert hc.poll(cfg, state, lambda s: None) == []
    assert wallet.paid == []
    _says(feed.feed_reads(), "dry_run")
    out = capsys.readouterr().out
    assert "Practice never uses a signal day or buys a pass" in out
    assert "practice preview" in out                   # the free fallback


def test_no_day_pass_over_a_used_up_prepaid_pass_in_practice(monkeypatch, clock):
    """A used-up prepaid pass only reads exits; live would buy today's
    day-pass over it — practice never does."""
    feed, wallet = Recorder(), Wallet()
    _setup(monkeypatch, feed, wallet)
    tid = hc._token_id(PASS_TOK)
    state = {"pass_status": {tid: {"status": "exits_only", "until_ts": time.time() + 9e5,
                                   "checked": time.time(), "first_seen": 1.0}}}
    cfg = {"feed_pass": {"token": PASS_TOK}, "day_pass_recurring": True,
           "lnbits_url": "https://w", "feed_mode": "dry_run"}
    hc.poll(cfg, state, lambda s: None)
    assert wallet.paid == [] and feed.minted == 0
    _says(feed.feed_reads(), "dry_run")
    assert all(h["Authorization"] == PASS_TOK for _p, h in feed.feed_reads())


@pytest.mark.parametrize("mode", ["dry_run", "off"])
def test_the_buy_itself_refuses_when_not_live(monkeypatch, clock, mode):
    """The last stop: _buy_day_pass pays nothing when not live, whoever
    calls it."""
    wallet = Wallet()
    monkeypatch.setattr(hc, "wallet_from_cfg", lambda cfg: wallet)
    body = {"payment": {"payment_request": bolt11(10_000), "macaroon": "M",
                        "amount_sats": 10_000}}
    state = {}
    assert hc._buy_day_pass({"day_pass_recurring": True, "feed_mode": mode},
                            state, body, lambda s: None) is False
    assert wallet.paid == [] and "l402" not in state


def test_ended_day_pass_exits_say_off_while_paused(monkeypatch, clock):
    """Paused with a real position: the ended day-pass still fetches its
    exits, saying "off"."""
    feed = Recorder()
    _setup(monkeypatch, feed, Wallet())
    state = {"l402": {"token": TOKEN, "expires_at": clock.time() - 60,
                      "paid_day": "2026-07-14"},
             "positions": {"SPY|2026-07-17|600.0|C": {"option_id": "o", "qty": 1,
                                                       "live": True}}}
    hc.poll({"feed_mode": "off", "day_pass_recurring": True,
             "lnbits_url": "https://w"}, state, lambda s: None)
    reads = feed.feed_reads()
    _says(reads, "off")
    assert reads[0][1]["Authorization"] == TOKEN


# ── the free /closed preview ────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,mode", [
    ({"dry_run": True, "source": "agenthc"}, "dry_run"),
    ({"dry_run": True, "source": "agenthc", "paused": True}, "off")])
def test_the_closed_preview_says_the_mode(home, monkeypatch, cfg, mode):
    seen = []

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"trades": []}
    monkeypatch.setattr(dry_run_preview._http, "get",
                        lambda url, params=None, headers=None, timeout=None:
                        seen.append((url, params, headers)) or R())
    dry_run_preview.refresh(cfg, True, now=1000)
    (url, params, headers), = seen
    assert url.endswith("/day-trade-ideas/closed")
    assert params["mode"] == mode and headers == {"X-Agent-Mode": mode}


# ── the agent's mode (the same helpers its orders use) ──────────────────────

def test_agent_feed_mode(home):
    real = {"positions": {KEY: {"option_id": "stub", "qty": 1}}}
    assert agent._feed_mode({"dry_run": False}) == "live"
    assert agent._feed_mode({"dry_run": True}) == "dry_run"
    # paused: off, also while it still reads exits for a real position
    assert agent._feed_mode({"dry_run": False, "paused": True}, real) == "off"
    assert agent._feed_mode({"dry_run": True, "paused": True}) == "off"
    # never chosen, no real trades: practice (agent._is_dry)
    assert agent._feed_mode({}) == "dry_run"


def _recording_source(seen):
    class Src:
        @staticmethod
        def poll(cfg, st, save_state):
            seen.append(cfg.get("feed_mode"))
            return []
    return Src


@pytest.mark.parametrize("config,mode", [
    ({"dry_run": False}, "live"),
    ({"dry_run": True}, "dry_run")])
def test_the_loop_hands_the_mode_to_every_read(home, monkeypatch, sent,
                                               loop_clock, config, mode):
    seen = []
    run_loop(home, monkeypatch, {"seen": [], "positions": {}}, [[], []],
             clock=loop_clock, source=_recording_source(seen), config=config)
    assert seen == [mode, mode]


def test_a_paused_agent_reading_exits_for_a_real_position_says_off(
        home, monkeypatch, sent, loop_clock):
    """On the pay-per-day feed a paused agent still reads the feed while it
    holds a real position (for its exit signal): that read says "off"."""
    import sources
    seen = []
    src = _recording_source(seen)
    monkeypatch.setitem(sources.SOURCES, "agenthc", src)
    monkeypatch.setitem(agent.SOURCES, "agenthc", src)
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1}}}
    run_loop(home, monkeypatch, state, [[]], clock=loop_clock,
             config={"source": "agenthc", "paused": True, "dry_run": False})
    assert seen == ["off"]


# ── what the owner is told ──────────────────────────────────────────────────

def test_dry_on_and_go_live_replies_say_only_live_uses_days(home):
    """The operator's description (2026-11-18), word for word."""
    assert agent.PRACTICE_MODE_TEXT == (
        "Practice mode logs each trade only after it closes — with the time "
        "AgentHC entered and exited and its recorded result — sized to your "
        "budget and rules. It never uses a signal day or buys a pass.")
    assert agent.LIVE_MODE_TEXT == (
        "Live mode trades signals in real time and uses one signal day on "
        "each trading day you receive signals.")
    (home / "config.json").write_text(json.dumps(
        {"source": "agenthc", "dry_run": False}), encoding="utf-8")
    reply = agent._apply_command("dry on")[1]
    assert agent.PRACTICE_MODE_TEXT in reply
    reply = agent._apply_command("go live")[1]
    assert agent.LIVE_MODE_TEXT in reply
    # another source: no release on close, no signal days
    (home / "config.json").write_text(json.dumps(
        {"source": "manual", "dry_run": False}), encoding="utf-8")
    assert agent.PRACTICE_MODE_TEXT not in agent._apply_command("dry on")[1]
    assert agent.LIVE_MODE_TEXT not in agent._apply_command("go live")[1]


def test_no_signal_warning_names_the_not_live_cause():
    import view_model
    assert "day_pass_not_live" in view_model.EXIT_LOSS_CAUSES
    assert "only LIVE buys a day-pass" in agent._SIGNAL_LOSS_WHY["day_pass_not_live"]
    assert agent._signal_loss_fix({}, "day_pass_not_live")


# ── review follow-ups ───────────────────────────────────────────────────────

WALLET_ONLY = {"source": "agenthc", "lnbits_url": "https://w",
               "day_pass_recurring": True}


def _real_state():
    return {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1}}}


def test_paused_wallet_only_with_a_real_position_is_told_exits_may_stop(
        home, monkeypatch, clock):
    """Paused (or in practice) with a real position and only the wallet's
    day-pass: no new day-pass is bought, so no exit signal is promised —
    the owner is told the no-signal sell closes it unless they buy signal
    days or resume / go live."""
    cfg = {**WALLET_ONLY, "dry_run": False, "paused": True}
    note = agent._pause_note(cfg, _real_state())
    assert "has no prepaid signal days" in note
    assert "does not buy a day-pass while paused" in note
    assert "no-signal sell at 15:30 ET" in note and "or resume" in note
    assert "still reads the signal feed" not in note
    # the pause reply says the same
    (home / "config.json").write_text(json.dumps(
        {**WALLET_ONLY, "dry_run": False}), encoding="utf-8")
    (home / "state.json").write_text(json.dumps(_real_state()), encoding="utf-8")
    reply = agent._apply_command("pause")[1]
    assert "does not buy a day-pass while paused" in reply
    assert "still reads the signal feed to get its exit signal" not in reply
    # and dry-run (the dashboard's / reply's dry-run note)
    agent._apply_command("resume")
    reply = agent._apply_command("dry on")[1]
    assert "does not buy a day-pass while in practice" in reply
    assert "or go live" in reply
    # the no-signal sell switched off: sell it yourself
    off = agent._pause_note({**cfg, "signal_loss_sell_et": "off"}, _real_state())
    assert "switched off" in off and "sell it yourself" in off


def test_a_prepaid_pass_or_key_keeps_the_exit_signal_promise(home, clock):
    real = _real_state()
    cfg = {**WALLET_ONLY, "dry_run": False, "paused": True,
           "feed_pass": {"token": PASS_TOK}}
    assert "still reads the signal feed" in agent._pause_note(cfg, real)
    assert agent._no_pass_exits_note(cfg, real, "paused") == ""
    keyed = {**WALLET_ONLY, "agenthc_api_key": "k"}
    assert agent._no_pass_exits_note(keyed, real, "paused") == ""
    # no real position: nothing to say
    assert agent._no_pass_exits_note(WALLET_ONLY, {"positions": {}}, "paused") == ""


def test_paused_wallet_only_no_running_pass_drives_the_no_signal_sell(
        home, monkeypatch, clock, capsys):
    """No running day-pass (and the ended one already said it has no
    exits): poll reads nothing, the cause is day_pass_not_live, and the
    no-signal warning names it and the fix."""
    feed, wallet = Recorder(), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = _real_state()
    cfg = {**WALLET_ONLY, "dry_run": False, "paused": True, "feed_mode": "off"}
    assert hc.poll(cfg, state, lambda s: None) == []
    assert feed.requests == [] and wallet.paid == []
    assert not hc.had_exit_access()
    assert hc.exit_loss_reason() == "day_pass_not_live"
    sent_msgs = []
    monkeypatch.setattr(agent, "notify",
                        lambda c, msg, **k: sent_msgs.append(msg))
    agent._note_exit_source(cfg, state, lambda s: None, "lost",
                            hc.exit_loss_reason(), now_et=WED_1030)
    assert state["signal_loss"]["cause"] == "day_pass_not_live"
    later = WED_1030 + timedelta(seconds=agent.SIGNAL_LOSS_WARN_S + 60)
    agent.signal_loss_tick(cfg, state, lambda s: None, None, None,
                           now_et=later)
    assert sent_msgs, "no no-signal warning went out"
    assert "only LIVE buys a day-pass" in sent_msgs[0]
    assert "buy signal days" in sent_msgs[0]


@pytest.mark.parametrize("extra", [
    {},                                                  # fresh install
    {"lnbits_url": "https://w"},                         # wallet, Recurring off
    {"day_pass_recurring": True}])                       # Recurring, no wallet
def test_practice_without_a_buying_wallet_does_not_blame_practice(
        monkeypatch, clock, capsys, extra):
    feed = Recorder()
    _setup(monkeypatch, feed, None)
    state = {}
    cfg = {**extra, "feed_mode": "dry_run"}
    assert hc.poll(cfg, state, lambda s: None) == []
    out = capsys.readouterr().out
    assert hc.PRACTICE_NO_PASS_TEXT not in out
    assert state.get("pass_notice") != hc.PRACTICE_NO_PASS_TEXT
    assert hc.exit_loss_reason() != "day_pass_not_live"
    if not extra.get("day_pass_recurring"):
        assert hc._day_pass_loss_code(cfg, state) == "day_pass_off"


def test_going_live_takes_the_practice_notice_off(monkeypatch, clock):
    feed, wallet = Recorder(), Wallet()
    _setup(monkeypatch, feed, wallet)
    state = {}
    hc.poll({**WALLET_ONLY, "feed_mode": "dry_run"}, state, lambda s: None)
    assert state["pass_notice"] == hc.PRACTICE_NO_PASS_TEXT
    # live, Recurring off: nothing is bought and the notice is gone
    live_off = {"lnbits_url": "https://w", "feed_mode": "live"}
    assert hc.poll(live_off, state, lambda s: None) == []
    assert "pass_notice" not in state and "not_live_pass_notified" not in state
    # paused, then live with the wallet buying: gone as well
    hc.poll({**WALLET_ONLY, "feed_mode": "off"}, state, lambda s: None)
    assert state["pass_notice"] == hc.OFF_NO_PASS_TEXT
    assert hc.poll({**WALLET_ONLY, "feed_mode": "live"}, state, lambda s: None)
    assert state.get("pass_notice") not in (hc.PRACTICE_NO_PASS_TEXT,
                                            hc.OFF_NO_PASS_TEXT)


def test_another_problem_notice_is_left_alone(monkeypatch, clock):
    state = {"pass_notice": hc.PASS_RUN_OUT_TEXT}
    hc._clear_not_live_notice(state, lambda s: None)
    assert state["pass_notice"] == hc.PASS_RUN_OUT_TEXT


# ── _feed_mode: the paths that decide whether a live agent is charged ──────

def test_feed_mode_unset_or_settled_live_is_live(home, monkeypatch):
    """Settings that never chose a mode on a setup that has traded live run
    LIVE (agent._is_dry): the feed must hear "live", or a live agent would
    stop paying for its signals."""
    monkeypatch.setattr(agent, "_SETTLED_DRY", {"dry": False, "warned": False})
    assert agent._feed_mode({}) == "live"
    monkeypatch.setattr(agent, "_SETTLED_DRY", {"dry": None, "warned": False})
    assert agent._feed_mode({}, _real_state()) == "live"
    assert agent._feed_mode({"dry_run": None}, _real_state()) == "live"
    (home / "trades.jsonl").write_text(json.dumps(
        {"action": "entry", "dry": False}) + "\n", encoding="utf-8")
    assert agent._feed_mode({}, {"positions": {}}) == "live"
    # settled practice stays practice
    monkeypatch.setattr(agent, "_SETTLED_DRY", {"dry": True, "warned": False})
    assert agent._feed_mode({}) == "dry_run"


def test_feed_mode_selling_only_after_a_move_is_off(home, monkeypatch):
    import transfer
    monkeypatch.setattr(transfer, "exits_only", lambda: {"at": 1})
    assert agent._feed_mode({"dry_run": False}, _real_state()) == "off"


def test_the_loop_says_live_for_an_unset_mode_with_a_real_position(
        home, monkeypatch, sent, loop_clock):
    monkeypatch.setattr(agent, "_SETTLED_DRY", {"dry": None, "warned": False})
    seen = []
    run_loop(home, monkeypatch, _real_state(), [[], []], clock=loop_clock,
             source=_recording_source(seen), config={})
    assert seen == ["live", "live"]


def test_an_alpaca_paper_account_with_live_selected_reads_as_live(
        home, monkeypatch, clock):
    """It receives the signals in real time, so it is LIVE and is charged:
    the paper-trading account changes nothing about the mode it says."""
    cfg = {"source": "agenthc", "broker": "alpaca", "alpaca_paper": True,
           "dry_run": False, "feed_pass": {"token": PASS_TOK}}
    assert agent._feed_mode(cfg) == "live"
    feed = Recorder()
    _setup(monkeypatch, feed, Wallet())
    hc.poll({**cfg, "feed_mode": agent._feed_mode(cfg)}, {}, lambda s: None)
    _says(feed.feed_reads(), "live")


def test_go_live_reply_no_history_claim_and_alpaca_paper(home):
    (home / "config.json").write_text(json.dumps(
        {"source": "agenthc", "dry_run": True}), encoding="utf-8")
    reply = agent._apply_command("go live")[1]
    assert "practice never did" not in reply
    assert "Alpaca paper" not in reply
    (home / "config.json").write_text(json.dumps(
        {"source": "agenthc", "dry_run": True, "broker": "alpaca",
         "alpaca_paper": True}), encoding="utf-8")
    reply = agent._apply_command("go live")[1]
    assert agent.ALPACA_PAPER_LIVE_TEXT in reply
    assert "receives the signals in real time and uses signal days" in reply
