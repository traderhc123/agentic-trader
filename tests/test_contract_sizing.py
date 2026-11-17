"""Contract sizing: an owner who sizes trades above 10 contracts gets them.

Maintainer report (2026-11-17): "a contract default issue too... when users
set budgets to more than 10 contracts per trade". What was wrong:

B1  setup --example-plan (which BOOT.md tells assistants to copy) set
    max_contracts_per_trade to 10, so a plan built from it cut EVERY trade
    to 10 contracts in every sizing mode, and the report still said the
    full size.
B2  nothing could show or change max_contracts_per_trade: no wizard field,
    no /v2 row, no chat command ('set max contracts 50' was answered as a
    question), and 'set contracts 15' said 15 while 10 were bought.
B3  practice mode on Alpaca and moomoo (and on Robinhood when the contract
    lookup failed) ignored the budget and booked contracts_per_trade (1).
B4  a trade the cap cut was logged like a full one (Alpaca and moomoo even
    said "→ 25x" next to a 10x buy).
B5  re-running the terminal setup reset sizing, the daily cap (and took 0)
    and the other-trades choice to their defaults.
B6  practice positions counted toward max_open_positions after going live,
    and a practice row in a contract silently blocked the real buy of it.
B8  Alpaca bought contracts_per_trade in budget mode with no budget set.
"""

import json
from pathlib import Path

import pytest

import agent
import setup_plan
import view_model
import webui
from brokers import alpaca, moomoo, robinhood
from conftest import StubBroker, make_event


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())


def _write_cfg(home, cfg):
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _cfg(home):
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def _log(home):
    p = home / "trades.jsonl"
    return ([json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
            if p.exists() else [])


BUDGET = {"sizing_mode": "budget", "budget_per_trade_usd": 5000,
          "contracts_per_trade": 1}


@pytest.fixture
def priced(monkeypatch):
    """Every adapter quotes the contract at $2.00 (ask) — no network."""
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 2.0)
    monkeypatch.setattr(moomoo, "_sdk", lambda: object())
    monkeypatch.setattr(moomoo, "_ask_price", lambda cfg, sdk, code: 2.0)
    monkeypatch.setattr(robinhood, "resolve_instrument", lambda rh, ev: "opt-1")
    monkeypatch.setattr(robinhood, "quote_price", lambda rh, oid, side="buy": 2.0)


def _live_size(name, cfg):
    """(qty, note) the live buy of each adapter sizes at $2.00."""
    if name == "alpaca":
        return alpaca._size(cfg, "OCC")
    if name == "moomoo":
        return moomoo._size(cfg, 2.0)
    return robinhood.size_contracts(None, cfg, "opt-1")


ADAPTERS = {"alpaca": alpaca, "moomoo": moomoo, "robinhood": robinhood}


# ── B1: the example plan never caps trades at 10 ────────────────────────────

def test_example_plan_never_caps_below_the_default():
    cap = setup_plan.EXAMPLE_PLAN.get("safety", {}).get("max_contracts_per_trade")
    assert cap is None or cap >= setup_plan._CAP_DEFAULT["max_contracts_per_trade"]


def test_a_plan_from_the_example_buys_the_whole_budget(home, priced):
    plan = {"sizing": {"mode": "budget", "budget_per_trade_usd": 5000},
            "safety": dict(setup_plan.EXAMPLE_PLAN["safety"])}
    new, report = setup_plan.apply(plan, {}, True, str(home))
    assert report["ok"], report
    assert new.get("max_contracts_per_trade", 100) >= 100
    for name in ADAPTERS:
        assert _live_size(name, new)[0] == 25, name     # $5,000 / $200


def test_a_plan_whose_cap_cuts_the_size_says_so(home):
    new, report = setup_plan.apply(
        {"sizing": {"mode": "budget", "budget_per_trade_usd": 5000},
         "safety": {"max_contracts_per_trade": 10}}, {}, True, str(home))
    assert report["ok"] and new["max_contracts_per_trade"] == 10  # still applied
    assert any("no trade buys more than 10" in w for w in report["warnings"])
    assert "at most 10 contracts" in report["sizing"]
    new, report = setup_plan.apply(
        {"sizing": {"mode": "contracts", "contracts_per_trade": 15},
         "safety": {"max_contracts_per_trade": 10}}, {}, True, str(home))
    assert any("each trade buys 10" in w for w in report["warnings"])
    # at the default cap: nothing to warn about
    _new, report = setup_plan.apply(
        {"sizing": {"mode": "contracts", "contracts_per_trade": 15}}, {}, True,
        str(home))
    assert report["warnings"] == []


