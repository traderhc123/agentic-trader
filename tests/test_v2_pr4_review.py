"""PR 4 review fixes (the 11-11 review of feat/v2-parity-and-switch): each
test here failed on the code the review read and passes on the fix.

Hermetic: no broker, wallet, AI, AgentHC or network."""

import json

import pytest

import agent
import webui

from test_v2_settings_api import (  # noqa: F401
    BASE, app, read_cfg, write_cfg)
from test_v2_parity import _running
from test_v2_parity_settings import (  # noqa: F401
    OWN, OWN_KEY, _old_wallet_files, lnbits)

WALLET = {**BASE, "lnbits_url": OWN, "lnbits_admin_key": OWN_KEY}


# ── the agent's wallet: made or replaced in /v2 as the classic route does ──

@pytest.mark.parametrize("start, payload", [
    ({**BASE, "source": "manual"}, {}),
    (WALLET, {"replace": True})])
def test_making_a_wallet_needs_the_agreement_as_the_classic_route_does(
        home, app, lnbits, monkeypatch, start, payload):
    write_cfg(home, start)
    c = app()
    # a token got while the agreement was on file...
    first = c.post("/api/v2/wallet/create", payload)
    assert first["needs_confirm"] is True
    monkeypatch.setattr(agent, "consent_ok", lambda: False)
    # ...the classic route refuses now, and so does /v2: no preview, no
    # confirmed make, no LNbits call, nothing saved, no old-wallet file
    refused = {"ok": False, "error": "accept the agreement first"}
    assert c.post("/api/wallet/create", payload) == refused
    assert c.post("/api/v2/wallet/create", payload) == refused
    assert c.post("/api/v2/wallet/create",
                  {**payload, "confirm": first["preview"]["token"]}) == refused
    assert lnbits["made"] == [] and read_cfg(home) == start
    assert _old_wallet_files(home) == []


@pytest.mark.parametrize("other", [
    {**WALLET, "lnbits_url": "https://lnbits.other.example",
     "lnbits_admin_key": "fakeOTHERkey13579"},           # another tab's wallet
    {**WALLET, "source": "manual"}])                     # another tab's source
def test_a_wallet_made_while_the_settings_changed_is_left_unused(
        home, app, lnbits, monkeypatch, other):
    """The LNbits call runs outside the settings lock: a save that lands
    meanwhile (another tab) is never overwritten by the reviewed replace."""
    import lightning_wallet
    write_cfg(home, WALLET)
    c = app()
    made = lightning_wallet.create_wallet

    def slow_create(*a, **k):
        out = made(*a, **k)
        write_cfg(home, other)  # saved while LNbits was answering
        return out
    monkeypatch.setattr(lightning_wallet, "create_wallet", slow_create)
    r = c.confirmed("/api/v2/wallet/create", {"replace": True})
    assert r == {"ok": False, "error": webui._REVIEW_AGAIN}
    assert read_cfg(home) == other and _old_wallet_files(home) == []
    assert len(lnbits["made"]) == 1  # made, empty, and never used


@pytest.mark.parametrize("start, payload", [
    (WALLET, {"replace": 1}), (WALLET, {"replace": 1.0}), (WALLET, {"replace": "true"}),
    ({**BASE, "source": "manual"}, {"replace": 0}),
    ({**BASE, "source": "manual"}, {"replace": 0.0}),
    ({**BASE, "source": "manual"}, {"replace": None})])
def test_the_replace_flag_is_true_or_false_only(home, app, lnbits, start, payload):
    write_cfg(home, start)
    c = app()
    r = c.post("/api/v2/wallet/create", payload)
    assert r["ok"] is False and "needs_confirm" not in r, payload
    assert lnbits["made"] == [] and read_cfg(home) == start


# ── signal source: a subscriber goes back to AgentHC signals ──

