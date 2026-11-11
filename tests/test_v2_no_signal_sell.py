"""The no-signal sell's time on /v2: one control, in Rules & limits next to
the expiry-day sell time, on the same two-step limits route, saved by the
chat's own check into the chat's own key ("no-signal sell on|off|HH:MM").
Hermetic: no broker, wallet, AI, AgentHC or network."""

import pytest

import agent

from test_v2_settings_api import (  # noqa: F401
    BASE, SECRETS, app, read_cfg, same_effect, write_cfg)
from test_v2_parity import _running
import test_v2_parity_settings as T


@pytest.mark.parametrize("start, chat, v2", [
    ("15:30", "no-signal sell off", "off"),
    ("15:30", "no-signal sell 14:40", "14:40"),
    ("15:30", "no-signal sell 9:45", "09:45"),
    ("off", "no-signal sell on", "15:30"),   # "on" is the default time
    ("off", "no-signal sell 15:59", "15:59"),
])
def test_the_no_signal_sell_time_is_the_chat_command(home, app, start, chat, v2):
    c = _running(app)
    out = same_effect(home, {**BASE, "signal_loss_sell_et": start},
                      lambda: c.post("/api/command", {"text": chat}),
                      lambda: c.confirmed("/api/v2/settings/limits",
                                          {"no_signal_sell_et": v2}))
    assert out["signal_loss_sell_et"] == v2
    got = c.get("/api/v2/settings")["no_signal"]
    assert got["sell_et"] == (None if v2 == "off" else v2)
    assert got["default"] == "15:30" and got["allowed"] == ["09:30", "15:59"]


def test_unset_reads_as_the_default(home, app):
    write_cfg(home, BASE)
    c = _running(app)
    got = c.get("/api/v2/settings")["no_signal"]
    assert got["sell_et"] == "15:30"


def test_off_or_later_raises_the_risk_and_the_preview_says_so(home, app):
    write_cfg(home, {**BASE, "signal_loss_sell_et": "15:30"})
    c = _running(app)
    for new, now, direction in (("off", None, "raises_risk"),
                                ("15:50", "15:50", "raises_risk"),
                                ("14:00", "14:00", "lowers_risk")):
        p = c.post("/api/v2/settings/limits", {"no_signal_sell_et": new})
        assert p["needs_confirm"] is True
        assert p["preview"]["changes"] == [{
            "key": "no_signal_sell_et", "label": "No-signal sell time",
            "saved_now": "15:30", "new": now, "direction": direction}]
        assert "It does not switch between Practice and LIVE." in p["preview"]["does_not"]
    assert read_cfg(home) == {**BASE, "signal_loss_sell_et": "15:30"}
    # off -> a time lowers the risk; the same time is nothing to change
    write_cfg(home, {**BASE, "signal_loss_sell_et": "off"})
    p = c.post("/api/v2/settings/limits", {"no_signal_sell_et": "15:30"})
    assert p["preview"]["changes"][0]["direction"] == "lowers_risk"
    write_cfg(home, BASE)
    r = c.post("/api/v2/settings/limits", {"no_signal_sell_et": "15:30"})
    assert r == {"ok": True, "saved": False,
                 "note": "Nothing to change: those are your settings already."}


def test_only_the_chats_range(home, app):
    write_cfg(home, {**BASE, "signal_loss_sell_et": "15:30"})
    c = _running(app)
    for bad in ("16:00", "09:00", "9:29", "25:00", "on", "", "3pm", "OFF",
                5, None, True, ["15:40"]):
        r = c.post("/api/v2/settings/limits", {"no_signal_sell_et": bad})
        assert r["ok"] is False and "09:30 to 15:59" in r["error"], bad
        assert "needs_confirm" not in r
    assert read_cfg(home) == {**BASE, "signal_loss_sell_et": "15:30"}


def _case_no_signal_sell(c, home, monkeypatch):
    start = {**BASE, "signal_loss_sell_et": "15:30"}
    return {"path": "/api/v2/settings/limits", "payload": {"no_signal_sell_et": "off"},
            "done": lambda: read_cfg(home).get("signal_loss_sell_et") == "off",
            "reset": lambda: None, "start": start}


def test_it_refuses_a_wrong_confirmation(home, app, monkeypatch):
    monkeypatch.setitem(T.TWO_STEP, "no-signal-sell", _case_no_signal_sell)
    T._check_two_step("no-signal-sell", home, app, monkeypatch)
    # a token for one value never confirms another
    write_cfg(home, {**BASE, "signal_loss_sell_et": "15:30"})
    c = _running(app)
    token = c.post("/api/v2/settings/limits",
                   {"no_signal_sell_et": "15:00"})["preview"]["token"]
    r = c.post("/api/v2/settings/limits", {"no_signal_sell_et": "off",
                                           "confirm": token})
    assert r["ok"] is False
    assert read_cfg(home)["signal_loss_sell_et"] == "15:30"


def test_a_saved_time_is_what_the_agent_sells_at(home, app):
    write_cfg(home, {**BASE, "dry_run": False})
    c = _running(app)
    r = c.confirmed("/api/v2/settings/limits", {"no_signal_sell_et": "14:10"})
    assert r["ok"] is True and r["saved"] is True
    cfg = read_cfg(home)
    assert cfg == {**BASE, "dry_run": False, "signal_loss_sell_et": "14:10"}
    assert agent._signal_loss_time(cfg, "2026-11-06") == 14 * 60 + 10
    assert r["settings"]["no_signal"]["sell_et"] == "14:10"
