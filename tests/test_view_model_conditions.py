"""One fact, one place: every owner-facing condition in the view-model has
one stable code (Status.conditions), and the agent's own messages
("From your agent") are tied to the condition they are about by that code,
so the dashboard can say each condition once — never by comparing text."""

import json
import os
import re
from datetime import timedelta

import pytest

import agent
import guardian_states as gs
import view_model as vm

NOW = gs.NOW
ACCT = "robinhood:0123456789"
LIVE = {"source": "agenthc", "broker": "robinhood", "dry_run": False,
        "agenthc_api_key": ""}
SPY = "SPY|2026-11-13|700.0|C"
LAPSED = ("Your AgentHC feed pass has run out — the agent gets no signals until "
          "you buy more signal days (dashboard → Settings → Signal source).")


def runtime(**over):
    t = NOW.timestamp()
    rt = {"running": True, "started_at": t - 7200, "heartbeat_at": t - 12,
          "wait_s": 30.0, "poll_at": t - 12, "poll_ok": True,
          "feed_access": True, "signal_at": None,
          "broker": {"id": "robinhood", "account_tag": ACCT, "connected": True}}
    rt.update(over)
    return rt


def real_pos(**over):
    row = {"option_id": "abc", "qty": 1, "live": True, "entry_mark": 4.12,
           "opened_at": NOW.timestamp() - 90000, "acct": ACCT}
    row.update(over)
    return row


def build(cfg=LIVE, st=None, rt=None):
    out = vm.build_status(cfg, st if st is not None else {"positions": {}}, [],
                          runtime() if rt is None else rt, now=NOW)
    assert vm.validate(out) == [], vm.validate(out)
    json.loads(json.dumps(out, allow_nan=False))
    return out


def codes(s):
    return [c["code"] for c in s["conditions"]]


def tid(token):
    from sources import agenthc_day_trade_ideas as hc
    return hc._token_id(token)


def well_formed(s):
    """Each condition once; each agent message tied to at most one."""
    cs = codes(s)
    assert len(cs) == len(set(cs)), cs
    refs = [i for c in s["conditions"] for i in c["notices"]]
    assert len(refs) == len(set(refs)), refs
    assert all(0 <= i < len(s["notices"]) for i in refs)
    for c in s["conditions"]:
        for i in c["notices"]:
            rank = vm._LEVEL_RANK
            assert rank[c["level"]] >= rank[s["notices"][i]["level"]]


@pytest.mark.parametrize("name", sorted(gs.STATES))
def test_every_fixture_state_states_each_condition_once(name):
    s = gs.status(name)
    assert vm.validate(s) == [], vm.validate(s)
    well_formed(s)


def test_fixture_states_carry_the_conditions_their_screens_show():
    expect = {"stopped": "agent_stopped", "not-answering": "agent_not_answering",
              "state-hold": "state_hold", "handoff-hold": "handoff_hold",
              "restarting": "restarting", "paused-new-trades": "paused",
              "broker-disconnected": "broker_down", "low-cash": "low_cash",
              "pass-lapsed": "signal_pass", "pass-exits-only": "signal_pass",
              "pass-unchecked": "signal_pass", "day-pass-refused": "signal_pass",
              "feed-down": "feed_unreachable"}
    for name, code in expect.items():
        assert code in codes(gs.status(name)), (name, codes(gs.status(name)))
    assert codes(gs.status("live")) == []


def test_the_operators_example_is_one_condition_not_two():
    """The agent's "feed pass has run out" and the dashboard's "No signal
    pass" are the same condition: one code, the agent's message tied to it."""
    st = {"positions": {SPY: real_pos()},
          "pass_status": {tid("tok"): {"status": "used_up"}},
          "pass_notice": LAPSED}
    s = build({**LIVE, "feed_pass": {"token": "tok"}}, st,
              runtime(feed_access=False))
    i = next(k for k, n in enumerate(s["notices"]) if n["text"] == LAPSED)
    passes = [c for c in s["conditions"] if c["code"] == "signal_pass"]
    assert passes == [{"code": "signal_pass", "level": "warn", "notices": [i]}]
    well_formed(s)
    # handed over by the running agent as well (code "pass"): still one
    s = build({**LIVE, "feed_pass": {"token": "tok"}}, st, runtime(
        feed_access=False,
        notices=[{"level": "warn", "code": "pass", "text": LAPSED}]))
    assert [n["text"] for n in s["notices"]].count(LAPSED) == 1
    assert [c["notices"] for c in s["conditions"] if c["code"] == "signal_pass"] == [[0]]


