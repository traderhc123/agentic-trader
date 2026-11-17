"""Audit PR9: the classic Apply for an AI code change (POST
/api/proposal/apply, which /dash and the built-in page both use) is held
to the same review rules as /v2: never a change made against older code
(it would write the old whole file back over an update), never a change
whose diff was too large to show whole, and, when the page sends the
diff_sha256 it was shown, never any other change. Each test here failed
on origin/main and passes on the fix.

Hermetic: no broker, wallet, AI, AgentHC or network; self_edit works on a
throwaway source tree."""

import hashlib

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
    r = c.post("/api/proposal/apply", {})  # what /dash (webui_dist) posts
    assert r["ok"] is False and "changed since" in r["reply"]
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == UPDATED
    assert self_edit.current() is not None


def test_classic_route_refuses_a_truncated_change(home, app, repo):
    write_cfg(home, BASE)
    c = app()
    _stage([("big_a.py", "", BIG), ("big_b.py", "", BIG)])
    assert c.get("/api/proposal")["truncated"] is True
    r = c.post("/api/proposal/apply", {})
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
    # the one shown: applied
    now = c.get("/api/proposal")
    r = c.post("/api/proposal/apply", {"diff_sha256": now["diff_sha256"]})
    assert r["ok"] is True, r
    assert (repo / "helper_mod.py").read_text(encoding="utf-8") == "A = 3\n"


# ── the built-in classic page ──

def test_builtin_classic_page_sends_the_shown_sha_and_blocks_a_cut_diff():
    page = webui._DASH_HTML
    start = page.index("async function propApply(")
    body = page[start:page.index("async function propReject(", start)]
    assert "diff_sha256" in body
    # the panel reads the cut flag and disables Apply when it is set
    panel = page[page.index("api('/api/proposal')"):]
    panel = panel[:panel.index("api('/api/trades')")]
    assert "truncated" in panel and "prop-apply" in panel and "disabled" in panel
    assert 'id="prop-apply"' in page