def test_a_live_plan_still_cannot_raise_the_cap(home):
    live = {"dry_run": False, "max_contracts_per_trade": 10}
    new, report = setup_plan.apply({"safety": {"max_contracts_per_trade": 100}},
                                   live, True, str(home))
    assert new is None
    assert "max_contracts_per_trade from 10 to 100" in report["errors"][0]


# ── B2: the cap can be seen and changed ─────────────────────────────────────

CAPPED = {"source": "agenthc", "broker": "alpaca", "dry_run": False,
          "sizing_mode": "budget", "budget_per_trade_usd": 500,
          "contracts_per_trade": 1, "max_contracts_per_trade": 10}


@pytest.mark.parametrize("text", [
    "set max contracts 50",
    "set max contracts to 50",
    "set max contracts per trade 50",
    "set contract cap 50",
    "set contracts cap 50",
    "set contract limit 50",
    "set max_contracts_per_trade 50",
    "max contracts 50",
    "max contracts per trade 50",
    "Set Max Contracts 50",
])
def test_chat_sets_max_contracts_in_the_words_owners_use(home, text):
    _write_cfg(home, CAPPED)
    handled, reply = agent._apply_command(text)
    assert handled and "set to 50" in reply, reply
    cfg = _cfg(home)
    assert cfg["max_contracts_per_trade"] == 50
    assert cfg["contracts_per_trade"] == 1 and cfg["sizing_mode"] == "budget"


@pytest.mark.parametrize("text", [
    "set max contracts 0", "set max contracts 1001", "set max contracts 2.5",
    "set max contracts fifty", "set contract cap 50 please",
])
def test_chat_max_contracts_out_of_range_changes_nothing(home, text):
    _write_cfg(home, CAPPED)
    before = (home / "config.json").read_text(encoding="utf-8")
    handled, reply = agent._apply_command(text)
    assert handled and reply.startswith("Nothing was changed.")
    assert "set max contracts" in reply
    assert (home / "config.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("text", ["max contracts", "max contracts?",
                                  "contract cap"])
def test_chat_shows_max_contracts(home, text):
    _write_cfg(home, CAPPED)
    handled, reply = agent._apply_command(text)
    assert handled and "Max contracts per trade: 10" in reply


@pytest.mark.parametrize("text", ["set contracts 20", "set contract 20",
                                  "set contracts per trade 20",
                                  "set contracts to 20"])
def test_chat_sets_the_contract_count_in_more_words(home, text):
    _write_cfg(home, CAPPED)
    handled, reply = agent._apply_command(text)
    assert handled and "20 contract(s)/trade" in reply, reply
    cfg = _cfg(home)
    assert cfg["sizing_mode"] == "contracts" and cfg["contracts_per_trade"] == 20
    assert cfg["max_contracts_per_trade"] == 10      # the cap is not the size


def test_set_contracts_above_the_cap_says_how_many_are_bought(home):
    _write_cfg(home, CAPPED)
    _handled, reply = agent._apply_command("set contracts 15")
    assert "capped at 10" in reply and "10 are bought, not 15" in reply
    assert "set max contracts" in reply
    cfg = _cfg(home)
    assert agent._sizing_desc(cfg).startswith("15 contract(s)/trade — capped at 10")
    # raise the cap: the full size is bought
    agent._apply_command("set max contracts 15")
    assert alpaca._size(_cfg(home), "OCC") == (15, "fixed contract count")


def test_set_budget_names_a_cap_below_the_default(home):
    _write_cfg(home, CAPPED)
    _handled, reply = agent._apply_command("set budget 5000")
    assert "No trade buys more than 10 contracts" in reply
    _write_cfg(home, {**CAPPED, "max_contracts_per_trade": 100})
    _handled, reply = agent._apply_command("set budget 5000")
    assert "contracts" not in reply


