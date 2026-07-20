"""Expired-position ledger pruning (agent._prune_expired_positions)."""

from agent import _prune_expired_positions


def _state(*keys):
    return {"positions": {k: {"option_id": "x", "qty": 1} for k in keys}}


def test_prunes_past_expiry_and_saves():
    state = _state("SPY|2020-07-17|620.0|C", "SPY|2099-12-19|620.0|C")
    saved = []
    dropped = _prune_expired_positions({}, state, lambda s: saved.append(True))
    assert dropped == ["SPY|2020-07-17|620.0|C"]
    assert list(state["positions"]) == ["SPY|2099-12-19|620.0|C"]
    assert saved  # state persisted exactly because something was dropped


def test_noop_when_nothing_expired_saves_nothing():
    state = _state("SPY|2099-12-19|620.0|C")
    saved = []
    assert _prune_expired_positions({}, state, lambda s: saved.append(True)) == []
    assert not saved


def test_malformed_keys_are_left_alone():
    state = _state("not-a-position-key")
    assert _prune_expired_positions({}, state, lambda s: None) == []
    assert "not-a-position-key" in state["positions"]


def test_empty_state_is_safe():
    assert _prune_expired_positions({}, {}, lambda s: None) == []
