"""Audit PR9: the classic Apply for an AI code change (POST
/api/proposal/apply, which /dash and the built-in page both use) is held
to the same review rules as /v2: never a change made against older code
(it would write the old whole file back over an update), never a change
whose diff was too large to show whole, and never any other change than
the one whose diff_sha256 the page was shown (a request without it applies
nothing), and, as on /v2, only by a second POST that sends back the
single-use token the first one previewed. Each test here failed
on origin/main and passes on the fix.

Hermetic: no broker, wallet, AI, AgentHC or network; self_edit works on a
throwaway source tree."""

import hashlib
import re
from pathlib import Path

import pytest

import webui

from test_v2_settings_api import BASE, app, write_cfg  # noqa: F401
from test_v2_pr4_review import _stage, repo  # noqa: F401

UPDATED = "A = 1\nB = 3  # from an update\n"
BIG = "".join(f"LINE_{i:06d} = {i}\n" for i in range(6000))  # ~110k diff each


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── self_edit.apply_and_restart without expect_sha256 (the classic call) ──

def test_classic_apply_refuses_a_change_made_against_older_code(home, repo):
    import self_edit
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    (repo / "helper_mod.py").write_text(UPDATED, encoding="utf-8")  # an update
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok is False and "changed since" in msg and "nothing was applied" in msg
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == UPDATED
    assert self_edit.current() is not None  # still waiting: Reject it


def test_classic_apply_refuses_a_change_too_large_to_show_whole(home, repo):
    import self_edit
    _stage([("big_a.py", "", BIG), ("big_b.py", "", BIG)])
    assert self_edit.diff_truncated(self_edit.current())
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok is False and "too large" in msg
    assert sorted(p.name for p in repo.iterdir()) == ["helper_mod.py"]
    assert self_edit.current() is not None


def test_classic_apply_still_applies_a_current_change_shown_whole(home, repo):
    import self_edit
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n"), ("new_mod.py", "", "C = 1\n")])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok is True, msg
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 2\n"
    assert (repo / "new_mod.py").read_text(encoding="utf-8") == "C = 1\n"
    assert self_edit.current() is None  # consumed


def test_illegal_paths_are_still_discarded_before_the_review_checks(home, repo):
    """The new checks come after the path checks: a proposal for a
    FOUNDATION file is still thrown away, not left waiting as 'stale'."""
    import self_edit
    _stage([("agent.py", "", "pwned = True\n")])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok is False and "FOUNDATION" in msg
    assert self_edit.current() is None


# ── the classic route ──

def test_classic_route_refuses_a_stale_change(home, app, repo):
    import self_edit
    write_cfg(home, BASE)
    c = app()
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    (repo / "helper_mod.py").write_text(UPDATED, encoding="utf-8")
    sha = c.get("/api/proposal")["diff_sha256"]
    r = c.post("/api/proposal/apply", {"diff_sha256": sha})
    assert r["ok"] is False and "changed since" in r["reply"]
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == UPDATED
    assert self_edit.current() is not None


def test_classic_route_refuses_a_truncated_change(home, app, repo):
    write_cfg(home, BASE)
    c = app()
    _stage([("big_a.py", "", BIG), ("big_b.py", "", BIG)])
    shown = c.get("/api/proposal")
    assert shown["truncated"] is True
    r = c.post("/api/proposal/apply", {"diff_sha256": shown["diff_sha256"]})
    assert r["ok"] is False and "too large" in r["reply"]
    assert sorted(p.name for p in repo.iterdir()) == ["helper_mod.py"]


def test_classic_route_binds_apply_to_the_diff_it_showed(home, app, repo):
    import self_edit
    write_cfg(home, BASE)
    c = app()
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    shown = c.get("/api/proposal")
    assert shown["diff_sha256"] == _sha(shown["diff"]) == \
        self_edit.diff_sha256(self_edit.current())
    # another change arrived after the page read this one: left alone
    _stage([("helper_mod.py", "A = 1\n", "A = 3\n")])
    r = c.post("/api/proposal/apply", {"diff_sha256": shown["diff_sha256"]})
    assert r["ok"] is False and "not the one you reviewed" in r["reply"]
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 1\n"
    # a malformed sha is refused, never treated as "no sha"
    for bad in ("abc", shown["diff_sha256"].upper(), 5, None, ""):
        r = c.post("/api/proposal/apply", {"diff_sha256": bad})
        assert r["ok"] is False, bad
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 1\n"
    # the one shown: applied, once confirmed
    now = c.get("/api/proposal")
    r = c.confirmed("/api/proposal/apply", {"diff_sha256": now["diff_sha256"]})
    assert r["ok"] is True, r
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 3\n"



