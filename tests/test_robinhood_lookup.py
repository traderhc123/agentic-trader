"""Empty Robinhood contract lookups: retried once, and the skip says what
Robinhood actually answered (2026-10-21: NFLX 10/23 $65 PUT reported as
"no tradable contract" while the contract existed and the login was fine)."""
import json

import pytest

import broker_events
from brokers import robinhood


def _text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


_ROW = {"data": {"results": [{"id": "opt-1", "chain_symbol": "NFLX",
                              "strike_price": "65.0000",
                              "expiration_date": "2026-10-23"}]}}


class _SeqRH:
    """Returns the given tool results in order for get_option_instruments."""
    last_error = None

    def __init__(self, *results):
        self.results = list(results)
        self.lookups = 0

    def call_tool(self, name, args):
        if name == "get_option_instruments":
            self.lookups += 1
            return self.results.pop(0) if self.results else _text({"data": {"results": []}})
        return {"content": []}


def _entry():
    return {"event": "ENTERED", "ticker": "NFLX", "expiry": "2026-10-23",
            "strike": 65.0, "type": "P", "event_id": "e1"}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(robinhood, "_RESOLVE_RETRY_WAIT_S", 0)
    broker_events.begin()
    yield
    broker_events.begin()


def _skip(rh):
    state = {"positions": {}}
    assert robinhood.execute(rh, {}, _entry(), state) is False
    return broker_events.drain()


def test_retry_recovers_after_a_transient_error(monkeypatch):
    rh = _SeqRH({"content": [{"type": "text", "text": "Too many requests"}], "isError": True},
                _text(_ROW))
    monkeypatch.setattr(robinhood, "size_contracts", lambda rh, cfg, oid: (0, "stop here"))
    state = {"positions": {}}
    robinhood.execute(rh, {}, _entry(), state)
    assert rh.lookups == 2                                   # retried once
    kinds = [r["kind"] for r in broker_events.drain()]
    assert "skip_no_contract" not in kinds and "broker_lookup_failed" not in kinds


def test_error_answer_is_reported_as_error_with_robinhood_text(capsys):
    err = {"content": [{"type": "text", "text": "429 Too many requests, slow down"}],
           "isError": True}
    rh = _SeqRH(err, err)
    reported = _skip(rh)
    assert rh.lookups == 2
    assert [r["kind"] for r in reported] == ["broker_lookup_failed"]
    assert "Too many requests" in reported[0]["message"]
    assert "rate-limiting" in broker_events.hint(reported[0]["message"])
    out = capsys.readouterr().out
    assert "retrying" in out and "Robinhood said: 429 Too many requests" in out


def test_zero_rows_is_still_no_contract():
    rh = _SeqRH(_text({"data": {"results": []}}), _text({"data": {"results": []}}))
    reported = _skip(rh)
    assert [r["kind"] for r in reported] == ["skip_no_contract"]
    assert "0 matching contracts" in reported[0]["message"]


def test_unparseable_answer_is_lookup_failed():
    bad = {"content": [{"type": "text", "text": "<html>gateway timeout</html>"}]}
    reported = _skip(_SeqRH(bad, bad))
    assert [r["kind"] for r in reported] == ["broker_lookup_failed"]
    assert "without contract data" in reported[0]["message"]


def test_rows_without_recognizable_id_ask_for_report():
    odd = _text({"data": {"results": [{"strike_price": "65.0000", "uuid": "x"}]}})
    reported = _skip(_SeqRH(odd, odd))
    assert [r["kind"] for r in reported] == ["broker_lookup_failed"]
    assert "agent-run.log" in reported[0]["message"]


def test_expiration_date_only_row_resolves():
    rh = _SeqRH(_text({"results": [{"id": "opt-9", "expiration_date": "2026-10-23"}]}))
    assert robinhood.resolve_instrument(rh, _entry()) == "opt-9"


def test_cash_hint_still_first():
    # is_cash_problem() has its own pattern, independent of the hint order
    assert broker_events.is_cash_problem("insufficient buying power")
    assert not broker_events.is_cash_problem("429 too many requests")


def test_new_kind_notifies():
    assert "broker_lookup_failed" in broker_events.KINDS
    assert "broker_lookup_failed" in broker_events.NOTIFY