def test_status_sizing_line_shows_the_cap():
    assert agent._sizing_desc({**BUDGET, "max_contracts_per_trade": 10}) == \
        "$5,000/trade budget, at most 10 contracts"
    assert agent._sizing_desc(BUDGET) == "$5,000/trade budget"  # default: as before


@pytest.fixture
def app(home, monkeypatch):
    made = {}

    class NoPort:
        def __init__(self, addr, handler):
            made["handler"] = handler

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(webui, "_AppServer", NoPort)

    def start(cfg):
        _write_cfg(home, cfg)
        webui.start_app(lambda: {"mode": "TEST", "fields": {}}, lambda: [],
                        agent._apply_command,
                        lambda: agent._load(agent.CONFIG_PATH, {}) or {})
        return made["handler"]
    return start


def test_safety_form_saves_max_contracts(home, app):
    h = app(CAPPED)
    assert h.routes_get["/api/state"](None)["max_contracts_per_trade"] == 10
    r = h.routes_post["/api/safety"](None, {"max_contracts": "40", "seeded": True})
    assert r["ok"], r
    assert _cfg(home)["max_contracts_per_trade"] == 40


def test_a_page_from_before_the_update_cannot_raise_max_contracts(home, app):
    h = app(CAPPED)
    assert h.routes_post["/api/safety"](None, {"max_contracts": "40"})["ok"]
    assert _cfg(home)["max_contracts_per_trade"] == 10
    assert h.routes_post["/api/safety"](None, {"max_contracts": "5"})["ok"]
    assert _cfg(home)["max_contracts_per_trade"] == 5
    # left out: kept
    assert h.routes_post["/api/safety"](None, {"seeded": True})["ok"]
    assert _cfg(home)["max_contracts_per_trade"] == 5


@pytest.mark.parametrize("bad", ["0", "1001", "abc", "2.5", "-3", "nan", "inf"])
def test_safety_form_refuses_max_contracts_out_of_range(home, app, bad):
    h = app(CAPPED)
    before = (home / "config.json").read_text(encoding="utf-8")
    r = h.routes_post["/api/safety"](None, {"max_contracts": bad, "seeded": True,
                                            "dry": True})
    assert r["ok"] is False and "Nothing was changed" in r["error"]
    assert (home / "config.json").read_text(encoding="utf-8") == before


def test_v2_settings_show_max_contracts(home, app):
    h = app(CAPPED)
    lim = h.routes_get["/api/v2/settings"](None)["limits"]
    assert lim["max_contracts_per_trade"] == 10
    assert lim["defaults"]["max_contracts_per_trade"] == 100
    assert lim["allowed"]["max_contracts_per_trade"] == (1, 1000)


def test_every_place_uses_the_same_contract_cap_range():
    assert webui._V2_LIMIT_RANGES["max_contracts_per_trade"] == agent.CONTRACT_CAP_RANGE
    import brokers.pricing as pricing
    assert pricing.CONTRACT_CAP_DEFAULT == agent.CONTRACT_CAP_DEFAULT
    assert setup_plan._CAP_DEFAULT["max_contracts_per_trade"] == agent.CONTRACT_CAP_DEFAULT


def test_dashboard_view_shows_the_default_cap_when_never_set():
    size = view_model._trade_size({"sizing_mode": "budget",
                                   "budget_per_trade_usd": 500})
    assert size["max_contracts"] == 100


def test_built_pages_have_the_max_contracts_field():
    dist = Path(webui.__file__).parent / "webui_dist" / "assets"
    wizard = "".join(p.read_text(encoding="utf-8") for p in dist.glob("index-*.js"))
    assert "sf-max-contracts" in wizard and "Most contracts in one trade" in wizard
    v2 = "".join(p.read_text(encoding="utf-8") for p in dist.glob("v2-*.js"))
    assert "max_contracts_per_trade" in v2 and "Most contracts in one trade" in v2
    src = Path(webui.__file__).read_text(encoding="utf-8")
    assert 'id="sf-maxc"' in src and "body.max_contracts=maxc" in src