@pytest.mark.parametrize("body", [{}, {"diff_sha256": None}, {"other": 1}])
def test_classic_route_refuses_an_apply_without_the_reviewed_sha(
        home, app, repo, body):
    """A page loaded before the sha rule (or any client) that doesn't say
    which change it showed applies nothing, and is told to reload."""
    import self_edit
    write_cfg(home, BASE)
    c = app()
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    r = c.post("/api/proposal/apply", body)
    assert r["ok"] is False and "Nothing was applied" in r["reply"]
    assert "Reload the page" in r["reply"]
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 1\n"
    assert self_edit.current() is not None  # still waiting, untouched
    # the same change, applied once the page sends the sha it showed
    sha = c.get("/api/proposal")["diff_sha256"]
    assert c.confirmed("/api/proposal/apply", {"diff_sha256": sha})["ok"] is True
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 2\n"


# ── the classic route's two-step confirmation (as /v2) ──

def _one_change(home, app):
    write_cfg(home, BASE)
    c = app()
    _stage([("helper_mod.py", "A = 1\n", "A = 2\n")])
    return c, c.get("/api/proposal")["diff_sha256"]


def _untouched(repo):
    import self_edit
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 1\n"
    assert self_edit.current() is not None  # still waiting


def test_classic_apply_first_post_only_previews(home, app, repo):
    c, sha = _one_change(home, app)
    r = c.post("/api/proposal/apply", {"diff_sha256": sha})
    assert r["ok"] is True and r["needs_confirm"] is True
    assert "Nothing was applied yet" in r["reply"]
    p = r["preview"]
    assert p["diff_sha256"] == sha and p["files"] == ["helper_mod.py"]
    assert p["token"] and p["expires_in_s"] > 0
    _untouched(repo)


@pytest.mark.parametrize("token", ["", "nonsense", "1.2.3", 5, None])
def test_classic_apply_refuses_a_malformed_token(home, app, repo, token):
    c, sha = _one_change(home, app)
    c.post("/api/proposal/apply", {"diff_sha256": sha})  # a real one exists
    r = c.post("/api/proposal/apply", {"diff_sha256": sha, "confirm": token})
    assert r["ok"] is False and "Nothing was changed" in r["reply"]
    _untouched(repo)


def test_classic_apply_refuses_a_spent_or_expired_token(home, app, repo,
                                                        monkeypatch):
    c, sha = _one_change(home, app)
    tok = c.post("/api/proposal/apply", {"diff_sha256": sha})["preview"]["token"]
    # forged signature: refused, and the try spends the token
    exp, nonce, mac = tok.split(".")
    bad = f"{exp}.{nonce}.{'0' * len(mac)}"
    assert c.post("/api/proposal/apply",
                  {"diff_sha256": sha, "confirm": bad})["ok"] is False
    assert c.post("/api/proposal/apply",
                  {"diff_sha256": sha, "confirm": tok})["ok"] is False
    _untouched(repo)
    # a token past its lifetime
    tok = c.post("/api/proposal/apply", {"diff_sha256": sha})["preview"]["token"]
    now = webui._clock()
    monkeypatch.setattr(webui, "_clock", lambda: now + webui._V2_CONFIRM_S + 5)
    r = c.post("/api/proposal/apply", {"diff_sha256": sha, "confirm": tok})
    assert r["ok"] is False and "expired" in r["reply"]
    _untouched(repo)


