"""Broker-adapter audit fixes (moomoo sizing and exit pricing, exit marks,
console-safe prints, OpenD-down reporting). Zero network: fakes only."""

import io
import sys

import pytest

import broker_events
import platform_compat
from brokers import alpaca, moomoo, robinhood, robinhood_mcp
from conftest import make_event

KEY = "SPY|2026-07-10|752.0|C"
CODE = "US.SPY260710C752000"


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def iloc(self):
        return self._rows


class FakeMoomoo:
    """Stand-in for the moomoo-api package: one contract quoted bid/ask;
    every order fills at once. quote_error = what opening a quote
    connection raises (OpenD not running)."""
    RET_OK = 0

    class TrdEnv:
        SIMULATE, REAL = "SIM", "REAL"

    class TrdSide:
        BUY, SELL = "BUY", "SELL"

    class OrderType:
        NORMAL = "NORMAL"

    class TrdMarket:
        US = "US"

    class SecurityFirm:
        FUTUINC = "FUTUINC"

    def __init__(self, bid=1.00, ask=1.20, quote_error=None, quote_ret=None):
        self.bid, self.ask = bid, ask
        self.quote_error, self.quote_ret = quote_error, quote_ret
        self.orders = []
        self.holdings = {CODE: 2}  # the account holds the ledger rows' contract

    def OpenQuoteContext(self, **kw):
        sdk = self
        if sdk.quote_error is not None:
            raise sdk.quote_error

        class Quotes:
            def get_market_snapshot(self, codes):
                if sdk.quote_ret is not None:
                    return sdk.quote_ret
                return sdk.RET_OK, _Rows([{"bid_price": sdk.bid, "ask_price": sdk.ask,
                                           "last_price": sdk.ask}])

            def close(self):
                pass
        return Quotes()

    def OpenSecTradeContext(self, **kw):
        sdk = self

        class Trades:
            def place_order(self, **kw):
                sdk.orders.append(kw)
                return sdk.RET_OK, _Rows([{"order_id": f"mm-{len(sdk.orders)}"}])

            def order_list_query(self, **kw):
                if not kw.get("order_id"):  # no other order is working
                    return sdk.RET_OK, _Rows([])
                return sdk.RET_OK, _Rows([{"order_id": kw.get("order_id"),
                                           "order_status": "FILLED_ALL",
                                           "dealt_qty": sdk.orders[-1]["qty"]}])

            def position_list_query(self, **kw):  # what the account holds
                return sdk.RET_OK, _Rows([{"code": c, "qty": q}
                                          for c, q in sdk.holdings.items()])

            def close(self):
                pass
        return Trades()


def _mm_cfg(**over):
    cfg = {"broker": "moomoo", "moomoo_paper": True, "sizing_mode": "budget",
           "budget_per_trade_usd": 2000, "max_contracts_per_trade": 100}
    cfg.update(over)
    return cfg


@pytest.fixture
def mm(monkeypatch):
    sdk = FakeMoomoo()
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    return sdk


# ── B2: moomoo sizes from the config the agent passes per call ──────────────

def test_moomoo_sizes_from_the_per_call_config_not_the_startup_copy(mm):
    """The owner lowers the budget in the dashboard while the agent runs:
    the agent passes the new config with every event, and the next buy must
    use it. (The client keeps the config it was started with.)"""
    client = moomoo.client(_mm_cfg(budget_per_trade_usd=2000))
    now = _mm_cfg(budget_per_trade_usd=200)          # "budget 200" in the dashboard
    state = {"positions": {}}
    assert moomoo.execute(client, now, make_event(), state)
    assert mm.orders[0]["qty"] == 1                  # $200 // ($1.20 x 100), not 16
    assert state["positions"][KEY]["qty"] == 1