# ── B3: practice books what live would buy ──────────────────────────────────

@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_practice_books_the_budget_size_on_every_broker(priced, name):
    cfg = {**BUDGET, "max_contracts_per_trade": 100}
    state = {"positions": {}}
    out = agent._execute_dry(cfg, make_event(), state, ADAPTERS[name], object())
    assert out == "entry"
    (row,) = state["positions"].values()
    assert row["qty"] == _live_size(name, cfg)[0] == 25


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_practice_skips_a_budget_too_small_like_live(priced, name):
    cfg = {**BUDGET, "budget_per_trade_usd": 100}
    state, skips = {"positions": {}}, []
    out = agent._execute_dry(cfg, make_event(), state, ADAPTERS[name], object(),
                             on_skip=lambda kind, note: skips.append(kind))
    assert out is None and state["positions"] == {}
    assert skips == ["skip_budget"] and _live_size(name, cfg)[0] == 0


def test_practice_never_books_one_contract_when_budget_cannot_be_priced(
        monkeypatch):
    monkeypatch.setattr(robinhood, "resolve_instrument", lambda rh, ev: "")
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 0.0)
    for broker, client in ((robinhood, object()), (alpaca, object()),
                           (None, None), (object(), object())):
        state, skips = {"positions": {}}, []
        out = agent._execute_dry(BUDGET, make_event(), state, broker, client,
                                 on_skip=lambda kind, note: skips.append(kind))
        assert out is None and state["positions"] == {}, broker
        assert skips == ["skip_no_quote"], broker


def test_practice_fixed_count_needs_no_quote():
    cfg = {"sizing_mode": "contracts", "contracts_per_trade": 7}
    state = {"positions": {}}
    assert agent._execute_dry(cfg, make_event(), state, None, None) == "entry"
    assert list(state["positions"].values())[0]["qty"] == 7


def test_practice_entry_through_handle_event_on_alpaca(home, no_policy, priced):
    cfg = {**BUDGET, "dry_run": True, "broker": "alpaca"}
    state = {"positions": {}, "seen": []}
    out = agent.handle_event(make_event(), cfg, state, alpaca, object(),
                             lambda s: None)
    assert "entry" in str(out) or state["positions"]
    (row,) = state["positions"].values()
    assert row["qty"] == 25 and row["dry"] is True


# ── B4: a capped trade says so ──────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_a_budget_the_cap_cuts_is_said(priced, name):
    qty, note = _live_size(name, {**BUDGET, "max_contracts_per_trade": 10})
    assert qty == 10
    assert "capped at 10 by max contracts per trade" in note
    assert "→ 25" in note                         # what the budget would buy
    qty, note = _live_size(name, BUDGET)
    assert qty == 25 and "capped" not in note


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_a_contract_count_the_cap_cuts_is_said(priced, name):
    qty, note = _live_size(name, {"sizing_mode": "contracts",
                                  "contracts_per_trade": 150})
    assert qty == 100 and "(150)" in note and "capped at 100" in note
    assert _live_size(name, {"sizing_mode": "contracts",
                             "contracts_per_trade": 15}) == (15, "fixed contract count")


# ── B8: budget mode with no budget is a skip on every broker ────────────────

@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_budget_mode_without_a_budget_skips(priced, name):
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 0,
           "contracts_per_trade": 7}
    qty, note = _live_size(name, cfg)
    assert qty == 0 and "unset" in note
    # an "other trades" budget override without its amount
    eff = agent._sizing_cfg({"sizing_mode": "contracts", "contracts_per_trade": 7,
                             "other_sizing_mode": "budget"}, {"track": "other"})
    assert _live_size(name, eff)[0] == 0


# ── B5: re-running the terminal setup keeps what is saved ───────────────────

def _answers(monkeypatch, *answers):
    asked, it = [], iter(answers)

    def ask(prompt=""):
        asked.append(prompt)
        return next(it, "")
    monkeypatch.setattr("builtins.input", ask)
    return asked