@pytest.mark.parametrize("was", ["manual", "url"])
def test_a_subscriber_with_only_a_key_can_switch_back_to_agenthc(home, app, was):
    """No pass and no wallet, a subscription key on file: the preview is
    offered, so the confirmed switch must work (it used to be refused with
    the classic "buy a pass above", the token used up)."""
    from test_v2_settings_api import same_effect
    key = "ahc_fake_saved_key_24681357"
    start = {**BASE, "source": was, "agenthc_api_key": key,
             "include_other_trades": True}
    if was == "url":
        start["source_url"] = "https://feed.example/signals.json"
    write_cfg(home, start)
    c = app()
    # the classic source form, with the key the owner has on file
    out = same_effect(home, start,
                      lambda: c.post("/api/source", {"source": "agenthc", "scope": "all",
                                                     "agenthc_key": key}),
                      lambda: c.confirmed("/api/v2/settings/source", {"source": "agenthc"}))
    assert out == {**start, "source": "agenthc"}
    write_cfg(home, start)
    r = c.confirmed("/api/v2/settings/source", {"source": "agenthc"})
    assert r["ok"] is True and r["saved"] is True and key not in json.dumps(r)


# ── code changes: Apply is bound to all of the change, against the files now ──

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway source tree for self_edit (never the real one)."""
    import self_edit
    r = tmp_path / "repo"
    r.mkdir()
    (r / "helper_mod.py").write_text("A = 1\n", encoding="utf-8")
    monkeypatch.setattr(self_edit, "_repo", lambda: str(r))
    monkeypatch.setattr(self_edit, "_import_problem", lambda paths: "")
    return r


def _udiff(rel, old, new):
    import difflib  # as self_edit.propose makes it
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def _stage(changes):
    """A proposal as self_edit.propose writes it: [(path, old, new)]."""
    import self_edit
    with open(self_edit._proposal_path(), "w", encoding="utf-8") as f:
        json.dump({"request": "r", "explanation": "x", "warnings": [],
                   "changes": [{"path": p, "content": new, "diff": _udiff(p, old, new)}
                               for p, old, new in changes]}, f)


def _sha_text(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_change_too_large_to_show_whole_is_never_applied_from_v2(home, app, repo):
    """The review shows the diff cut at 200,000 characters: what is not
    shown can't have been reviewed, so /v2 says it is cut and refuses it
    (it used to apply all of it, bound to the cut text only)."""
    import self_edit
    big = "".join(f"LINE_{i:06d} = {i}\n" for i in range(6000))  # ~110k each
    _stage([("big_a.py", "", big), ("big_b.py", "", big)])
    c = app()
    shown = c.get("/api/proposal")
    assert shown["pending"] is True and shown["truncated"] is True
    full = "\n".join(ch["diff"] for ch in self_edit.current()["changes"])
    assert len(shown["diff"]) < len(full)
    for sha in (_sha_text(shown["diff"]), _sha_text(full)):
        r = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
        assert r["ok"] is False and "needs_confirm" not in r and "too large" in r["error"]
        ok, msg = self_edit.apply_and_restart(restart=False, expect_sha256=sha)
        assert ok is False
    assert sorted(p.name for p in repo.iterdir()) == ["helper_mod.py"]
    assert self_edit.current() is not None  # still waiting: Reject it
    # a change that fits is shown whole and is not marked cut
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    assert c.get("/api/proposal")["truncated"] is False


def test_a_change_made_against_older_code_is_never_applied_from_v2(home, app, repo):
    """An update (or a hand edit) changed the file after the AI helper
    wrote its change: applying it would write the old whole file back over
    the update. /v2 refuses at the preview and at the confirm."""
    import self_edit
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    c = app()
    sha = _sha_text(c.get("/api/proposal")["diff"])
    first = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
    assert first["needs_confirm"] is True
    updated = "A = 1\nB = 3  # from an update\n"
    (repo / "helper_mod.py").write_text(updated, encoding="utf-8")
    r = c.post("/api/v2/proposal/apply", {"diff_sha256": sha,
                                          "confirm": first["preview"]["token"]})
    assert r["ok"] is False and "changed since" in r["error"]
    r = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})
    assert r["ok"] is False and "needs_confirm" not in r and "changed since" in r["error"]
    ok, msg = self_edit.apply_and_restart(restart=False, expect_sha256=sha)
    assert ok is False and "changed since" in msg
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == updated
    # the same change against the file as it is: applied
    (repo / "helper_mod.py").write_text("A = 1\n", encoding="utf-8")
    done = c.confirmed("/api/v2/proposal/apply", {"diff_sha256": sha})
    assert done["ok"] is True, done
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 2\n"