def test_moomoo_percent_mode_uses_the_budget_the_agent_resolved(mm):
    """%-of-account: the agent turns the % into a dollar budget per entry
    and passes it in the per-call config. Sizing from the startup copy
    (still "percent") skipped every entry."""
    client = moomoo.client(_mm_cfg(sizing_mode="percent", percent_per_trade=5))
    resolved = _mm_cfg(sizing_mode="budget", budget_per_trade_usd=360)
    state = {"positions": {}}
    assert moomoo.execute(client, resolved, make_event(), state)
    assert mm.orders[0]["qty"] == 3


def test_moomoo_other_track_sizing_reaches_the_order(mm):
    import agent
    cfg = _mm_cfg(budget_per_trade_usd=1200, other_budget_per_trade_usd=120)
    client = moomoo.client(cfg)
    ev = make_event(track="other")
    assert moomoo.execute(client, agent._sizing_cfg(cfg, ev), ev, {"positions": {}})
    assert mm.orders[0]["qty"] == 1                  # the $120 "other trades" budget


def test_moomoo_account_and_gateway_still_come_from_the_startup_copy(mm):
    """Only sizing follows the per-call config: which account the order
    goes to (paper or real) stays the one the agent was started with,
    like its holdings reads and position tags."""
    client = moomoo.client(_mm_cfg(moomoo_paper=True))
    now = _mm_cfg(moomoo_paper=False, moomoo_trade_pwd="x", budget_per_trade_usd=200)
    assert moomoo.execute(client, now, make_event(), {"positions": {}})
    assert mm.orders[0]["trd_env"] == "SIM"


# ── B3: moomoo feed exits sell at the bid ───────────────────────────────────

def test_moomoo_feed_exit_is_priced_at_the_bid_not_the_ask(mm):
    state = {"positions": {KEY: {"option_id": CODE, "qty": 2}}}
    assert moomoo.execute(None, _mm_cfg(), make_event(event="EXITED"), state)
    (order,) = mm.orders
    assert order["trd_side"] == "SELL" and order["qty"] == 2
    assert order["price"] == 1.00                    # the bid; the ask is 1.20
    assert state["positions"] == {}


def test_moomoo_exit_with_no_bid_still_goes_out(mm):
    """No bid quoted: the sell is still sent (at the ask / last trade, as
    before) instead of waiting for a bid that may never come."""
    mm.bid = 0.0
    state = {"positions": {KEY: {"option_id": CODE, "qty": 1}}}
    assert moomoo.execute(None, _mm_cfg(), make_event(event="EXITED"), state)
    assert mm.orders[0]["price"] == 1.20


def test_moomoo_marketable_exit_keeps_bid_minus_five_percent(mm):
    state = {"positions": {KEY: {"option_id": CODE, "qty": 1}}}
    ev = make_event(event="EXITED", exit_limit="marketable")
    assert moomoo.execute(None, _mm_cfg(), ev, state)
    assert mm.orders[0]["price"] == 0.95


def test_moomoo_entry_still_buys_at_the_ask(mm):
    assert moomoo.execute(None, _mm_cfg(), make_event(), {"positions": {}})
    assert mm.orders[0]["trd_side"] == "BUY" and mm.orders[0]["price"] == 1.20


# ── B4: an exit is marked at the bid ────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def _alpaca_quote(monkeypatch, bid, ask):
    occ = alpaca._occ_symbol(make_event())
    monkeypatch.setattr(
        alpaca.requests, "get",
        lambda url, **kw: _Resp(200, {"quotes": {occ: {"bp": bid, "ap": ask}}}))


def test_alpaca_marks_a_sell_at_the_bid(monkeypatch):
    _alpaca_quote(monkeypatch, bid=1.00, ask=1.20)
    cl = alpaca._Client({"alpaca_key_id": "k", "alpaca_secret": "s"})
    assert alpaca.mark_price(cl, {}, make_event(), "sell") == 1.00
    assert alpaca.mark_price(cl, {}, make_event(), "buy") == 1.20
    assert alpaca.mark_price(cl, {}, make_event()) == 1.20