def test_agent_messages_name_their_condition_or_none():
    rt = runtime(notices=[
        {"level": "warn", "code": "low_cash", "text": "Only $40 cash."},
        {"level": "urgent", "code": "records", "condition": "state_hold",
         "text": "on hold"},
        {"level": "info", "code": "expiry", "condition": "position_expiring",
         "text": "SPY expires today"},
        {"level": "warn", "code": "second_copy", "text": "second copy?"},
        {"level": "info", "code": "something_new", "text": "a new message"},
        {"level": "info", "code": "x", "condition": "not_a_condition",
         "text": "unknown condition"}],
        hold="state_hold")
    s = build(rt=rt)
    by = {c["code"]: c for c in s["conditions"]}
    assert by["low_cash"]["notices"] == [0]
    assert by["state_hold"]["notices"] == [1] and by["state_hold"]["level"] == "urgent"
    assert by["position_expiring"]["notices"] == [2]
    assert by["second_copy"]["notices"] == [3]
    # messages about no known condition: shown as they are, tied to nothing
    tied = {i for c in s["conditions"] for i in c["notices"]}
    assert 4 not in tied and 5 not in tied
    # the notices themselves are what they always were
    assert s["notices"][1] == {"level": "urgent", "code": "records", "text": "on hold"}
    well_formed(s)


def test_conditions_from_the_facts_alone():
    s = build(rt=runtime(broker={"id": "robinhood", "account_tag": ACCT,
                                 "connected": False, "last_error": "down"}),
              st={"positions": {SPY: real_pos()}})
    assert {"broker_down", "positions_unmanaged"} <= set(codes(s))
    assert all(c["notices"] == [] for c in s["conditions"])
    s = build({**LIVE, "paused": True})
    assert codes(s) == ["paused"]
    s = vm.build_status({**LIVE, "agenthc_api_key": "k"}, {"positions": {}}, [],
                        None, now=NOW)
    assert codes(s) == ["agent_stopped"]
    # a fact stays a fact while the agent is stopped: no pass is no pass
    s = vm.build_status(LIVE, {"positions": {}}, [], None, now=NOW)
    assert codes(s) == ["agent_stopped", "signal_pass"]
    s = build(rt=runtime(poll_ok=False))
    assert "feed_unreachable" in codes(s)
    # most urgent first
    s = build({**LIVE, "paused": True},
              rt=runtime(broker={"id": "robinhood", "account_tag": ACCT,
                                 "connected": False}))
    assert codes(s)[0] == "broker_down"


def test_the_agent_tags_its_own_banners(home, monkeypatch):
    """agent.py's banners name their condition only when asked, so the
    classic dashboard's lines are what they were."""
    monkeypatch.setitem(agent._BAK_FAIL, "stale", True)
    tagged = agent._durability_banner(conditions=True)
    assert [w["condition"] for w in tagged] == ["backup_failing"]
    assert agent._durability_banner() == [
        {"level": w["level"], "text": w["text"]} for w in tagged]
    et = NOW.astimezone(agent.MARKET_TZ)
    day = et.date().isoformat()
    yday = (et.date() - timedelta(days=3)).isoformat()
    st = {"positions": {f"SPY|{day}|700.0|C": {"option_id": "a", "qty": 1},
                        f"QQQ|{yday}|500.0|C": {"option_id": "b", "qty": 1}}}
    for paused in (False, True):
        lines = agent.expiry_banner({}, st, now_et=et, paused=paused,
                                    conditions=True)
        assert lines and all(w["condition"] in vm.CONDITIONS for w in lines)
        assert agent.expiry_banner({}, st, now_et=et, paused=paused) == [
            {"level": w["level"], "text": w["text"]} for w in lines]
    paused_lines = agent.expiry_banner({}, st, now_et=et, paused=True,
                                       conditions=True)
    assert {w["condition"] for w in paused_lines} == {
        "position_expired", "position_expiring_unsold"}


def test_the_typescript_lists_the_condition_codes():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *vm.TS_PATH.split("/")), encoding="utf-8") as f:
        src = f.read()
    listed = re.search(r"export const CONDITIONS = \[(.*?)\] as const", src, re.S)
    assert re.findall(r'"(\w+)"', listed.group(1)) == list(vm.CONDITIONS)
    assert set(vm._CONDITION_LEVEL) <= set(vm.CONDITIONS)
    assert set(vm._NOTICE_CODE_CONDITION.values()) <= set(vm.CONDITIONS)


def test_conditions_survive_hostile_notices():
    s = build(rt=runtime(notices=[None, "x", {"text": ""},
                                  {"text": "t", "level": "boom", "condition": 5},
                                  {"text": "u", "code": ["list"]}]))
    well_formed(s)
    assert s["conditions"] == [] or all(c["notices"] == [] for c in s["conditions"])