@pytest.mark.parametrize("saved", [
    {"sizing_mode": "contracts", "contracts_per_trade": 20,
     "max_contracts_per_trade": 50},
    {"sizing_mode": "budget", "budget_per_trade_usd": 2500.0,
     "contracts_per_trade": 1, "max_contracts_per_trade": 100},
    {"sizing_mode": "percent", "percent_per_trade": 3.0,
     "contracts_per_trade": 1, "max_contracts_per_trade": 25},
    {"sizing_mode": "budget", "budget_per_trade_usd": 800.0,
     "contracts_per_trade": 1, "max_contracts_per_trade": 100,
     "include_other_trades": True, "other_sizing_mode": "contracts",
     "other_contracts_per_trade": 4},
])
def test_terminal_sizing_step_enter_keeps_everything(monkeypatch, saved):
    _answers(monkeypatch)                    # Enter at every question
    cfg = dict(saved)
    agent._ask_sizing(cfg)
    assert cfg == saved


def test_terminal_sizing_step_asks_for_max_contracts(monkeypatch):
    asked = _answers(monkeypatch, "2", "30", "40")
    cfg = {}
    agent._ask_sizing(cfg)
    assert cfg["contracts_per_trade"] == 30 and cfg["max_contracts_per_trade"] == 40
    assert any("Most contracts one trade may buy" in a for a in asked)
    # a count above the cap is pointed out at once
    asked = _answers(monkeypatch, "2", "30", "10")
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    agent._ask_sizing({})
    assert any("10 are bought, not 30" in line for line in out)


@pytest.mark.parametrize("start,answer,want", [
    ({}, "", 5),
    ({}, "20", 20),
    ({"max_entries_per_day": 25}, "", 25),
    ({"max_entries_per_day": 25}, "0", 25),       # 0 would skip every entry
    ({"max_entries_per_day": 25}, "1001", 25),
    ({"max_entries_per_day": 25}, "abc", 25),
])
def test_terminal_daily_cap_keeps_saved_and_refuses_zero(monkeypatch, start,
                                                          answer, want):
    _answers(monkeypatch, answer)
    cfg = dict(start)
    agent._ask_daily_cap(cfg)
    assert cfg["max_entries_per_day"] == want


def test_terminal_setup_runs_the_questions():
    import inspect
    src = inspect.getsource(agent.cmd_setup)
    assert "_ask_sizing(cfg)" in src and "_ask_daily_cap(cfg)" in src
    assert 'or "1"' not in inspect.getsource(agent._ask_sizing)
    assert "else 5" not in src


@pytest.mark.parametrize("saved,want", [(True, True), (False, False), (None, False)])
def test_terminal_source_setup_keeps_other_trades(monkeypatch, saved, want):
    from sources import agenthc_day_trade_ideas as hc

    class Stop(Exception):
        pass

    def stop():
        raise Stop
    monkeypatch.setattr(hc, "day_pass_price", stop)
    _answers(monkeypatch)
    cfg = {} if saved is None else {"include_other_trades": saved}
    with pytest.raises(Stop):
        hc.setup(cfg)
    assert cfg["include_other_trades"] is want


# ── B6: positions count toward the limit in their own mode ──────────────────

LIVE = {"dry_run": False, "max_entries_per_day": 50, "max_open_positions": 3,
        "sizing_mode": "contracts", "contracts_per_trade": 1}
PRACTICE_ROW = {"option_id": "dry", "qty": 1, "dry": True}
REAL_ROW = {"option_id": "opt", "qty": 1, "live": True}


def _rows(row, n):
    return {f"T{i}|2026-07-10|{i}.0|C": dict(row) for i in range(n)}


def test_practice_positions_never_block_real_entries(home, no_policy):
    broker = StubBroker()
    state = {"positions": _rows(PRACTICE_ROW, 3)}
    out = agent.handle_event(make_event(), LIVE, state, broker, object(),
                             lambda s: None)
    assert len(broker.calls) == 1, out


@pytest.mark.parametrize("row", [REAL_ROW, {"qty": 1}])   # unknown = real
def test_real_positions_still_block_real_entries(home, no_policy, row):
    broker = StubBroker()
    state = {"positions": _rows(row, 3)}
    out = agent.handle_event(make_event(), LIVE, state, broker, object(),
                             lambda s: None)
    assert broker.calls == [] and "max open positions reached (3)" in out
    assert "3 real positions open now" in out