def test_alpaca_sell_mark_without_a_bid_is_unavailable_not_the_ask(monkeypatch):
    _alpaca_quote(monkeypatch, bid=0, ask=1.20)
    cl = alpaca._Client({"alpaca_key_id": "k", "alpaca_secret": "s"})
    assert alpaca.mark_price(cl, {}, make_event(), "sell") == 0.0


def test_moomoo_marks_a_sell_at_the_bid(mm):
    cl = moomoo.client(_mm_cfg())
    assert moomoo.mark_price(cl, {}, make_event(), "sell") == 1.00
    assert moomoo.mark_price(cl, {}, make_event(), "buy") == 1.20
    mm.bid = 0.0
    assert moomoo.mark_price(cl, {}, make_event(), "sell") == 0.0


def test_exit_pnl_estimate_uses_the_bid(mm):
    """Bought at 1.50, the market is now 1.00 bid / 1.20 ask: the 2
    contracts sell at the bid, so the estimate is -$100, not -$60."""
    import agent
    cl = moomoo.client(_mm_cfg())
    mark = agent._mark(moomoo, cl, _mm_cfg(), make_event(event="EXITED"), "sell")
    assert agent._est_exit_pnl({"qty": 2, "entry_mark": 1.50}, mark, {}) == -100.0


# ── B5: a status line can never stop an order ───────────────────────────────

def _cp1252_stdout(monkeypatch):
    """Swap in stdout the way Windows gives it to a redirected process the
    supervisor didn't start: the legacy code page, strict errors. Called
    inside the test (pytest re-installs its own stdout for each phase).
    Returns a function that reads back what was printed."""
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                           line_buffering=True)
    monkeypatch.setattr(sys, "stdout", out)

    def printed():
        out.flush()
        return raw.getvalue().decode("cp1252")
    return printed


def test_safe_print_replaces_what_the_console_cannot_show(monkeypatch):
    printed = _cp1252_stdout(monkeypatch)
    platform_compat.safe_print("$500 budget @ ~$2.50 → 2x", "✓")
    assert printed().strip() == "$500 budget @ ~$2.50 ? 2x ?"


def test_safe_print_never_raises_on_a_broken_stream(monkeypatch):
    class Broken:
        encoding = "utf-8"

        def write(self, s):
            raise OSError("pipe closed")

        def flush(self):
            raise OSError("pipe closed")
    monkeypatch.setattr(sys, "stdout", Broken())
    platform_compat.safe_print("still fine →")


def test_robinhood_budget_entry_is_placed_on_a_cp1252_console(monkeypatch):
    """The budget line contains "→", which cp1252 can't encode. print()
    raised before the order went out, so no budget-mode entry was placed."""
    printed = _cp1252_stdout(monkeypatch)
    placed = []
    monkeypatch.setattr(robinhood, "resolve_instrument", lambda rh, ev: "opt-1")
    monkeypatch.setattr(robinhood, "quote_price", lambda rh, oid, side="buy": 2.50)
    monkeypatch.setattr(robinhood, "place",
                        lambda rh, cfg, opt, side, effect, qty, **kw:
                        placed.append((side, qty)) or
                        {"order_id": "o-1", "was_limit": False})
    monkeypatch.setattr(robinhood, "_settle_fast", lambda *a: None)
    cfg = {"robinhood_account": "A1", "sizing_mode": "budget",
           "budget_per_trade_usd": 500}
    state = {"positions": {}}
    assert robinhood.execute(None, cfg, make_event(), state)
    assert placed == [("buy", 2)]
    assert "buying 2x" in printed()


