"""The wallet's auto-pay cap follows AgentHC's day price ("auto", the
default): the server's 1-day sats + 10%, rounded up to 1,000, at most
150,000; its USD day price at the live BTC price when it sent no sats;
30,000 when there is no price. A number the owner saved is used exactly;
a saved 30000 (the old default) counts as auto."""

import pytest

from sources import agenthc_day_trade_ideas as hc


def _server(monkeypatch, usd=None, sats=None):
    monkeypatch.setattr(hc, "_price_cache", {"at": float("inf"), "usd": usd, "sats": sats})


def test_auto_cap_from_the_servers_sats(monkeypatch):
    _server(monkeypatch, usd={1: 50.0, 5: 225.0}, sats={1: 55_600, 5: 250_000})
    # 55,600 × 1.10 = 61,160 → 62,000
    assert hc.auto_autopay_cap() == {"sats": 62_000, "basis": "server_sats"}
    caps = hc.autopay_caps({})
    assert caps["per_invoice"] == caps["per_day"] == 62_000
    assert caps["per_invoice_auto"] and caps["per_day_auto"]
    assert hc.autopay_cap_words({}) == "Auto: follows AgentHC's day price (now ≈ 62,000 sats)"


def test_auto_cap_from_the_servers_usd_at_the_live_btc_price(monkeypatch):
    _server(monkeypatch, usd={1: 50.0, 5: 225.0})             # no sats sent
    monkeypatch.setattr(hc, "_live_btc_usd", lambda: 100_000.0)
    # $50 at $100k = 50,000 sats × 1.10 = 55,000
    assert hc.auto_autopay_cap() == {"sats": 55_000, "basis": "server_usd"}
    assert hc.autopay_caps({"max_autopay_sats": "auto"})["per_invoice"] == 55_000


@pytest.mark.parametrize("why", ["no server price", "no btc price"])
def test_auto_cap_without_a_price_is_the_old_30000(monkeypatch, why):
    if why == "no server price":
        _server(monkeypatch)                                   # unreachable / older server
        monkeypatch.setattr(hc, "_live_btc_usd", lambda: 100_000.0)
    else:
        _server(monkeypatch, usd={1: 50.0})
        monkeypatch.setattr(hc, "_live_btc_usd", lambda: 0.0)
    assert hc.auto_autopay_cap() == {"sats": 30_000, "basis": "fallback"}
    assert hc.autopay_caps({})["per_invoice"] == 30_000
    assert "isn't known yet" in hc.autopay_cap_words({})


def test_auto_cap_never_goes_above_150000(monkeypatch):
    _server(monkeypatch, usd={1: 500.0}, sats={1: 900_000})
    assert hc.auto_autopay_cap()["sats"] == 150_000
    assert hc.autopay_caps({})["per_invoice"] == 150_000


@pytest.mark.parametrize("saved", [30_000, "30000", 30_000.0, None, "auto", "AUTO ", "lots"])
def test_old_default_and_missing_values_count_as_auto(monkeypatch, saved):
    _server(monkeypatch, usd={1: 50.0}, sats={1: 55_600})
    cfg = {"max_autopay_sats": saved, "max_autopay_sats_per_day": saved}
    caps = hc.autopay_caps(cfg)
    assert caps["per_invoice"] == caps["per_day"] == 62_000
    assert caps["per_invoice_auto"] and caps["per_day_auto"]
    assert hc._one_pass_a_day(cfg)          # "auto" never allows a second pass a day


@pytest.mark.parametrize("saved", [45_000, 400_000, "45,000", 0])
def test_an_owners_number_is_kept_exactly(monkeypatch, saved):
    _server(monkeypatch, usd={1: 50.0}, sats={1: 55_600})
    want = int(str(saved).replace(",", ""))
    caps = hc.autopay_caps({"max_autopay_sats": saved})
    assert caps["per_invoice"] == caps["per_day"] == want      # no ceiling, no auto
    assert not caps["per_invoice_auto"]
    assert hc.autopay_cap_words({"max_autopay_sats": saved}) == f"Fixed: {want:,} sats"


def test_an_owners_daily_cap_is_kept_and_allows_more_passes(monkeypatch):
    _server(monkeypatch, usd={1: 50.0}, sats={1: 55_600})
    cfg = {"max_autopay_sats_per_day": 200_000}
    caps = hc.autopay_caps(cfg)
    assert caps["per_invoice"] == 62_000 and caps["per_day"] == 200_000
    assert not hc._one_pass_a_day(cfg)


def test_refusal_says_the_cap_in_force_and_how_to_raise_it(monkeypatch):
    _server(monkeypatch, usd={1: 50.0}, sats={1: 55_600})
    text = hc.autopay_refusal_text(70_000, {})
    assert "costs 70,000 sats" in text
    assert "auto-pay cap for one pass (62,000 sats)" in text
    assert "Auto: follows AgentHC's day price (now ≈ 62,000 sats)" in text
    assert "'set autopay cap 77000'" in text and "max_autopay_sats to 77000" in text
    fixed = hc.autopay_refusal_text(70_000, {"max_autopay_sats": 40_000})
    assert "(40,000 sats)" in fixed and "Fixed: 40,000 sats" in fixed


def test_chat_command_sets_a_number_and_goes_back_to_auto(home, monkeypatch):
    import agent
    _server(monkeypatch, usd={1: 50.0}, sats={1: 55_600})
    agent._save(agent.CONFIG_PATH, {"source": "agenthc"}, private=True)
    ok, reply = agent._apply_command("set autopay cap 80000")
    assert ok and "Fixed: 80,000 sats" in reply
    assert agent._load(agent.CONFIG_PATH, {})["max_autopay_sats"] == 80_000
    ok, reply = agent._apply_command("autopay cap")
    assert ok and "Fixed: 80,000 sats" in reply
    ok, reply = agent._apply_command("set autopay cap 30000")    # read as auto: refused
    assert ok and reply.startswith("Nothing was changed.")
    assert agent._load(agent.CONFIG_PATH, {})["max_autopay_sats"] == 80_000
    ok, reply = agent._apply_command("set autopay cap auto")
    assert ok and "now ≈ 62,000 sats" in reply
    assert agent._load(agent.CONFIG_PATH, {})["max_autopay_sats"] == "auto"
