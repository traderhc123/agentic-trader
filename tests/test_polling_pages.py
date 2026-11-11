"""The dashboards' timers pause while the page is hidden, and the ones that
can only end one way stop (webui_app/src/lib/polling.ts, the classic pages'
poll() in webui.py). The Node suite drives the timing on a fake clock; here
it runs under pytest, and the classic pages are checked for the guard."""

import os
import re
import shutil
import subprocess

import pytest

import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    try:
        r = subprocess.run([node, "--experimental-strip-types", "-e", "0"],
                           capture_output=True, timeout=30, env=dict(os.environ))
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("node could not be started")
    if r.returncode != 0:
        pytest.skip("this node cannot load TypeScript (needs 22.6 or newer)")
    return node


def test_polling_rules_on_a_fake_clock():
    node = _node()
    r = subprocess.run(
        [node, "--experimental-strip-types", "--test", "--test-reporter=tap",
         os.path.join(ROOT, "webui_app", "tests", "polling.test.mjs")],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env=dict(os.environ))
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert re.search(r"^(?:# |ℹ )fail 0$", r.stdout, re.M), r.stdout[-2000:]
    assert re.search(r"^(?:# |ℹ )pass [1-9]", r.stdout, re.M), r.stdout[-2000:]


PAGES = {"setup": webui._WIZARD_HTML, "dashboard": webui._DASH_HTML}


@pytest.mark.parametrize("name", sorted(PAGES))
def test_classic_pages_pause_while_hidden(name):
    page = PAGES[name]
    assert "function poll(fn,every,o)" in page
    assert "visibilitychange" in page and "document.hidden" in page


def test_classic_pages_have_no_unguarded_polls():
    setup, dash = webui._WIZARD_HTML, webui._DASH_HTML
    assert "poll(refresh,5000)" in dash
    assert "setInterval(refresh" not in dash
    assert "poll(passSizes,900000)" in setup
    assert "setInterval(passSizes" not in setup
    assert "setInterval(checkPass" not in setup
    assert "ms=>ms<120000?3000:15000" in setup
    # the wallet balance after Fund: 10 minutes at most, then a Refresh link
    assert "ms=>ms<600000?4000:null" in setup and 'id="fund-refresh"' in setup
    assert "/api/wallet/balance');\n    if(b.ok)" not in setup


def _scripts(page):
    return re.findall(r"<script>(.*?)</script>", page, re.S)


@pytest.mark.parametrize("name", sorted(PAGES))
def test_classic_page_scripts_parse(name, tmp_path):
    node = _node()
    for i, js in enumerate(_scripts(PAGES[name])):
        f = tmp_path / f"{name}-{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True,
                           text=True, encoding="utf-8", timeout=60,
                           env=dict(os.environ))
        assert r.returncode == 0, r.stderr[-2000:]


_DRIVE_CLASSIC_POLL = r"""
// the classic pages' poll(), on a fake clock and a fake document
let now = 0, seq = 0;
const timers = new Map(), listeners = new Set();
globalThis.Date = class extends Date { static now() { return now; } };
globalThis.setTimeout = (fn, ms) => { const id = ++seq; timers.set(id, {at: now + ms, fn}); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
globalThis.document = {hidden: false,
  addEventListener: (t, fn) => listeners.add(fn),
  removeEventListener: (t, fn) => listeners.delete(fn)};
const flush = () => new Promise((r) => setImmediate(r));
async function advance(ms) {
  const end = now + ms; await flush();
  for (;;) {
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
    if (!next) break;
    timers.delete(next[0]); now = next[1].at; next[1].fn(); await flush();
  }
  now = end;
}
async function hide(h) { document.hidden = h; for (const fn of [...listeners]) fn(); await flush(); }
const out = {};
(async () => {
  let n = 0;
  poll(() => n++, 5000);
  await advance(10000); out.visible = n;            // 0, 5 s, 10 s
  await hide(true); await advance(3600000); out.hidden = n;
  out.timersHidden = timers.size;
  await hide(false); out.shown = n;                 // at once
  let m = 0, stopped = 0;
  poll(() => m++, (ms) => (ms < 600000 ? 4000 : null),
       {immediate: false, onStop: () => stopped++});
  await advance(3600000);
  out.balanceCalls = m; out.stopped = stopped;
  console.log(JSON.stringify(out));
})();
"""


def test_classic_poll_pauses_and_stops(tmp_path):
    node = _node()
    import json
    f = tmp_path / "drive.js"
    f.write_text(webui._POLL_JS + _DRIVE_CLASSIC_POLL, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env=dict(os.environ))
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["visible"] == 3
    assert got["hidden"] == 3 and got["timersHidden"] == 0
    assert got["shown"] == 4
    assert got["balanceCalls"] == 150 and got["stopped"] == 1