def test_classic_apply_refuses_a_token_for_another_sha(home, app, repo):
    """A token is good only for the sha it was issued for: the change on the
    page was replaced, the new one previewed elsewhere — the old token can't
    apply it, and the new token can't apply under the old sha."""
    c, old = _one_change(home, app)
    tok_old = c.post("/api/proposal/apply", {"diff_sha256": old})["preview"]["token"]
    _stage([("helper_mod.py", "A = 1\n", "A = 3\n")])
    new = c.get("/api/proposal")["diff_sha256"]
    r = c.post("/api/proposal/apply", {"diff_sha256": new, "confirm": tok_old})
    assert r["ok"] is False and "different change" in r["reply"]
    tok_new = c.post("/api/proposal/apply", {"diff_sha256": new})["preview"]["token"]
    r = c.post("/api/proposal/apply", {"diff_sha256": old, "confirm": tok_new})
    assert r["ok"] is False and "not the one you reviewed" in r["reply"]
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 1\n"


def test_classic_apply_refuses_a_v2_token_and_settings_changed_since(
        home, app, repo):
    c, sha = _one_change(home, app)
    # a /v2 preview's token is for the /v2 route only
    v2 = c.post("/api/v2/proposal/apply", {"diff_sha256": sha})["preview"]["token"]
    r = c.post("/api/proposal/apply", {"diff_sha256": sha, "confirm": v2})
    assert r["ok"] is False and "different change" in r["reply"]
    # settings changed between the preview and the confirm: review again
    tok = c.post("/api/proposal/apply", {"diff_sha256": sha})["preview"]["token"]
    write_cfg(home, {**BASE, "contracts_per_trade": 3})
    r = c.post("/api/proposal/apply", {"diff_sha256": sha, "confirm": tok})
    assert r["ok"] is False and "Review again" in r["reply"]
    _untouched(repo)


def test_classic_apply_applies_with_a_valid_token_once(home, app, repo):
    import self_edit
    c, sha = _one_change(home, app)
    tok = c.post("/api/proposal/apply", {"diff_sha256": sha})["preview"]["token"]
    r = c.post("/api/proposal/apply", {"diff_sha256": sha, "confirm": tok})
    assert r["ok"] is True and "Applied 1 file" in r["reply"], r
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 2\n"
    assert self_edit.current() is None

# ── the built-in classic page ──

def test_builtin_classic_page_sends_the_shown_sha_and_blocks_a_cut_diff():
    page = webui._DASH_HTML
    start = page.index("async function propApply(")
    body = page[start:page.index("async function propReject(", start)]
    assert "diff_sha256" in body
    # two steps: the preview's token is sent back, after the owner confirms
    assert "needs_confirm" in body and "window.confirm(" in body
    assert "confirm:pv.token" in body
    # the panel reads the cut flag and disables Apply when it is set
    panel = page[page.index("api('/api/proposal')"):]
    panel = panel[:panel.index("api('/api/trades')")]
    assert "truncated" in panel and "prop-apply" in panel and "disabled" in panel
    assert 'id="prop-apply"' in page


# ── the React /dash page (webui_app -> webui_dist) ──

def test_react_dash_sends_the_shown_sha_and_blocks_a_cut_diff():
    src = (Path(webui.__file__).parent / "webui_app" / "src" / "dash"
           / "Dashboard.tsx").read_text(encoding="utf-8")
    panel = src[src.index("function ProposalPanel("):]
    panel = panel[:panel.index("\n}\n")]
    assert '"/api/proposal/apply", { diff_sha256: sha }' in panel
    assert "needs_confirm" in panel and "confirm: pending.token" in panel
    assert "p.diff_sha256" in panel and "p.truncated" in panel
    assert "disabled={blocked}" in panel and "can't be applied" in panel


def test_built_react_dash_sends_the_shown_sha():
    """The bundle users get (webui_dist) was rebuilt from that source."""
    dist = Path(webui.__file__).parent / "webui_dist" / "assets"
    js = "".join(p.read_text(encoding="utf-8")
                 for p in dist.glob("index-*.js"))
    assert re.search(r'"/api/proposal/apply",\{diff_sha256:', js)
    assert '"/api/proposal/apply",{})' not in js
    assert re.search(r'"/api/proposal/apply",\{diff_sha256:\w+,confirm:', js)
    assert "too large to show in full, so it can't be applied" in js