def test_alpaca_budget_entry_is_placed_on_a_cp1252_console(monkeypatch):
    _cp1252_stdout(monkeypatch)
    placed = []
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 2.50)
    monkeypatch.setattr(alpaca, "_order",
                        lambda cfg, occ, side, qty, **kw:
                        placed.append((side, qty)) or "a-1")
    monkeypatch.setattr(alpaca, "_confirm_fill",
                        lambda cfg, oid, qty, side="buy": qty)
    cfg = {"alpaca_key_id": "k", "alpaca_secret": "s", "sizing_mode": "budget",
           "budget_per_trade_usd": 500}
    assert alpaca.execute(alpaca._Client(cfg), cfg, make_event(), {"positions": {}})
    assert placed == [("buy", 2)]


def test_moomoo_budget_entry_is_placed_on_a_cp1252_console(mm, monkeypatch):
    _cp1252_stdout(monkeypatch)
    assert moomoo.execute(None, _mm_cfg(budget_per_trade_usd=500), make_event(),
                          {"positions": {}})
    assert mm.orders[0]["qty"] == 4


def test_robinhood_partial_exit_note_cannot_raise_on_cp1252(monkeypatch):
    """The "⚠️ exit only filled" line runs while the ledger is being
    settled: it must not raise half-way through."""
    _cp1252_stdout(monkeypatch)
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "pending": {
        "order_id": "o-1", "side": "sell", "requested": 2, "filled": 1}}}}
    broker_events.begin()
    robinhood._finalize_pending(state, KEY, 1)
    assert state["positions"][KEY]["qty"] == 1
    assert [e["kind"] for e in broker_events.drain()] == ["exit_partial"]


def test_robinhood_connection_notes_cannot_raise_on_cp1252(tmp_path, monkeypatch):
    """"⚠️ connection lost" and "✓ restored" are printed inside the call
    that talks to Robinhood: raising there would lose the call's answer."""
    _cp1252_stdout(monkeypatch)
    rh = robinhood_mcp.RobinhoodMCP(str(tmp_path / "rh.json"))
    broker_events.begin()
    rh._mark_down("can't reach Robinhood (ReadTimeout)")
    assert [e["kind"] for e in broker_events.drain()] == ["broker_disconnected"]
    rh._mark_ok()
    assert rh.last_error is None


# ── B6: OpenD not running is a connection problem, not a missing quote ──────

def test_moomoo_gateway_down_is_reported_as_a_connection_failure(monkeypatch, capsys):
    sdk = FakeMoomoo(quote_error=ConnectionRefusedError("connection refused"))
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {}}
    broker_events.begin()
    assert not moomoo.execute(None, _mm_cfg(moomoo_port=11111), make_event(), state)
    (ev,) = broker_events.drain()
    assert ev["kind"] == "broker_disconnected"
    assert "OpenD" in ev["message"] and "127.0.0.1:11111" in ev["message"]
    assert "running" in ev["message"] and "subscription" not in ev["message"]
    assert "subscription" not in capsys.readouterr().out
    assert sdk.orders == [] and state["positions"] == {}


def test_moomoo_gateway_down_on_exit_says_so_and_keeps_the_position(monkeypatch):
    sdk = FakeMoomoo(quote_error=ConnectionRefusedError("connection refused"))
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {KEY: {"option_id": CODE, "qty": 1}}}
    broker_events.begin()
    assert not moomoo.execute(None, _mm_cfg(), make_event(event="EXITED"), state)
    (ev,) = broker_events.drain()
    assert ev["kind"] == "broker_disconnected" and "OpenD" in ev["message"]
    assert KEY in state["positions"] and sdk.orders == []


def test_moomoo_missing_quote_with_the_gateway_up_keeps_its_message(monkeypatch):
    sdk = FakeMoomoo(quote_ret=(-1, "No right to get the quote"))
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    broker_events.begin()
    assert not moomoo.execute(None, _mm_cfg(), make_event(), {"positions": {}})
    (ev,) = broker_events.drain()
    assert ev["kind"] == "skip_no_quote" and "subscription" in ev["message"]
    assert "No right to get the quote" in ev["message"]   # OpenD's own reason