def test_practice_counts_practice_positions(home, no_policy):
    cfg = {**LIVE, "dry_run": True}
    state = {"positions": {**_rows(PRACTICE_ROW, 3)}}
    out = agent.handle_event(make_event(), cfg, state, None, None, lambda s: None)
    assert "max open positions reached (3)" in out
    assert "3 practice positions open now" in out
    # real positions left from LIVE don't use up practice's limit
    state = {"positions": _rows(REAL_ROW, 3)}
    agent.handle_event(make_event(), cfg, state, None, None, lambda s: None)
    assert len(state["positions"]) == 4


def test_a_practice_row_in_the_contract_is_said_not_silent(home, no_policy):
    broker = StubBroker()
    key = "SPY|2026-07-10|752.0|C"
    state = {"positions": {key: dict(PRACTICE_ROW, qty=5)}}
    out = agent.handle_event(make_event(), LIVE, state, broker, object(),
                             lambda s: None)
    assert broker.calls == [] and "practice" in out
    rows = _log(home)
    assert rows and rows[-1]["action"] == "broker_no_order"
    assert "practice (dry-run) position" in rows[-1]["reason"]
    assert state["positions"][key]["qty"] == 5       # nothing replaced


def test_dashboard_limit_counts_this_modes_positions():
    state = {"positions": {**_rows(PRACTICE_ROW, 3)}}
    lim = view_model._limits({**LIVE}, state, [], "live", None,
                             view_model.datetime.now(view_model.timezone.utc))
    assert lim["positions_reached"] is False
    lim = view_model._limits({**LIVE, "dry_run": True}, state, [], "practice",
                             None, view_model.datetime.now(view_model.timezone.utc))
    assert lim["positions_reached"] is True


# ── review of #90: quantity math and what Enter keeps ───────────────────────

@pytest.mark.parametrize("name", sorted(ADAPTERS))
@pytest.mark.parametrize("budget,price,want", [
    (700, 0.07, 100), (1400, 0.14, 100), (5600, 0.56, 100),  # float: 99 before
    (7, 0.07, 1), (6.99, 0.07, 0), (5000, 2.0, 25), (5000, 2.01, 24)])
def test_budget_buys_every_whole_contract_it_pays_for(monkeypatch, name, budget,
                                                      price, want):
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: price)
    monkeypatch.setattr(robinhood, "resolve_instrument", lambda rh, ev: "opt-1")
    monkeypatch.setattr(robinhood, "quote_price", lambda rh, oid, side="buy": price)
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": budget,
           "max_contracts_per_trade": 1000}
    if name == "moomoo":
        qty = moomoo._size(cfg, price)[0]
    else:
        qty = _live_size(name, cfg)[0]
    assert qty == want


def test_terminal_daily_cap_saved_zero_is_not_kept_on_enter(monkeypatch):
    _answers(monkeypatch)                    # Enter
    cfg = {"max_entries_per_day": 0}         # what the old setup took
    agent._ask_daily_cap(cfg)
    assert cfg["max_entries_per_day"] == 5


@pytest.mark.parametrize("saved", [
    {"sizing_mode": "budget", "budget_per_trade_usd": 12345.67,
     "contracts_per_trade": 1, "max_contracts_per_trade": 100},
    {"sizing_mode": "budget", "budget_per_trade_usd": 1234567.0,
     "contracts_per_trade": 1, "max_contracts_per_trade": 100},
    {"sizing_mode": "percent", "percent_per_trade": 0.125,
     "contracts_per_trade": 1, "max_contracts_per_trade": 100},
    {"sizing_mode": "budget", "budget_per_trade_usd": 800.0,
     "contracts_per_trade": 1, "max_contracts_per_trade": 100,
     "include_other_trades": True, "other_sizing_mode": "budget",
     "other_budget_per_trade_usd": 12345.67},
])
def test_terminal_sizing_enter_keeps_every_digit(monkeypatch, saved):
    _answers(monkeypatch)
    cfg = dict(saved)
    agent._ask_sizing(cfg)
    assert cfg == saved
