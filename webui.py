"""Local web UI — a setup wizard and a live dashboard, no terminal needed.

Both servers bind 127.0.0.1 ONLY (never exposed to the network). On a remote
VPS, reach them through an SSH tunnel:  ssh -L 8721:127.0.0.1:8721 user@host

Wizard (``python agent.py setup --web``, port 8721):
    Full setup in the browser — the consent gate (checkbox + typed phrase,
    still the human's own affirmative act), signal source + Lightning wallet,
    Robinhood OAuth (seamless: the wizard listens on the same 127.0.0.1:8721
    the OAuth redirect targets, so approval lands back here automatically),
    dollar-budget or contract sizing, and the safety rails.

Dashboard (served by the same app once ``python agent.py run`` starts, /dash):
    The user's OWN agent serves its own UI: live status, trade log, and a
    command box — pause / resume / dry on|off / set budget N / set cap N / set positions N /
    stop, plus free-text questions answered by the LLM (user's API key)
    against the live status and trade log. Command handling is deliberately
    a fixed allowlist; the LLM can only ANSWER, never trade.

Stdlib only (http.server + threading) — no new Python dependencies. The page
UI is a React app built from webui_app/ into webui_dist/ (committed; no Node
needed to run it). Without a build, the built-in pages below are served.
"""

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_PORT = 8721      # one app, one port (also the Robinhood OAuth redirect port)

# The UI binds loopback only, but a webpage in the user's browser can still
# reach 127.0.0.1: reject requests addressed to a foreign Host (DNS-rebinding)
# and state-changing POSTs carrying a foreign Origin (classic CSRF). Same-origin
# dashboard calls send our own Host/Origin (or, for top-level navigations, none).
_ALLOWED_HOSTS = (f"127.0.0.1:{APP_PORT}", f"localhost:{APP_PORT}")
_ALLOWED_ORIGINS = tuple(f"http://{h}" for h in _ALLOWED_HOSTS)


def open_app_window(url):
    """Open `url` as a chromeless 'app' window (feels native), falling back to
    a normal browser tab. Never raises — a cosmetic nicety, not a dependency."""
    import shutil
    import subprocess
    import sys
    candidates = []
    if sys.platform == "darwin":
        import os
        # `open -na <App>` exits non-zero AFTER Popen returns when the app
        # isn't installed, so check first (like the Windows branch) or a
        # Safari-only Mac would open nothing at all.
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
            if any(os.path.isdir(os.path.join(d, app + ".app")) for d in
                   ("/Applications", os.path.expanduser("~/Applications"))):
                candidates.append(["open", "-na", app, "--args", f"--app={url}"])
    elif sys.platform == "win32":
        import os
        for exe in ("msedge", "chrome", "brave"):  # Edge ships with Windows
            path = shutil.which(exe)
            if not path:
                for base in (os.environ.get("PROGRAMFILES(X86)"),
                             os.environ.get("PROGRAMFILES"),
                             os.environ.get("LOCALAPPDATA")):
                    sub = {"msedge": r"Microsoft\Edge\Application\msedge.exe",
                           "chrome": r"Google\Chrome\Application\chrome.exe",
                           "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe"}[exe]
                    if base and os.path.isfile(os.path.join(base, sub)):
                        path = os.path.join(base, sub)
                        break
            if path:
                candidates.append([path, f"--app={url}"])
    else:
        for exe in ("google-chrome", "chromium", "chromium-browser",
                    "brave-browser", "microsoft-edge"):
            if shutil.which(exe):
                candidates.append([exe, f"--app={url}"])
    for cmd in candidates:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    return False


# ── dashboard login ──────────────────────────────────────────────────────────
# The UI binds loopback only, but loopback is shared by every local process
# and other OS users. Every page and API call requires a per-install secret,
# delivered once via a `?k=<token>` link (printed in the terminal / opened in
# the browser by the agent itself) and then kept in an HttpOnly cookie.
_SESSION_COOKIE = "at_session"
_LOCKED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Locked</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:#090b0f;color:#e7eaf0;
font:13px/1.5 "Inter var",Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,Arial,sans-serif}
.card{max-width:440px;width:100%;background:#0e1117;border:1px solid #1c222d;border-radius:8px;padding:24px}
h2{margin:0 0 8px;font-size:18px;font-weight:600;letter-spacing:-.01em}p{margin:0 0 12px;color:#b4bccb}
pre{margin:0;background:#07090c;border:1px solid #1c222d;border-radius:6px;padding:10px 12px;
font:12px/1.6 ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:#b4bccb;overflow:auto}</style></head>
<body><div class="card">
<h2>This dashboard is locked</h2>
<p>Open the login link printed in the terminal where the agent is running,
or print it again with:</p><pre>python agent.py link</pre></div></body></html>"""


class RawResponse:
    """A GET route's non-JSON reply (e.g. a file download)."""

    def __init__(self, body, ctype="application/octet-stream", headers=None):
        self.body, self.ctype, self.headers = body, ctype, headers or {}


def _token_path():
    home = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
    return os.path.join(home, "ui_token")


def ui_token():
    """This install's dashboard secret (created on first use, private)."""
    import secrets
    path = _token_path()
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    import platform_compat
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tok = secrets.token_urlsafe(32)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tok)
    platform_compat.make_private(tmp)
    os.replace(tmp, path)
    return tok


def _token_ok(candidate):
    import hmac
    return bool(candidate) and hmac.compare_digest(str(candidate), ui_token())


class _AppServer(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a SECOND process bind a port that is
    already in use (unlike POSIX), so a duplicate agent would silently share
    the dashboard port instead of failing the bind. Use exclusive binding."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


WIZARD_PORT = APP_PORT
DASH_PORT = APP_PORT

# ── the app UI (React build, committed) ──────────────────────────────────────
# Source lives in webui_app/ (React + Vite); `npm run build` there writes
# webui_dist/, which is committed so users never need Node. /setup and /dash
# serve webui_dist/index.html; /assets/* serves its hashed files. If the build
# is missing (or the user opens ?ui=classic) the built-in pages below are used.
# /v2 serves webui_dist/v2.html: the redesigned dashboard, built from the same
# source folder as a second entry and read from /api/v2/status. /dash stays
# the default; the two link to each other.
_DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui_dist")
_ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
# The app loads nothing from the network: scripts, styles and fonts all come
# from this agent. Inline styles are allowed (UI library positioning); inline
# scripts are not.
_APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'")


def _app_index():
    """The built app's index.html, or None when no build is present."""
    try:
        with open(os.path.join(_DIST_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _app_v2():
    """The built /v2 dashboard page, or None when the build has none."""
    try:
        with open(os.path.join(_DIST_DIR, "v2.html"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _asset_path(path):
    """Filesystem path for a `/assets/<name>` URL, or None. Only plain file
    names directly inside webui_dist/assets — no sub-paths, no traversal."""
    import re
    name = path[len("/assets/"):]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}", name) or ".." in name:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in _ASSET_TYPES:
        return None
    base = os.path.realpath(os.path.join(_DIST_DIR, "assets"))
    full = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(full) != base or not os.path.isfile(full):
        return None
    return full

# Run-loop controls the dashboard can flip; agent.py polls these.
# The owner's Pause is NOT here: it is saved in config.json ("paused") so it
# survives a restart, and it only stops new entries (agent._paused).
# hold_until: an update or a move is about to restart the agent — until that
# time the heartbeat polls nothing and places no orders, so an in-flight
# order can finish saving. It runs out by itself if the restart never comes.
# replaced: a move file was imported while this agent runs (setup_replaced).
CONTROLS = {"stop": False, "hold_until": 0.0, "replaced": False}
_HOLD_S = 60


def hold_orders(seconds=_HOLD_S, now=None):
    import time
    CONTROLS["hold_until"] = (time.time() if now is None else now) + seconds


def release_orders():
    CONTROLS["hold_until"] = 0.0


def orders_held(now=None):
    import time
    return ((time.time() if now is None else now)
            < float(CONTROLS.get("hold_until") or 0))


def setup_replaced():
    """A move file was imported while this agent runs: the files on disk are
    the imported setup, and what this process has in memory is the setup
    from before. The agent is on hold with no time limit (hold_orders) until
    it restarts, and nothing it has in memory may be saved over those files
    (agent.cmd_run's save_state) — however long the restart takes."""
    return bool(CONTROLS.get("replaced"))

# The classic pages' timers: poll() pauses while the page is hidden and asks
# once at once when it is shown again (a tab left open in the background
# asked the agent — and through it AgentHC or the wallet — all day long);
# `every` is a number of ms or a function of the ms since the start (null
# ends it); o.stopWhen() true ends it; o.onStop runs when it ends by itself.
# The same rules as webui_app/src/lib/polling.ts.
_POLL_JS = """
function poll(fn,every,o){
  o=o||{};const t0=Date.now();let t=null,off=false,busy=false;
  const clear=()=>{if(t!==null)clearTimeout(t);t=null;};
  const end=(own)=>{if(off)return;off=true;clear();
    document.removeEventListener('visibilitychange',vis);if(own&&o.onStop)o.onStop();};
  const next=()=>{clear();if(off||document.hidden)return;
    const ms=typeof every==='function'?every(Date.now()-t0):every;
    if(ms==null){end(true);return;}t=setTimeout(run,ms);};
  async function run(){t=null;if(off||busy||document.hidden)return;busy=true;
    try{await fn();}catch(e){}finally{busy=false;}
    if(off)return;if(o.stopWhen&&o.stopWhen()){end(true);return;}next();}
  function vis(){if(off)return;clear();if(!document.hidden)run();}
  document.addEventListener('visibilitychange',vis);
  if(o.immediate!==false&&!document.hidden)run();else next();
  return ()=>end(false);
}
"""

_STYLE = """
<style>
 :root{
   color-scheme:dark;
   --bg:#090b0f; --surface:#0e1117; --surface-2:#12161e; --raise:#171c26; --sink:#07090c;
   --line:#1c222d; --line-2:#272f3d; --line-3:#343e50;
   --fg:#e7eaf0; --fg-2:#b4bccb; --muted:#8b95a8; --faint:#808a9d;
   /* --faint-deco: decoration only (rules, disabled/locked chrome) — never body text */
   --faint-deco:#566074; --field-line:#5a657a;
   --accent:#5b9dff; --accent-hi:#74adff; --accent-ink:#0a1830;
   --accent-soft:rgba(91,157,255,.12); --accent-line:rgba(91,157,255,.42);
   --up:#33c17d; --up-soft:rgba(51,193,125,.12); --up-line:rgba(51,193,125,.45);
   --down:#f0605d; --down-soft:rgba(240,96,93,.12);
   --warn:#e9a640; --warn-soft:rgba(233,166,64,.11); --warn-line:rgba(233,166,64,.45);
   --live:#ff4d5e; --live-soft:rgba(255,77,94,.12); --live-line:rgba(255,77,94,.50);
   --ok:var(--up); --err:var(--down);
   --r-sm:4px; --r:6px; --r-lg:8px;
   --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:20px; --s6:24px; --s8:32px;
   --font:"Inter var",Inter,-apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",system-ui,Roboto,"Helvetica Neue",Arial,sans-serif;
   --mono:ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono","Cascadia Mono","Roboto Mono",Menlo,Consolas,monospace;
   --fs-xs:11px; --fs-sm:12px; --fs:13px; --fs-md:14px; --fs-lg:16px; --fs-xl:22px;
 }
 *{box-sizing:border-box}
 html{background:var(--bg)}
 body{margin:0;background:var(--bg);color:var(--fg);font:var(--fs)/1.5 var(--font);
   -webkit-font-smoothing:antialiased;font-feature-settings:"cv11","ss01"}
 /* tabular numerals only where digits must line up — "tnum" on prose also
    fixes the width of hyphens (Inter), which spaces out "dry-run" */
 body :is(.num,.kpi .v,.t td,.feed .pnl,.feed .when,.chart-head,.chart-head *,.chart-axis,.pass .q,.pass .p,.inv-quote){
   font-variant-numeric:tabular-nums}
 a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
 code,.mono{font-family:var(--mono);font-size:.92em}
 code{background:var(--raise);border:1px solid var(--line);padding:0 4px;border-radius:3px;color:var(--fg-2)}
 svg.i{width:14px;height:14px;flex:none;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}

 /* buttons: bare = neutral; main actions add .btn-primary */
 button,.btn{font:500 var(--fs)/1 var(--font);height:30px;padding:0 12px;border-radius:var(--r);cursor:pointer;
   display:inline-flex;align-items:center;gap:6px;white-space:nowrap;border:1px solid var(--line-2);
   background:var(--surface-2);color:var(--fg);text-decoration:none;transition:background .12s,border-color .12s,color .12s}
 button:hover,.btn:hover{background:var(--raise);border-color:var(--line-3);text-decoration:none}
 button:focus-visible,.btn:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
 button:disabled,.btn:disabled{opacity:.45;cursor:not-allowed}
 .btn-primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
 .btn-primary:hover{background:var(--accent-hi);border-color:var(--accent-hi)}
 .btn-warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}
 .btn-warn:hover{background:rgba(233,166,64,.18);border-color:var(--warn)}
 .btn-live{color:var(--live);border-color:var(--live-line);background:var(--live-soft)}
 .btn-live:hover{background:rgba(255,77,94,.18);border-color:var(--live)}
 .btn-ghost{background:transparent;border-color:transparent;color:var(--fg-2)}
 .btn-ghost:hover{background:var(--surface-2);border-color:var(--line)}
 .btn-sm{height:24px;padding:0 8px;font-size:var(--fs-sm)}

 /* inputs */
 input[type=text],input[type=password],input[type=number],select,textarea{
   width:100%;height:34px;padding:0 10px;background:var(--sink);color:var(--fg);border:1px solid var(--field-line);
   border-radius:var(--r);font:var(--fs-md) var(--font);transition:border-color .12s,box-shadow .12s}
 textarea{height:auto;padding:8px 10px;line-height:1.5}
 select{appearance:none;-webkit-appearance:none;padding-right:30px;
   background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%237c8597' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
   background-repeat:no-repeat;background-position:right 11px center}
 input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
 input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
 :is(.disc,pre,#chatlog):focus-visible{outline:2px solid var(--accent);outline-offset:1px}
 input[type=checkbox],input[type=radio]{accent-color:var(--accent);width:16px;height:16px;margin:0;flex:none}
 ::placeholder{color:var(--faint)}

 /* pills + tags */
 .pill{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 8px;border-radius:var(--r-sm);
   font:600 var(--fs-xs)/1 var(--font);letter-spacing:.04em;text-transform:uppercase;border:1px solid var(--line-2);color:var(--fg-2);white-space:nowrap}
 .pill .dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
 .pill.dry{color:var(--accent);border-color:var(--accent-line);background:var(--accent-soft)}
 .pill.live{color:var(--live);border-color:var(--live-line);background:var(--live-soft)}
 .pill.paused{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}
 .pill.bad{color:var(--down);border-color:rgba(240,96,93,.45);background:var(--down-soft)}
 .pill.open .dot{background:var(--up);box-shadow:0 0 0 3px rgba(51,193,125,.18)}
 .tag{display:inline-block;font:600 10px/16px var(--mono);letter-spacing:.04em;padding:0 5px;border-radius:3px;vertical-align:1px}
 .tag.call{color:var(--up);background:var(--up-soft)} .tag.put{color:var(--down);background:var(--down-soft)}
 .tag.dry{color:var(--accent);background:var(--accent-soft)}
 .tag.live{color:var(--live);background:var(--live-soft)}
 .up{color:var(--up)} .down{color:var(--down)} .muted{color:var(--muted)} .faint{color:var(--faint)}
 .num{font-family:var(--mono)}
 .ok{color:var(--ok)} .err{color:var(--err);white-space:pre-line}

 /* panels */
 .panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);min-width:0}
 .ph{display:flex;align-items:center;gap:var(--s2);min-height:38px;padding:8px var(--s4);border-bottom:1px solid var(--line)}
 .ph h2[tabindex]:focus{outline:none}
 .ph h2{margin:0;font:600 var(--fs-xs)/1.4 var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--fg-2)}
 .ph .count{font:500 var(--fs-xs)/1 var(--mono);color:var(--muted);background:var(--raise);border-radius:3px;padding:3px 5px}
 .ph .sp{flex:1}
 .pb{padding:var(--s3) var(--s4)}

 /* long text + code */
 pre{background:var(--sink);border:1px solid var(--line);border-radius:var(--r);padding:var(--s3);
   font:12px/1.6 var(--mono);color:var(--fg-2);white-space:pre-wrap;word-break:break-word;overflow:auto;max-width:100%}
 .disc{background:var(--sink);border:1px solid var(--line-2);border-radius:var(--r);padding:12px 14px;
   font-size:13px;color:var(--fg-2);max-height:320px;overflow:auto;line-height:1.55}
 .disc h3,.disc h4,.disc h5,.disc h6{margin:.9rem 0 .35rem;font-size:13px;font-weight:600;color:var(--fg)}
 .disc h3{font-size:14px} .disc p{margin:.45rem 0} .disc ul{margin:.3rem 0 .5rem 1.1rem;padding:0}
 .disc blockquote{margin:.5rem 0;padding:.4rem .7rem;border-left:2px solid var(--accent);color:var(--muted)}
 .disc code{font-size:12px}
 #pass-help.disc{max-height:none;border-left:2px solid var(--warn);background:var(--surface)}

 /* app bar (both pages) */
 .appbar{position:sticky;top:0;z-index:5;background:rgba(9,11,15,.88);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
   border-bottom:1px solid var(--line)}
 .appbar-in{max-width:1360px;margin:0 auto;min-height:52px;padding:0 24px;display:flex;align-items:center;gap:var(--s3);
   flex-wrap:wrap;row-gap:8px}
 @media (max-width:1100px){
   .appbar .lbl-sm,.brand .crumb,.brand .page{display:none}
   .acts-a .btn-ghost{width:30px;padding:0;justify-content:center}
   .acts-a #upd-badge{width:auto;padding:0 8px}
 }
 @media (max-width:900px) and (min-width:641px){.appbar-in{padding-top:8px;padding-bottom:8px}}
 .brand{display:flex;align-items:center;gap:10px;min-width:0;color:var(--fg)}
 a.brand:hover{text-decoration:none}
 .mark{width:24px;height:24px;flex:none}
 .brand .name{font:600 var(--fs-md)/1 var(--font);letter-spacing:-.01em;white-space:nowrap}
 .brand .crumb{color:var(--faint);font-size:var(--fs-md)}
 .brand .page{color:var(--fg-2);font-size:var(--fs-md);white-space:nowrap}
 .appbar .sp{flex:1}
 .vsep{width:1px;height:20px;background:var(--line-2);flex:none}
 .actions{display:flex;gap:6px;align-items:center}

 /* chat bubbles (dashboard + wizard) */
 #chatlog{display:flex;flex-direction:column;gap:10px;height:300px;overflow-y:auto;background:var(--sink);
   border:1px solid var(--line);border-radius:var(--r);padding:var(--s3);font-size:var(--fs)}
 .msg{max-width:88%;align-self:flex-start;min-width:0}
 .msg .who{font:600 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
 .msg .body{padding:8px 11px;border-radius:10px;white-space:pre-wrap;overflow-wrap:anywhere}
 .msg.me{align-self:flex-end}
 .msg.me .who{color:var(--accent);text-align:right}
 .msg.me .body{background:var(--accent-soft);border:1px solid var(--accent-line);color:var(--fg);border-bottom-right-radius:3px}
 .msg.agent .body{background:var(--surface-2);border:1px solid var(--line);color:var(--fg-2);border-bottom-left-radius:3px}
 .chatrow{display:flex;gap:6px}
 .chatrow input{flex:1;min-width:0;height:32px}
 .chatrow button{height:32px}
 .hint{font-size:var(--fs-sm);color:var(--muted);line-height:1.45}

 @media (max-width:640px){
   /* the wrapped phone header is ~140px: pinned, it would cover focused
      controls (WCAG 2.4.11) and eat reading space — let it scroll away */
   .appbar{position:static}
   .appbar-in{padding-left:16px;padding-right:16px;height:auto;min-height:52px;flex-wrap:wrap;
     padding-top:10px;padding-bottom:10px;row-gap:10px}
   .brand .crumb,.brand .page,.vsep.hide-sm,.lbl-sm{display:none}
   .brand{order:1} .acts-a{order:2;margin-left:auto} .appbar .sp{display:none}
   .appbar .pill{order:3}
   .acts-b{order:5;width:100%} .acts-b button{flex:1;justify-content:center}
   .acts-b{gap:8px}
   .acts-a .btn-ghost{width:44px;height:44px;padding:0;justify-content:center}
   .acts-a #upd-badge{width:auto;padding:0 8px}
   .hide-sm{display:none}
   #chatlog{height:220px}
   /* touch: >=44px targets; 16px fields so iOS Safari does not zoom on focus */
   button,.btn{height:auto;min-height:44px}
   .btn-sm{min-height:40px}
   input[type=text],input[type=password],input[type=number],select{font-size:16px;height:44px}
   textarea{font-size:16px}
   .chatrow input,.chatrow button{height:44px}
 }
 @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>"""

_WIZARD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentic-trader setup</title>""" + _STYLE + """
<style>
 #bg{display:none}
 .wz{max-width:1120px;margin:0 auto;padding:var(--s6) 24px var(--s8)}
 .wz-bar .appbar-in{max-width:1120px}
 .eyebrow{font:600 var(--fs-xs)/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
 .wz-head{margin-bottom:var(--s5)}
 .wz-head .eyebrow{margin-bottom:8px}
 .wz-head h1{margin:0;font:600 24px/1.2 var(--font);letter-spacing:-.02em}
 .wz-head p{margin:8px 0 0;color:var(--fg-2);max-width:66ch;font-size:var(--fs-md)}
 .wz-body{display:grid;grid-template-columns:220px minmax(0,1fr);gap:var(--s6);align-items:start}
 .wz-body>*,.wz-main>*{min-width:0}
 .wz-side{position:sticky;top:72px}

 /* stepper (render() builds span.chip > span.n + span.lbl) */
 #stepbar{display:flex;flex-direction:column;gap:2px}
 #stepbar .chip{display:flex;align-items:center;gap:10px;height:36px;padding:0 10px;border-radius:var(--r);
   color:var(--muted);font:400 var(--fs)/1.5 var(--font);cursor:pointer;user-select:none;position:relative;
   width:100%;border:0;background:none;text-align:left;white-space:nowrap}
 #stepbar .chip:hover{background:var(--surface-2)}
 /* locked steps are disabled buttons: dim decoration tone is exempt from text contrast */
 #stepbar .chip.lock{color:var(--faint-deco);cursor:default;opacity:1}
 #stepbar .chip.lock:hover{background:none}
 #stepbar .chip.done{color:var(--fg-2)}
 #stepbar .chip.cur{background:var(--surface-2);color:var(--fg);box-shadow:inset 2px 0 0 var(--accent);font-weight:500}
 #stepbar .n{width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font:600 10px/1 var(--mono);
   border:1px solid var(--line-3);color:var(--muted);flex:none}
 #stepbar .chip.lock .n{border-color:var(--line-2);color:var(--faint-deco)}
 #stepbar .chip.done .n{background:var(--up-soft);border-color:transparent;color:var(--up);font-size:11px}
 #stepbar .chip.cur .n{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
 #stepbar .meta{margin-left:auto;font:500 10px/1 var(--mono);color:var(--faint)}
 .stepnow,.stepseg{display:none}
 .stepnow{align-items:center;gap:var(--s3);font:600 var(--fs-md)/1.3 var(--font);color:var(--fg)}
 .stepnow .nx{margin-left:auto;font:500 var(--fs-sm)/1.3 var(--font);color:var(--muted);white-space:nowrap}
 .stepnow button.nx{height:auto;min-height:32px;padding:0 6px;margin-right:-6px}
 .stepnow .nx.go{color:var(--accent);cursor:pointer}
 /* headings take programmatic focus on step change (screen readers announce
    the new step); no ring — it is not an interactive control */
 .wz-main section>h2[tabindex]:focus,.wz-main section>h2[tabindex]:focus-visible{outline:none}
 .stepseg{grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;margin-top:10px}
 .stepseg button{display:block;width:100%;height:40px;min-height:40px;padding:0;margin:0;border:0;border-radius:2px;cursor:default;
   background:linear-gradient(var(--line-2),var(--line-2)) center/100% 4px no-repeat}
 .stepseg button:disabled{opacity:1}
 .stepseg button.on{cursor:pointer}
 .stepseg button.on:hover{background-size:100% 6px}
 .stepseg button.done{background-image:linear-gradient(var(--up),var(--up))}
 .stepseg button.cur{background-image:linear-gradient(var(--accent),var(--accent))}
 .stepseg button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
 .upd{margin-top:var(--s4);padding:var(--s3);border:1px solid var(--line);border-radius:var(--r-lg);font-size:var(--fs-sm);color:var(--muted)}
 .upd{display:flex;flex-direction:column;gap:8px}
 .upd button{width:100%;justify-content:center}
 #upd-msg:empty{display:none}
 .upd pre{max-height:12rem;margin:8px 0}
 #upd-msg{display:block;overflow-wrap:anywhere}

 /* step cards — render() sets className to 'active' or '' only */
 .wz-main section{display:none;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);min-width:0}
 .wz-main section.active{display:block}
 .wz-main section>h2:first-child{margin:0;padding:var(--s5) var(--s6) var(--s4);border-bottom:1px solid var(--line);
   font:600 18px/1.3 var(--font);letter-spacing:-.01em;color:var(--fg)}
 .wz-main section>h2 .eyebrow{display:block;margin-bottom:7px}
 .wz-main section>p.ok{margin:0;padding:10px var(--s6);background:var(--up-soft);color:var(--up);border-bottom:1px solid var(--line);font-size:var(--fs)}
 .sec-b{padding:var(--s5) var(--s6)}
 .sec-b+.sec-b{border-top:1px solid var(--line)}
 :where(.sec-b,.stack,.opt,.adv-b,#fund-box,#help .pb) p{margin:0}
 .sec-b>*+*,.stack>*+*{margin-top:var(--s3)}
 .copy{font-size:var(--fs);color:var(--fg-2);line-height:1.6;max-width:72ch}
 .copy b{color:var(--fg);font-weight:600}
 .fs-grid{display:grid;grid-template-columns:200px minmax(0,1fr);gap:var(--s6)}
 .fs-grid>*{min-width:0}
 .fs-lbl{font:600 var(--fs)/1.4 var(--font);color:var(--fg)}
 .fs-lbl p{font-weight:400;color:var(--muted);font-size:var(--fs-sm);margin:4px 0 0;line-height:1.5}
 .field>label{display:block;font:500 var(--fs-sm)/1.4 var(--font);color:var(--fg-2);margin-bottom:6px}
 .field .help{font-size:var(--fs-sm);color:var(--muted);margin:7px 0 0;line-height:1.55}
 .field+.field{margin-top:var(--s4)}
 .two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--s2)}
 label.check{display:flex;gap:10px;align-items:flex-start;padding:var(--s3);border:1px solid var(--line-2);
   border-radius:var(--r);background:var(--sink);color:var(--fg-2);font-size:var(--fs);line-height:1.5;cursor:pointer}
 label.check input{margin-top:2px}
 label.check b{color:var(--fg)}
 label.check:has(input:checked){border-color:var(--accent-line)}
 label.check:has(#sf-dry:not(:checked)){border-color:var(--live-line);background:var(--live-soft)}
 #sf-live-warn{border-left-color:var(--live)}
 .two>.field{margin-top:0}
 .opts{display:flex;flex-direction:column;gap:var(--s2)}
 #c-phrase{font-family:var(--mono);letter-spacing:.04em}
 #disclaimer{max-height:340px}
 .notice{border:1px solid var(--line-2);border-left:2px solid var(--warn);border-radius:var(--r);padding:var(--s3) var(--s4);
   font-size:var(--fs);color:var(--fg-2);background:var(--surface);line-height:1.55}
 .notice b{color:var(--fg)}
 .notice ul{margin:6px 0 0;padding-left:18px} .notice li{margin:3px 0}
 .sep{display:flex;align-items:center;gap:var(--s3);color:var(--faint);font-size:var(--fs-sm)}
 .sep::before,.sep::after{content:"";flex:1;height:1px;background:var(--line)}

 /* access copy: three fact cells */
 .facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--s2)}
 .facts>div{border:1px solid var(--line);border-radius:var(--r);padding:10px 12px;background:var(--surface-2);
   font-size:var(--fs-sm);color:var(--fg-2);line-height:1.5;min-width:0}
 .facts h4{margin:0 0 4px;font:600 var(--fs-xs)/1.3 var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--fg)}

 /* passes */
 .passes{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:var(--s2)}
 button.pass{height:auto;flex-direction:column;align-items:flex-start;gap:0;padding:12px 12px 11px;text-align:left;
   border-radius:var(--r-lg);background:var(--surface-2);white-space:normal;min-width:0}
 button.pass:hover{border-color:var(--accent-line);background:var(--raise)}
 .pass .tagline{font:600 9px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent);height:9px;margin-bottom:8px}
 .pass .q{font:600 22px/1 var(--mono);letter-spacing:-.02em;color:var(--fg)}
 .pass .u{font:500 var(--fs-sm)/1.2 var(--font);color:var(--muted);margin-top:6px}
 .pass .p{font:500 var(--fs-sm)/1 var(--mono);color:var(--fg-2);margin-top:12px;padding-top:10px;border-top:1px solid var(--line);width:100%}
 button.pass.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset;background:var(--accent-soft)}
 .status-line{font-size:var(--fs);color:var(--muted)}
 .status-line:empty{display:none}

 /* lightning invoice */
 .invoice{display:flex;gap:var(--s5);align-items:flex-start;border:1px solid var(--line-2);border-radius:var(--r-lg);
   background:var(--sink);padding:var(--s4)}
 .invoice>.inv-r{flex:1;min-width:0}
 .qr{width:168px;height:168px;flex:none;background:#fff;border-radius:var(--r);padding:8px}
 .qr svg{width:100%;height:100%;display:block;border-radius:0!important}
 .inv-quote{font:600 16px/1.35 var(--mono);color:var(--fg);letter-spacing:-.01em}
 pre.bolt{margin:var(--s3) 0 0;padding:9px 10px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
   font:11.5px/1.5 var(--mono);color:var(--fg-2);white-space:pre-wrap;word-break:break-all;max-height:62px;overflow:auto}
 .btnrow{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;margin-top:var(--s3)}
 .btnrow .msg-s{font-size:var(--fs-sm);color:var(--up)}
 .inv-meta{font-size:var(--fs-sm);color:var(--muted);margin-top:var(--s3)!important;line-height:1.5}
 #pass-box>*+*{margin-top:var(--s3)}
 #pass-help ul{margin:6px 0 0;padding-left:18px}

 /* advanced disclosure */
 details.adv{border:1px solid var(--line);border-radius:var(--r-lg);background:var(--surface)}
 details.adv>summary{list-style:none;cursor:pointer;display:flex;align-items:flex-start;gap:8px;padding:10px var(--s4);
   font-size:var(--fs);color:var(--fg-2);line-height:1.45}
 details.adv>summary::-webkit-details-marker{display:none}
 details.adv>summary:hover{color:var(--fg)}
 details.adv>summary .chev{margin-top:2px;transition:transform .15s;color:var(--muted)}
 details.adv[open]>summary .chev{transform:rotate(90deg)}
 details.adv[open]>summary{border-bottom:1px solid var(--line)}
 .adv-b{padding:var(--s4)}
 .adv-b>*+*{margin-top:var(--s3)}
 .inrow{display:flex;gap:var(--s2);align-items:center}
 .inrow input{flex:1;min-width:0}
 #fund-box{border:1px solid var(--line);border-radius:var(--r);padding:var(--s4);background:var(--surface-2)}
 #fund-box>*+*{margin-top:var(--s3)}

 /* footers: the LAST .navrow of a step card */
 .navrow{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
 .wz-main section>.navrow:last-child{padding:var(--s4) var(--s6);border-top:1px solid var(--line);background:var(--surface-2);
   border-radius:0 0 var(--r-lg) var(--r-lg);gap:var(--s3)}
 .navrow.inline{padding:0;border:0;background:none}
 .navrow .sp{flex:1}
 .navrow .note{font-size:var(--fs-sm);color:var(--muted)}
 .navrow span[id]:empty{display:none}
 .navrow>span[id]{font-size:var(--fs-sm);overflow-wrap:anywhere;min-width:0}

 /* broker fields + launch options */
 #brk-fields .field{margin-top:var(--s3)}
 #brk-fields label.check{margin-top:var(--s3)}
 #brk-fields:empty{display:none}
 .launch{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--s3)}
 .opt{border:1px solid var(--line-2);border-radius:var(--r-lg);padding:var(--s4);background:var(--surface-2);min-width:0;display:flex;flex-direction:column}
 .opt>h3+p{margin-top:6px}
 .opt>*+*{margin-top:var(--s3)}
 .opt h3{margin:0;font:600 var(--fs-md)/1.35 var(--font);color:var(--fg)}
 .opt h3 .eyebrow{display:block;margin-bottom:6px}
 .opt>.cta{margin-top:auto;padding-top:var(--s3)}
 #done-body:empty{display:none}
 #done-body details summary{cursor:pointer}
 #deploy-msg:empty{display:none}

 /* assistant */
 #help.assist{margin-top:var(--s4)}
 #help .pb>*+*{margin-top:var(--s3)}
 #help #chatlog{height:160px}
 #help #chatlog:empty{display:none}

 @media (max-width:860px){
   .wz-body{grid-template-columns:minmax(0,1fr);gap:var(--s4)}
   .wz-main section>h2 .eyebrow{display:none}
   .wz-side{position:static}
   #stepbar .chip{display:none}
   .stepnow{display:flex}
   .stepseg{display:grid}
   /* the aside dissolves so the update row can sit BELOW the step card + help */
   .wz-side{display:contents}
   #stepbar{order:0} .wz-main{order:1} .upd{order:2}
   .upd{flex-direction:row;align-items:center;gap:var(--s3);flex-wrap:wrap;margin-top:0;padding:0;border:0;background:none}
   .upd button{width:auto;margin:0}
   .upd #upd-btn{background:transparent;border-color:transparent;color:var(--muted);padding-left:0}
   .upd #upd-btn:hover{color:var(--fg)}
   #upd-msg{flex:1 1 200px}
   .fs-grid{grid-template-columns:minmax(0,1fr);gap:var(--s3)}
 }
 @media (max-width:640px){
   .wz{padding:var(--s4) 16px var(--s6)}
   .wz-head h1{font-size:21px}
   .wz-head p{font-size:var(--fs)}
   .wz-main section>h2:first-child,.sec-b,.wz-main section>.navrow:last-child{padding-left:var(--s4);padding-right:var(--s4)}
   .wz-main section>p.ok{padding-left:var(--s4);padding-right:var(--s4)}
   .wz-main section>h2:first-child{padding-top:var(--s4);padding-bottom:var(--s3);font-size:17px}
   .sec-b{padding-top:var(--s4);padding-bottom:var(--s4)}
   .passes{grid-template-columns:1fr 1fr}
   .facts,.two,.launch{grid-template-columns:minmax(0,1fr)}
   .invoice{flex-direction:column;align-items:stretch}
   .qr{width:180px;height:180px;align-self:center}
   .wz-main section>.navrow:last-child .sp{display:none}
   .wz-main section>.navrow:last-child .btn-primary{flex:1;justify-content:center}
   .wz-main section>.navrow:last-child .note{flex-basis:100%}
   .wz-main section>.navrow:last-child>span[id]:not(:empty){flex-basis:100%}
   details.adv>summary{min-height:44px;align-items:center}
   details.adv>summary .chev{margin-top:0}
   .stepnow button.nx{min-height:40px}
   #help #chatlog{height:220px}
 }
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
 <symbol id="i-check" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></symbol>
 <symbol id="i-arrow" viewBox="0 0 16 16"><path d="M3 8h10M9 4l4 4-4 4"/></symbol>
 <symbol id="i-back" viewBox="0 0 16 16"><path d="M13 8H3M7 4 3 8l4 4"/></symbol>
 <symbol id="i-refresh" viewBox="0 0 16 16"><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"/></symbol>
 <symbol id="i-copy" viewBox="0 0 16 16"><rect x="5" y="5" width="8.5" height="8.5" rx="1.5"/><path d="M3 10.5V3.8C3 3.3 3.3 3 3.8 3h6.7"/></symbol>
 <symbol id="i-ext" viewBox="0 0 16 16"><path d="M9 3h4v4M13 3 7.5 8.5M11.5 9.5v3c0 .6-.4 1-1 1h-7c-.6 0-1-.4-1-1v-7c0-.6.4-1 1-1h3"/></symbol>
 <symbol id="i-chev" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></symbol>
 <symbol id="i-bolt" viewBox="0 0 16 16"><path d="M9 1.8 3.5 9h4l-.6 5.2L12.5 7h-4z"/></symbol>
 <symbol id="i-shield" viewBox="0 0 16 16"><path d="M8 1.8 2.8 3.8v4c0 3 2.2 5.4 5.2 6.4 3-1 5.2-3.4 5.2-6.4v-4z"/></symbol>
 <symbol id="mark" viewBox="0 0 24 24"><rect x=".5" y=".5" width="23" height="23" rx="5.5" fill="#12161e" stroke="#343e50"/>
  <path d="M5.5 15.5l4-4.5 3 2.6 5.5-6.6" fill="none" stroke="#5b9dff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="18" cy="7" r="1.6" fill="#5b9dff"/></symbol>
</svg>
<canvas id="bg" aria-hidden="true"></canvas>

<header class="appbar wz-bar"><div class="appbar-in">
 <a class="brand" href="/dash"><svg class="mark" aria-hidden="true"><use href="#mark"/></svg>
  <span class="name">Agentic Trader</span><span class="crumb">/</span><span class="page">Setup</span></a>
 <span class="sp"></span>
 <span class="pill hide-sm" title="This page is served by your agent on this computer only"><span class="dot" style="color:var(--up)"></span>127.0.0.1 · local only</span>
 <div class="actions acts-a">
  <a class="btn" href="/dash">Dashboard<svg class="i"><use href="#i-arrow"/></svg></a>
 </div>
</div></header>

<main class="wz">
<div class="wz-head">
 <div class="eyebrow">Setup</div>
 <h1>Set up your trading agent</h1>
 <p>Everything runs on your own machine — this wizard is served from
 127.0.0.1 by your agent itself. It moves real money; read each step carefully.</p>
</div>

<div class="wz-body">
<aside class="wz-side">
 <div id="stepbar" role="navigation" aria-label="Setup steps"></div>
 <div class="upd">
  <button onclick="checkUpdates()" id="upd-btn"><svg class="i"><use href="#i-refresh"/></svg>Check for updates</button>
  <button onclick="pullUpdates()" id="upd-pull" class="btn-primary" style="display:none">Pull latest code</button>
  <span id="upd-msg" class="muted"></span>
 </div>
</aside>

<div class="wz-main">
<section id="s-consent"><h2 tabindex="-1"><span class="eyebrow">Step 1 of 7</span>Agreement</h2>
 <div class="sec-b">
  <div class="disc" id="disclaimer" tabindex="0" role="region" aria-label="User agreement and disclaimer">loading…</div>
  <label class="check"><input type="checkbox" id="c-read"><span>I have read the entire agreement
  above and I accept all of its terms, including that <b>all liability is mine</b>
  and that no party here is a registered investment adviser.</span></label>
  <div class="field">
   <label for="c-phrase">Type exactly <b class="num">I AGREE AND ACCEPT ALL LIABILITY</b> to accept:</label>
   <input type="text" id="c-phrase" autocomplete="off" spellcheck="false" autocapitalize="characters" autocorrect="off">
  </div>
 </div>
 <div class="navrow"><span id="c-msg" role="status" aria-live="polite"></span><span class="sp"></span>
  <button class="btn-primary" onclick="doConsent()">Accept &amp; continue<svg class="i"><use href="#i-arrow"/></svg></button></div>
</section>

<section id="s-llm"><h2 tabindex="-1"><span class="eyebrow">Step 2 of 7</span>Connect your AI (recommended)</h2>
 <div class="sec-b">
  <p class="copy">Any model works — Claude, ChatGPT/OpenAI, Grok, Gemini,
  OpenRouter (hundreds of models with one key), DeepSeek, Mistral, Groq, or a
  model running on this computer. It powers the help chat, your plain-English
  trading policy and the dashboard Q&amp;A. It never places trades. Skippable.</p>
 </div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">API key<p>Claude, OpenAI, Grok, Gemini, OpenRouter or a local model.</p></div>
  <div class="stack">
   <div class="field"><label for="llm-key">1. Paste an API key</label>
    <input type="password" id="llm-key" placeholder="sk-ant-…, sk-…, xai-…, AIza…, sk-or-…"
     autocomplete="off" oninput="llmKeyTyped()" onkeydown="if(event.key==='Enter')llmFind()">
    <p class="help">The provider is detected from the key.
    Need one? <a id="llm-keys-link" href="https://console.anthropic.com/settings/keys"
    target="_blank" rel="noopener">Get a key ↗</a> · <b>Claude Max or Team plan?</b>
    It includes monthly API credits — link them to a Claude Console organization
    (claude.ai → your plan's API credits), then create a key there. Signing in
    with a chat account (claude.ai, chatgpt.com) doesn't connect here; the API
    key does. Keys stay on this computer.</p></div>
   <details id="llm-adv" class="adv"><summary><svg class="i chev"><use href="#i-chev"/></svg><span>Provider / local model / custom endpoint</span></summary>
    <div class="adv-b">
     <div class="field"><label for="llm-provider">Provider</label>
      <select id="llm-provider" onchange="llmProviderChanged()" aria-label="Provider"></select></div>
     <div class="field"><label for="llm-base">Endpoint URL (custom / local only)</label>
      <input type="text" id="llm-base" placeholder="http://localhost:11434/v1" aria-label="Custom or local endpoint URL"></div>
     <p class="muted">Local models (Ollama, LM Studio) need no key — pick
     "Local model", check the URL, then press Find models.</p>
    </div>
   </details>
   <div class="navrow inline"><button class="btn-primary" onclick="llmFind()">Find models</button>
    <span id="llm-find-msg" role="status" aria-live="polite"></span></div>
  </div>
 </div></div>
 <div class="sec-b" id="llm-pick" style="display:none"><div class="fs-grid">
  <div class="fs-lbl">Model<p>Pick one the key can use.</p></div>
  <div class="field"><label for="llm-model">2. Pick a model</label>
   <input type="text" id="llm-model" list="llm-models" autocomplete="off"
    placeholder="start typing to filter">
   <datalist id="llm-models"></datalist>
   <p class="help">Bigger models follow your trading policy more reliably;
   any error or unclear answer from the model means the trade is skipped.</p></div>
 </div></div>
 <div class="navrow"><span id="llm-msg" role="status" aria-live="polite"></span><span class="sp"></span>
  <button onclick="goBack()"><svg class="i"><use href="#i-back"/></svg>Back</button>
  <button class="back" id="llm-skip" onclick="skipLLM()">Skip for now</button>
  <button id="llm-connect" class="btn-primary" onclick="doLLM()" style="display:none">Connect &amp; continue<svg class="i"><use href="#i-arrow"/></svg></button></div>
</section>

<section id="s-source"><h2 tabindex="-1"><span class="eyebrow">Step 3 of 7</span>Signal source</h2>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Feed<p>Where your agent's trade ideas come from.</p></div>
  <div class="stack">
   <div class="field"><label for="src">Source</label>
   <select id="src" onchange="srcChanged()">
    <option value="agenthc">AgentHC Agentic Day Trade Ideas (journal feed, sats-priced)</option>
    <option value="manual">My own commands (commands.jsonl)</option>
    <option value="url">A JSON feed URL I provide</option>
   </select></div>
  </div>
 </div></div>
 <div id="src-agenthc">
  <div class="sec-b"><div class="fs-grid">
   <div class="fs-lbl">Signals<p>How many of AgentHC's ideas to follow.</p></div>
   <div class="field"><label for="hc-scope">Which trade signals?</label>
    <select id="hc-scope">
     <option value="main">Main pick only — one high-conviction trade a day (default)</option>
     <option value="all">Main pick + "other trades" — wider journal, several entries a day</option>
    </select>
    <p class="help">"Other trades" follows AgentHC's broader shadow journal; your
    daily entry cap and per-trade budget still apply to every signal.</p></div>
  </div></div>
  <div class="sec-b"><div class="fs-grid">
   <div class="fs-lbl">Access — prepay signal days<p>Prepaid in sats over Lightning.</p></div>
   <div class="stack">
    <p class="copy">One Lightning payment from the wallet you already use (Cash
    App, Strike, Phoenix, Alby…) — <b>~$10 per signal day</b>, priced in sats.</p>
    <div class="facts">
     <div><h4>Billing</h4>A day
     is only used when your agent actually receives a trade signal (an entry or
     an exit) that day; quiet days are free.</div>
     <div><h4>Carry-over</h4>Unused days carry over when you buy more.</div>
     <div><h4>Custody</h4>The agent never
     holds money. You pay each invoice from your own wallet.</div>
    </div>
    <p class="muted" style="font-size:var(--fs-sm)">"Main pick only" uses a day only
    for main-pick signals; adding "other trades" (above) uses days on those
    too.</p>
    <div class="passes">
     <button class="pass" data-days="1" onclick="buyPass(1)"><span class="tagline">Try it</span><span class="q">1</span><span class="u">signal day</span><span class="p">≈ $10</span></button>
     <button class="pass" data-days="5" onclick="buyPass(5)"><span class="tagline"></span><span class="q">5</span><span class="u">signal days</span><span class="p">≈ $50</span></button>
     <button class="pass" data-days="20" onclick="buyPass(20)"><span class="tagline"></span><span class="q">20</span><span class="u">signal days</span><span class="p">≈ $200</span></button>
     <button class="pass" data-days="60" onclick="buyPass(60)"><span class="tagline"></span><span class="q">60</span><span class="u">signal days</span><span class="p">≈ $600</span></button>
    </div>
    <p id="pass-sizes-note" class="status-line" role="status"></p>
    <p id="pass-status" class="status-line" role="status"></p>
    <div id="pass-box" style="display:none">
     <div class="invoice">
      <div id="pass-qr" class="qr" style="display:none"></div>
      <div class="inv-r">
       <p id="pass-quote" class="inv-quote"></p>
       <pre id="pass-bolt11" class="bolt" tabindex="0" aria-label="Lightning invoice"></pre>
       <div class="btnrow"><button onclick="copyText('pass-bolt11','pass-copy-msg')"><svg class="i"><use href="#i-copy"/></svg>Copy invoice</button>
        <a id="pass-link" class="btn" href="#"><svg class="i"><use href="#i-bolt"/></svg>Open in my Lightning wallet app</a>
        <span id="pass-copy-msg" class="msg-s"></span></div>
       <p class="inv-meta">Scan the QR with your phone's wallet, or copy the invoice
       into any Lightning app. This page updates by itself once you pay (the
       invoice is good for 1 hour).</p>
      </div>
     </div>
     <div id="pass-help" style="display:none" class="disc notice">
      <b>Payment not going through?</b> Nothing has arrived yet. Most failures
      happen inside the paying wallet, before the payment leaves it:
      <ul>
       <li>Your wallet needs <b id="pass-need"></b> available (the invoice plus
       a little for network fees).</li>
       <li><b>Cash App:</b> add money to your Cash App balance (or buy the
       bitcoin) first, then pay from that balance. Payments that pull from your
       bank often fail before they're sent. Cash App also limits Lightning
       payments — try the <b>1 signal day</b> pass first, or pay from
       <b>Strike</b> or <b>Phoenix</b>.</li>
       <li>Still stuck? Use <b>Advanced → auto-pay from a wallet the agent
       controls</b> below, or open the dashboard's <b>Help</b> panel and send
       the support report.</li>
      </ul>
     </div>
    </div>
    <details id="adv-wallet" class="adv"><summary><svg class="i chev"><use href="#i-chev"/></svg><span>Advanced: auto-pay each day
    from a Lightning wallet the agent controls, or use an API key</span></summary>
    <div class="adv-b">
     <p class="muted">The agent pays ~$10 each market day from a wallet it holds
     the keys to. Only for people who want hands-off daily payments.</p>
     <div class="navrow inline"><button onclick="makeWallet()">Create my agent's wallet for me</button>
      <span id="mw-msg" role="status" aria-live="polite"></span></div>
     <div id="fund-box" style="display:none">
      <p>Wallet ready ✓ — now give your agent some sats. Pay this invoice from any
      Lightning app (Strike, Cash App, Phoenix, Alby…):</p>
      <div class="field"><label for="fund-sats">Amount (sats) — a day pass is ~$10 of bitcoin</label>
       <div class="inrow"><input type="number" id="fund-sats" value="16000" min="1000" oninput="satsChanged()">
        <button onclick="fundInvoice()">Show invoice</button></div></div>
      <p id="fund-quote" class="muted" style="display:none;font-size:var(--fs-sm)"></p>
      <p id="fund-note" class="ok" style="display:none"></p>
      <div class="navrow inline">
       <button id="recur-btn" onclick="toggleRecurring()">Recurring day-pass: OFF</button>
       <span class="muted" style="flex:1 1 220px;font-size:var(--fs-sm)">ON = the agent buys each day's ~$10 pass from its
       wallet automatically (never above your auto-pay cap). OFF = it never
       spends without you.</span>
      </div>
      <div id="fund-qr" class="qr" style="display:none"></div>
      <pre id="bolt11" class="bolt" style="display:none" tabindex="0" aria-label="Lightning invoice"></pre>
      <p id="fund-open" style="display:none">
      <button onclick="copyInvoice()"><svg class="i"><use href="#i-copy"/></svg>Copy invoice</button> <span id="copy-msg" class="up"></span>
      <a id="fund-link" class="btn" href="#"><svg class="i"><use href="#i-bolt"/></svg>Open in my Lightning wallet app</a>
      · balance: <span id="fund-bal" class="num">0</span> sats<span id="fund-bal-at" class="muted"></span>
      <a id="fund-refresh" href="#" style="display:none" onclick="fundBalance();return false">Refresh</a>
      <span class="muted">(scan the QR with your phone's wallet, or copy the
      invoice and paste it in any Lightning app — updates automatically after
      you pay)</span></p>
      <p class="notice">Hosted wallet on demo.lnbits.com — a public TEST server
      (custodial; it can be reset). Keep only a few days of passes there, or use
      your own instance below.</p>
     </div>
     <p class="muted">Your own LNbits wallet:</p>
     <div class="field"><label for="ln-url">LNbits instance URL</label>
      <input type="text" id="ln-url" placeholder="https://…" aria-label="LNbits instance URL"></div>
     <div class="field"><label for="ln-key">LNbits wallet admin key</label>
      <input type="password" id="ln-key" placeholder="ADMIN key" aria-label="LNbits wallet admin key"></div>
     <p class="sep">— or —</p>
     <div class="field"><label for="hc-key">AgentHC Premium API key</label>
      <input type="password" id="hc-key" placeholder="Premium API key" aria-label="AgentHC Premium API key"></div>
    </div>
    </details>
   </div>
  </div></div>
 </div>
 <div id="src-url" style="display:none"><div class="sec-b">
  <div class="field"><label for="src-url-input">Feed URL</label>
  <input type="text" id="src-url-input" placeholder="https://my-feed.example.com/events"></div>
 </div></div>
 <div id="src-manual" style="display:none"><div class="sec-b"><p class="muted">You'll append JSON
  lines to <code>~/.agentic-trader/commands.jsonl</code> — see the README.</p></div></div>
 <div class="navrow"><span class="note">Your choice of source — not investment advice.</span>
  <span id="src-msg" role="status" aria-live="polite"></span><span class="sp"></span>
  <button onclick="goBack()"><svg class="i"><use href="#i-back"/></svg>Back</button>
  <button class="btn-primary" onclick="doSource()">Save &amp; continue<svg class="i"><use href="#i-arrow"/></svg></button></div>
</section>

<section id="s-broker"><h2 tabindex="-1"><span class="eyebrow">Step 4 of 7</span>Broker — where orders execute</h2>
 <div class="sec-b">
  <p class="copy">Pick your platform. Robinhood is one-click OAuth; key-based
  platforms take API keys (many offer a paper/simulated mode — the safest trial).</p>
  <p id="brk-status" class="status-line" role="status"></p>
 </div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Robinhood<p>One-click OAuth in a new tab.</p></div>
  <div class="stack">
   <div class="navrow inline"><button onclick="rhStart()">Connect Robinhood</button>
    <span id="rh-msg" role="status" aria-live="polite"></span></div>
   <div id="rh-accounts"></div>
  </div>
 </div></div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Key-based platform<p>Many offer a paper/simulated mode.</p></div>
  <div class="stack">
   <select id="brk-sel" onchange="brkFields()" aria-label="Key-based platform"></select>
   <div id="brk-fields"></div>
   <div class="navrow inline" id="brk-connect" style="display:none">
    <button class="btn-primary" onclick="brkConnect()">Connect</button> <span id="brk-msg" role="status" aria-live="polite"></span></div>
  </div>
 </div></div>
 <div class="sec-b">
  <details class="adv"><summary><svg class="i chev"><use href="#i-chev"/></svg><span>Don't see your
  platform? Your agent can build the connector</span></summary>
   <div class="adv-b"><p class="muted">Every platform needs a small adapter (different APIs). Your
   agent can WRITE one for any platform with a trading API — after setup, in the
   dashboard: <code>self-edit on</code> then
   <code>code: add a broker adapter for &lt;platform&gt; using their API</code>.
   You review the diff before it applies; it then appears here automatically.
   moomoo, Interactive Brokers, Tastytrade, Tradier, etc. all work this way.</p></div>
  </details>
 </div>
 <div class="navrow"><span class="note">Orders execute at the broker you connect here.</span><span class="sp"></span>
  <button onclick="goBack()"><svg class="i"><use href="#i-back"/></svg>Back</button>
  <button class="btn-primary" data-next onclick="goStep(cur+1)">Continue<svg class="i"><use href="#i-arrow"/></svg></button></div>
</section>

<section id="s-sizing"><h2 tabindex="-1"><span class="eyebrow">Step 5 of 7</span>Position sizing</h2>
 <div class="sec-b">
  <p class="notice">Nobody here is an investment advisor and none of this
  software can advise position sizing — this number is YOUR decision, made
  with money you can afford to lose entirely.</p>
 </div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Main book<p>The daily main pick.</p></div>
  <div class="stack">
   <div class="opts">
    <label class="check"><input type="radio" name="szmode" value="budget" checked onchange="szModeChanged('main')"><span>Fixed dollar
    amount per trade (buys what fits; skips trades that exceed it)</span></label>
    <label class="check"><input type="radio" name="szmode" value="contracts" onchange="szModeChanged('main')"><span>Fixed number of
    contracts per trade</span></label>
    <label class="check"><input type="radio" name="szmode" value="percent" onchange="szModeChanged('main')"><span>Percent of
    account per trade (re-read from your broker at each entry; skips trades that exceed it)</span></label>
   </div>
   <div class="field"><label for="sz-val" id="sz-val-lbl">Dollars per trade</label>
    <input type="number" id="sz-val" placeholder="e.g. 500" min="0" step="any"></div>
  </div>
 </div></div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Shadow book<p>"Other trades" — only used if your source includes them.</p></div>
  <div class="stack">
   <div class="opts">
    <label class="check"><input type="radio" name="szomode" value="same" checked onchange="szModeChanged('other')"><span>Same
    as the main book</span></label>
    <label class="check"><input type="radio" name="szomode" value="budget" onchange="szModeChanged('other')"><span>Fixed dollar
    amount per trade</span></label>
    <label class="check"><input type="radio" name="szomode" value="contracts" onchange="szModeChanged('other')"><span>Fixed number of
    contracts per trade</span></label>
    <label class="check"><input type="radio" name="szomode" value="percent" onchange="szModeChanged('other')"><span>Percent of
    account per trade</span></label>
   </div>
   <div class="field" id="sz-other-field" style="display:none"><label for="sz-other-val" id="sz-other-lbl">Dollars per trade</label>
    <input type="number" id="sz-other-val" placeholder="e.g. 100" min="0" step="any"></div>
  </div>
 </div></div>
 <div class="navrow"><span class="note">your choice — not advice</span><span id="sz-msg" role="status" aria-live="polite"></span><span class="sp"></span>
  <button onclick="goBack()"><svg class="i"><use href="#i-back"/></svg>Back</button>
  <button class="btn-primary" onclick="doSizing()">Save &amp; continue<svg class="i"><use href="#i-arrow"/></svg></button></div>
</section>

<section id="s-safety"><h2 tabindex="-1"><span class="eyebrow">Step 6 of 7</span>Safety rails &amp; extras</h2>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Mode<p>Dry-run logs every decision and places no orders.</p></div>
  <div class="stack">
   <label class="check"><input type="checkbox" id="sf-dry" checked onchange="sfDryChanged()"><span>Start in
   <b>dry-run</b> mode (log actions, place no orders — recommended)</span></label>
   <div class="notice" id="sf-live-warn" role="alert" style="display:none"><b>LIVE mode:</b> the agent
   will send real orders with real money to your broker — your decision, not advice.</div>
  </div>
 </div></div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Limits</div>
  <div>
   <div class="field"><label for="sf-cap">Max new entries per day</label>
    <input type="number" id="sf-cap" value="5" min="1"></div>
   <div class="field"><label for="sf-maxloss">Max daily loss in $ (optional, blank/0 = off) — once today's estimated
    realized loss reaches this, no new entries until tomorrow; exits still run</label>
    <input type="number" id="sf-maxloss" placeholder="e.g. 300" min="0"></div>
  </div>
 </div></div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Notifications<p>Any/all, optional.</p></div>
  <div>
   <div class="two">
    <div class="field"><label for="nt-discord">Discord webhook URL</label>
     <input type="text" id="nt-discord" placeholder="https://discord.com/api/webhooks/…"></div>
    <div class="field"><label for="nt-ntfy">ntfy.sh topic</label>
     <input type="text" id="nt-ntfy" placeholder="my-agent-alerts"></div>
    <div class="field"><label for="nt-tg-token">Telegram bot token</label>
     <input type="text" id="nt-tg-token" placeholder="123456:ABC…"></div>
    <div class="field"><label for="nt-tg-chat">Telegram chat id</label>
     <input type="text" id="nt-tg-chat" placeholder="e.g. 123456789"></div>
   </div></div>
 </div></div>
 <div class="sec-b"><div class="fs-grid">
  <div class="fs-lbl">Trading policy<p>Optional, veto-only.</p></div>
  <div>
   <div class="field"><label for="pol-text">My trading policy (optional — plain-English rules the agent's LLM
   checks before every entry; veto-only; uses YOUR Anthropic API key)</label>
    <textarea id="pol-text" rows="5"
     placeholder="- Never trade more than 2 new entries per day.&#10;- Skip puts.&#10;- Only tickers: SPY, QQQ, NVDA."></textarea></div>
   <div class="field"><label for="pol-key">Claude API key — only if you skipped step 2 (Connect your AI)</label>
    <input type="password" id="pol-key" aria-label="Claude API key" placeholder="sk-ant-…" autocomplete="off">
    <p class="help">Leave blank if your AI is already connected. Using another
    provider? Connect it in step 2 instead.</p></div>
  </div>
 </div></div>
 <div class="navrow"><span id="sf-msg" role="status" aria-live="polite"></span><span class="sp"></span>
  <button onclick="goBack()"><svg class="i"><use href="#i-back"/></svg>Back</button>
  <button class="btn-primary" onclick="doSafety()">Finish setup<svg class="i"><use href="#i-arrow"/></svg></button></div>
</section>

<section id="s-done"><h2 tabindex="-1"><span class="eyebrow">Step 7 of 7</span>Done — where should your agent live?</h2>
 <div class="sec-b">
  <div id="done-body"></div>
  <div id="deploy-box" class="launch">
   <div class="opt">
    <h3>Option A — this computer</h3>
    <p class="muted">One click — the agent starts here in the background and this
    page becomes its dashboard. You can close the terminal window; it keeps
    running. Keep this computer awake during market hours.</p>
    <p><label><input type="checkbox" id="autostart" checked> Also start it
    automatically when I log in to this computer, and open this dashboard
    (already signed in)</label></p>
    <p class="cta"><button class="btn-primary" onclick="startLocal()">Start my agent now</button>
    <span id="start-msg" role="status" aria-live="polite" class="muted"></span></p>
   </div>
   <div class="opt">
    <h3>Option B — let your agent create its own cloud server</h3>
    <p class="muted">~$6/mo on
    YOUR DigitalOcean account: it provisions the server, moves its completed
    setup there, and starts itself. Your token is used once and not stored.</p>
    <div class="field"><label for="do-token">DigitalOcean API token (write scope)</label>
     <input type="password" id="do-token" placeholder="DigitalOcean API token" aria-label="DigitalOcean API token" autocomplete="off"></div>
    <div class="field"><label for="do-region">Region</label>
    <div class="inrow"><select id="do-region" aria-label="Region" style="flex:1"><option>nyc3</option><option>sfo3</option><option>tor1</option>
     <option>lon1</option><option>fra1</option><option>sgp1</option></select>
     <button onclick="deploy()">Create my agent's server</button></div></div>
    <div id="deploy-msg" role="status" aria-live="polite"></div>
   </div>
  </div>
 </div>
 <div class="navrow"><span class="note">Dry-run or LIVE is what you chose in Safety — switch any time from the dashboard. Your decision, not advice.</span><span class="sp"></span>
  <button onclick="goBack()"><svg class="i"><use href="#i-back"/></svg>Back</button></div>
</section>

<div id="help" class="panel assist">
 <div class="ph"><h2>Ask your agent</h2><span class="sp"></span><span class="muted" style="font-size:var(--fs-sm)">uses the AI from step 2</span></div>
 <div class="pb">
  <p class="muted">Need help with this step? Ask your agent (works once your
  AI is connected in step 2 — any provider).</p>
  <div id="chatlog" role="log" aria-live="polite" tabindex="0" aria-label="Conversation with your agent"></div>
  <div class="chatrow">
   <input type="text" id="cmd" aria-label="Ask your agent a question" placeholder="e.g. what is dry-run?"
    onkeydown="if(event.key==='Enter')ask()">
   <button class="btn-primary" onclick="ask()">Ask</button>
  </div>
 </div>
</div>
</div>
</div>
</main>

<script>""" + _POLL_JS + """
// HTML-escape helper, hoisted so every builder can use it (loadBrokers/brkFields
// run during boot(), before any local definition would exist).
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
// Tiny, safe markdown for the agreement: escape EVERYTHING first, then add
// only headings, bold, inline code, quotes, bullets and paragraphs.
function mdLite(src){
  const inl=t=>t.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
  return esc(src||'').split(/\\n\\s*\\n/).map(block=>{
    const ls=block.split('\\n');
    if(ls.every(l=>/^\\s*[-*]\\s+/.test(l)))
      return '<ul>'+ls.map(l=>'<li>'+inl(l.replace(/^\\s*[-*]\\s+/,''))+'</li>').join('')+'</ul>';
    const h=block.match(/^(#{1,4})\\s+(.*)$/);
    if(h&&ls.length===1)return '<h'+(h[1].length+2)+'>'+inl(h[2])+'</h'+(h[1].length+2)+'>';
    if(ls.every(l=>/^&gt;/.test(l)))
      return '<blockquote>'+inl(ls.map(l=>l.replace(/^&gt;\\s?/,'')).join(' '))+'</blockquote>';
    return '<p>'+inl(ls.join(' '))+'</p>';
  }).join('');
}
const STEPS=[["s-consent","Agreement"],["s-llm","Your AI"],["s-source","Source"],
 ["s-broker","Broker"],["s-sizing","Sizing"],["s-safety","Safety"],["s-done","Launch"]];
let doneSet=new Set(), cur=0, maxUnlocked=0;
function idx(id){return STEPS.findIndex(s=>s[0]===id);}
function goStep(i){if(i>=0&&i<=maxUnlocked&&i<STEPS.length){cur=i;render();
  const m=document.querySelector('.wz-main');
  if(m&&m.getBoundingClientRect().top<0)m.scrollIntoView({block:'start'});
  focusStep();}}
// The button that had focus just went display:none with its step — move
// focus to the new step's heading so keyboard + screen-reader users follow.
function focusStep(){
  const h=document.querySelector('#'+STEPS[cur][0]+'>h2');
  if(h){try{h.focus({preventScroll:true});}catch(e){h.focus();}}
}
function sfDryChanged(){
  document.getElementById('sf-live-warn').style.display=
    document.getElementById('sf-dry').checked?'none':'';
}
function goBack(){goStep(cur-1);}
function render(){
  const bar=document.getElementById('stepbar');bar.innerHTML='';
  const mk=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;
    if(txt!=null)e.textContent=txt;return e;};
  STEPS.forEach((s,i)=>{
    const d=doneSet.has(s[0]);
    const c=mk('button','chip'+(d?' done':'')+(i===cur?' cur':'')+(i>maxUnlocked?' lock':''));
    c.type='button';
    c.appendChild(mk('span','n',d&&i!==cur?'✓':String(i+1)));
    c.appendChild(mk('span','lbl',s[1]));
    if(i===cur)c.setAttribute('aria-current','step');
    if(i<=maxUnlocked)c.onclick=()=>goStep(i);
    else{c.disabled=true;c.title='Locked — finish the earlier steps first';}
    bar.appendChild(c);
  });
  // phones: "Step 3 of 7 · Source / Next: Broker" + a 7-segment bar
  const now=mk('div','stepnow');
  now.appendChild(mk('span','','Step '+(cur+1)+' of '+STEPS.length+' · '+STEPS[cur][1]));
  if(cur+1<STEPS.length){
    const open=cur+1<=maxUnlocked;
    const nx=mk(open?'button':'span','nx'+(open?' go btn-ghost':''),'Next: '+STEPS[cur+1][1]+(open?' →':''));
    if(open){nx.type='button';nx.onclick=()=>goStep(cur+1);}
    now.appendChild(nx);
  }
  bar.appendChild(now);
  // phones: the chips are hidden, so the segments ARE the step jumper —
  // real buttons (40px hit area, focusable, labelled; disabled when locked)
  const seg=mk('div','stepseg');
  seg.setAttribute('role','group');seg.setAttribute('aria-label','Jump to step');
  STEPS.forEach((s,i)=>{
    const d=doneSet.has(s[0]);
    const g=mk('button',(i===cur?'cur':(d?'done':''))+(i<=maxUnlocked?' on':''));
    g.type='button';
    g.setAttribute('aria-label','Step '+(i+1)+': '+s[1]+(d?' (done)':'')+(i>maxUnlocked?' (locked)':''));
    g.title=(i+1)+' · '+s[1];
    if(i===cur)g.setAttribute('aria-current','step');
    if(i<=maxUnlocked)g.onclick=()=>goStep(i);else g.disabled=true;
    seg.appendChild(g);
  });
  bar.appendChild(seg);
  document.querySelectorAll('button[data-next]').forEach(b=>{b.disabled=cur+1>maxUnlocked;});
  STEPS.forEach((s,i)=>{document.getElementById(s[0]).className=(i===cur)?'active':'';});
}
function restoredNote(sec,txt){
  // '✓ already configured' line at the top of a completed tab, so returning
  // users see WHAT is set up, not just a checked chip. textContent = no
  // injection surface.
  if(!txt)return;
  const el=document.getElementById(sec);if(!el)return;
  let p=document.getElementById(sec+'-restored');
  if(!p){p=document.createElement('p');p.id=sec+'-restored';p.className='ok';
    el.insertBefore(p,el.children[1]||null);}
  // server copy says "4 signal day(s)" / "1 contract(s)": pluralise from n
  p.textContent='✓ '+String(txt).replace(/(\\d[\\d,]*)(\\s+(?:[a-z-]+\\s+)?)(day|contract)\\(s\\)/gi,
    (m,n,mid,w)=>n+mid+w+(n==='1'?'':'s'));
  if(sec==='s-llm'){const b=document.getElementById('llm-skip');
    if(b){b.textContent='Continue';b.className='btn-primary';}}
}
function done(id){
  // auto-advance only the FIRST time a step completes — re-saving an
  // already-done step must not yank the user off the tab they chose
  const first=!doneSet.has(id);
  doneSet.add(id);
  maxUnlocked=Math.max(maxUnlocked,Math.min(idx(id)+1,STEPS.length-1));
  if(first)cur=Math.min(idx(id)+1,STEPS.length-1);
  render();
  if(first&&booted)focusStep();}
let booted=false;
function unlock(id){maxUnlocked=Math.max(maxUnlocked,idx(id));render();}
async function api(p, body){
  const r = await fetch(p, body?{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}:{});
  return await r.json();
}
// Update button: never silent. A ticking "checking… 12s" while the server
// works, a hard timeout, and every failure shown in words.
let updTick=null;
function updBusy(label){
  const msg=document.getElementById('upd-msg');const t0=Date.now();
  if(updTick)clearInterval(updTick);
  const draw=()=>{const s=Math.round((Date.now()-t0)/1000);
    msg.textContent=label+'… '+s+'s'+(s>=20?' (downloading from GitHub — this can take up to a minute)':'');};
  draw();updTick=setInterval(draw,1000);
}
function updDone(html){
  if(updTick){clearInterval(updTick);updTick=null;}
  document.getElementById('upd-msg').innerHTML=html;
}
async function updCall(p,body,ms){
  const ctl=new AbortController();const to=setTimeout(()=>ctl.abort(),ms);
  try{
    const r=await fetch(p,body?{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),signal:ctl.signal}:{signal:ctl.signal});
    if(r.status===401||r.status===403)
      return {ok:false,error:'Your dashboard login expired — open the link from '+
        '`python agent.py link` (or restart the agent) and try again.'};
    try{return await r.json();}
    catch(e){return {ok:false,error:'The agent answered unexpectedly (HTTP '+r.status+'). Restart it and try again.'};}
  }catch(e){
    return {ok:false,error:(e&&e.name==='AbortError')
      ?'No answer after '+Math.round(ms/1000)+'s. Your network may be blocking GitHub — run `python agent.py netcheck` in the agent folder, or re-run the installer.'
      :'Could not reach the agent on this computer — is it still running? Restart it, then reload this page.'};
  }finally{clearTimeout(to);}
}
async function checkUpdates(){
  document.getElementById('upd-pull').style.display='none';
  updBusy('checking');
  const r=await updCall('/api/update/check?force=1',null,150000);  // pressed: fresh
  if(!r.ok){updDone('<span class="err">'+esc(r.error||'update check failed')+'</span>');return;}
  if(!r.behind){updDone('<span class="ok">up to date ✓</span>');return;}
  updTarget=r.target||'';
  updDone('<span class="ok">'+r.behind+' update'+(r.behind>1?'s':'')+
    ' available</span> — review before pulling:<pre style="white-space:pre-wrap;'+
    'max-height:12rem;overflow:auto">'+esc(r.summary||'')+'</pre>'+
    '<span class="muted">installs exactly '+esc(updTarget.slice(0,12))+
    ' from github.com/traderhc123/agentic-trader</span>');
  document.getElementById('upd-pull').style.display='';
}
let updTarget='';
async function pullUpdates(){
  updBusy('installing');
  const r=await updCall('/api/update/pull',{expect:updTarget},400000);
  updDone(r.ok?'<span class="ok">'+esc(r.note)+'</span>'
    :'<span class="err">'+esc(r.error||'update failed')+'</span>');
  if(r.ok)document.getElementById('upd-pull').style.display='none';
  if(r.ok&&r.restarting){
    // Server is re-execing onto the new code — poll until it's back,
    // then reload so the NEW UI is what's on screen.
    setTimeout(async function waitBack(){
      try{await api('/api/state');location.reload();}
      catch(e){setTimeout(waitBack,1500);}
    },2500);
  }
}
let quoteTimer=null,satsEdited=false;
async function updateQuote(){
  const el=document.getElementById('fund-quote');
  const sats=parseInt(document.getElementById('fund-sats').value||'0',10);
  if(!sats){el.style.display='none';return;}
  try{
    const r=await api('/api/wallet/quote?sats='+sats);
    if(!r.ok){el.style.display='none';return;}
    let t=sats.toLocaleString()+' sats ≈ $'+r.usd.toFixed(2)
      +' (BTC $'+Math.round(r.btc_usd).toLocaleString()+')'
      +' · covers ~'+r.day_passes+' day-pass'+(r.day_passes===1?'':'es')+' at ~$10/day';
    if(r.day_pass_sats)
      t+=' — one day pass ≈ '+r.day_pass_sats.toLocaleString()+' sats';
    if(r.invoice_cap)
      t+=' · instance max '+r.invoice_cap.toLocaleString()+' sats/invoice';
    el.textContent=t;el.style.display='';
  }catch(e){el.style.display='none';}
}
function satsChanged(){satsEdited=true;clearTimeout(quoteTimer);quoteTimer=setTimeout(updateQuote,400);}
function setRecurBtn(on){
  const b=document.getElementById('recur-btn');
  b.textContent='Recurring day-pass: '+(on?'ON':'OFF');
  b.dataset.on=on?'1':'';
}
async function toggleRecurring(){
  const on=!document.getElementById('recur-btn').dataset.on;
  const r=await api('/api/wallet/recurring',{on:on});
  if(r.ok)setRecurBtn(r.on);
}
function renderQr(m,target){
  // Server sends the QR as a 0/1 module matrix; draw it as one SVG path.
  // No image bytes, no external assets — and the lightning: link stays as a
  // fallback for desktops that DO have a handler registered.
  const box=document.getElementById(target||'fund-qr');
  if(!m||!m.length){box.style.display='none';return;}
  const n=m.length;let d='';
  for(let y=0;y<n;y++)for(let x=0;x<m[y].length;x++)if(m[y][x])d+='M'+x+' '+y+'h1v1h-1z';
  box.innerHTML='<svg viewBox="0 0 '+n+' '+n+'" width="230" height="230"'
    +' shape-rendering="crispEdges" style="background:#fff;border-radius:6px">'
    +'<path d="'+d+'" fill="#000"/></svg>';
  box.style.display='';
}
async function copyInvoice(){
  const t=document.getElementById('bolt11').textContent;
  const msg=document.getElementById('copy-msg');
  try{await navigator.clipboard.writeText(t);msg.textContent='copied ✓';}
  catch(e){msg.textContent='clipboard blocked — select the invoice text above and copy it';}
}
async function copyText(srcId,msgId){
  const t=document.getElementById(srcId).textContent;
  const msg=document.getElementById(msgId);
  try{await navigator.clipboard.writeText(t);msg.textContent='copied ✓';}
  catch(e){msg.textContent='clipboard blocked — select the invoice text above and copy it';}
}
let passTimer=null,passHelpTimer=null,passBusy=false;
async function buyPass(days){
  // one invoice per click: a double-click must not ask for two
  if(passBusy)return;passBusy=true;
  try{await buyPassNow(days);}finally{passBusy=false;}
}
async function buyPassNow(days){
  document.querySelectorAll('button.pass').forEach(b=>b.classList.toggle('sel',b.dataset.days===String(days)));
  const st=document.getElementById('pass-status');st.textContent='creating invoice…';
  const r=await api('/api/pass/buy',{days:days});
  if(!r.ok){st.innerHTML='<span class="err">'+esc(r.error)+'</span>';
    if(r.refresh_sizes){document.querySelectorAll('button.pass').forEach(b=>b.classList.remove('sel'));passSizes();}
    if(r.unavailable)document.getElementById('adv-wallet').open=true;return;}
  document.getElementById('pass-box').style.display='';
  document.getElementById('pass-quote').textContent=days+' signal day'+(days>1?'s':'')+' — '+
    r.amount_sats.toLocaleString()+' sats'+(r.amount_usd?' (~$'+r.amount_usd+')':'')+
    (r.carried?' · plus '+r.carried+' unused day'+(Number(r.carried)===1?'':'s')+' carried over':'');
  renderQr(r.qr,'pass-qr');
  const need=Math.ceil((r.amount_sats||0)*1.01)+50;
  document.getElementById('pass-need').textContent=need.toLocaleString()+' sats';
  document.getElementById('pass-help').style.display='none';
  if(passHelpTimer)clearTimeout(passHelpTimer);
  passHelpTimer=setTimeout(()=>{if(passTimer)document.getElementById('pass-help').style.display='';},120000);
  document.getElementById('pass-bolt11').textContent=r.bolt11;
  document.getElementById('pass-link').href='lightning:'+r.bolt11;
  document.getElementById('pass-copy-msg').textContent='';
  st.textContent='waiting for payment…';
  if(passTimer)passTimer();
  // every 3 s for 2 minutes, then every 15 s; paused while the page is hidden
  passTimer=poll(checkPass,ms=>ms<120000?3000:15000,{immediate:false});
}
async function checkPass(){
  const r=await api('/api/pass/status');
  const st=document.getElementById('pass-status');
  if(r.status==='active'){
    if(passTimer){passTimer();passTimer=null;}
    document.getElementById('pass-box').style.display='none';
    {const n=Number(r.days_left);
    st.innerHTML='<span class="ok">✓ Access active — '+(Number.isFinite(n)&&r.days_left!=null?n+' signal day'+(n===1?'':'s'):'? signal days')+
      ' left</span>';}
  }else if(r.status==='pending'){st.textContent='waiting for payment…';}
  else if(r.status==='unpaid'){
    if(passTimer){passTimer();passTimer=null;}
    document.getElementById('pass-box').style.display='none';
    st.innerHTML='<span class="err">That invoice expired unpaid — pick a pass again.</span>';
  }else if(r.status==='exits_only'){st.innerHTML='<span class="err">Your signal days are used up — exits still come through for a few days. Buy more above.</span>';}
  else if(r.status==='error'&&r.error){st.innerHTML='<span class="err">'+esc(r.error)+'</span>';}
  else if(r.status==='expired'||r.status==='used_up'){st.innerHTML='<span class="err">Your pass has run out — buy more days above.</span>';}
}
function srcChanged(){
  const v=document.getElementById('src').value;
  for(const k of ['agenthc','url','manual'])
    document.getElementById('src-'+k).style.display = (k===v)?'':'none';
}
// Show only the pass sizes the feed server sells right now (it can take
// sizes off sale, e.g. while its node can't receive that large a payment).
// If the server can't be asked, every card stays visible. An empty list
// means every size is paused. Re-checked every 15 minutes while the page is
// visible (and when it is shown again) so a size comes back on its own once
// the node has refilled.
async function passSizes(){
  try{const r=await api('/api/pass/options');
    if(!Array.isArray(r.options))return;
    document.querySelectorAll('.pass[data-days]').forEach(b=>{
      b.style.display=r.options.indexOf(+b.dataset.days)>=0?'':'none';});
    const n=document.getElementById('pass-sizes-note');
    if(n)n.textContent=r.options.length?'':'Passes are paused for a little while — '+
      'our node is refilling so it can receive the payment. This page checks again every 15 minutes while it is open.';
  }catch(e){}
}
async function boot(){
  poll(passSizes,900000);  // at once, then every 15 min while visible
  const st=await api('/api/state');
  document.getElementById('disclaimer').innerHTML=mdLite(st.disclaimer);
  const notes=st.notes||{};
  if(st.consent){done('s-consent');restoredNote('s-consent',notes.consent||'Agreement signed — on file');}
  if(st.llm){done('s-llm');restoredNote('s-llm',notes.llm||'AI connected');
    // Key on file: leave the box blank and press Find models to switch model.
    document.getElementById('llm-key').placeholder='saved key on file — leave blank and press Find models to change model';}
  if(st.source){done('s-source');restoredNote('s-source',notes.source||('Source saved: '+st.source));
    const sel=document.getElementById('src');sel.value=st.source;srcChanged();
    document.getElementById('hc-scope').value=st.include_other_trades?'all':'main';}
  if(st.wallet&&st.wallet.connected){showWallet(st.wallet.page,'wallet connected ✓');
    document.getElementById('adv-wallet').open=true;}
  if(st.pass)checkPass();
  setRecurBtn(!!st.recurring);
  if(st.broker){done('s-broker');
    // numeric = Robinhood account last4; otherwise a key-based broker id
    // (status goes in the step's own line, not beside the Robinhood button)
    const t=/^\\d+$/.test(st.broker)?'Robinhood account ····'+st.broker:st.broker;
    restoredNote('s-broker','Broker connected: '+t);}
  if(st.sizing){done('s-sizing');restoredNote('s-sizing',notes.sizing||'Sizing saved');
    restoreSizing(st.sizing_detail);}
  if(st.safety){done('s-safety');restoredNote('s-safety',notes.safety||'Safety saved');}
  // Safety shows the mode and limits as they are set now, never its own
  // defaults: re-saving the step must not switch the mode or reset the cap.
  SF={dry:(typeof st.dry_run==='boolean')?st.dry_run:null,
      cap:st.max_entries_per_day==null?null:String(st.max_entries_per_day),
      loss:st.max_daily_loss_usd>0?String(st.max_daily_loss_usd):''};
  if(SF.dry!==null){document.getElementById('sf-dry').checked=SF.dry;sfDryChanged();}
  if(SF.cap!==null)document.getElementById('sf-cap').value=SF.cap;
  if(SF.loss)document.getElementById('sf-maxloss').value=SF.loss;
  cur=Math.min(maxUnlocked,STEPS.length-1);render();booted=true;
  loadBrokers();
}
async function doConsent(){
  const rd=document.getElementById('c-read'),ph=document.getElementById('c-phrase');
  // tie the error to the field it is about (screen readers read it on focus)
  const mark=(el,bad)=>{if(bad){el.setAttribute('aria-invalid','true');el.setAttribute('aria-describedby','c-msg');}
    else{el.removeAttribute('aria-invalid');el.removeAttribute('aria-describedby');}};
  if(!rd.checked){mark(rd,true);mark(ph,false);
    document.getElementById('c-msg').innerHTML='<span class="err">check the box after reading</span>';rd.focus();return;}
  mark(rd,false);
  const r=await api('/api/consent',{phrase:ph.value});
  mark(ph,!r.ok);
  document.getElementById('c-msg').innerHTML=r.ok?'<span class="ok">accepted ✓</span>'
    :'<span class="err">'+esc(r.error)+'</span>';
  if(!r.ok)ph.focus();
  if(r.ok)done('s-consent');
}
let llmBusy=false, LLMP={};
async function llmLoadProviders(){
  const r=await api('/api/llm/providers');LLMP=r.providers||{};
  const sel=document.getElementById('llm-provider');
  sel.innerHTML='<option value="">— detect from key —</option>'+
    Object.entries(LLMP).map(([id,p])=>'<option value="'+id+'">'+esc(p.name)+'</option>').join('');
}
function llmProviderChanged(){
  const id=document.getElementById('llm-provider').value,p=LLMP[id];
  if(p){document.getElementById('llm-base').value=(id==='custom'||id==='ollama')?p.base_url:'';
    if(p.keys_url)document.getElementById('llm-keys-link').href=p.keys_url;}
}
function llmKeyTyped(){
  // A pasted key arrives whole — look up its models without another click.
  const v=document.getElementById('llm-key').value.trim();
  if(!llmBusy&&v.length>=30)llmFind();
}
async function llmFind(){
  if(llmBusy)return;llmBusy=true;
  const msg=document.getElementById('llm-find-msg');msg.textContent=' checking key…';
  try{
    const r=await api('/api/llm/models',{key:document.getElementById('llm-key').value,
      provider:document.getElementById('llm-provider').value,
      base_url:document.getElementById('llm-base').value});
    if(!r.ok){msg.innerHTML=' <span class="err">'+esc(r.error)+'</span>';
      if(r.need_provider)document.getElementById('llm-adv').open=true;return;}
    document.getElementById('llm-provider').value=r.provider;
    msg.innerHTML=' <span class="ok">'+esc(LLMP[r.provider]?LLMP[r.provider].name:r.provider)+
      ' ✓ — '+r.models.length+' models</span>';
    document.getElementById('llm-models').innerHTML=r.models.map(m=>'<option value="'+esc(m)+'">').join('');
    document.getElementById('llm-model').value=r.suggested||'';
    document.getElementById('llm-pick').style.display='';
    document.getElementById('llm-connect').style.display='';
    // one primary per footer: Connect & continue takes over from Continue
    const sk=document.getElementById('llm-skip');
    if(sk&&sk.className==='btn-primary')sk.className='back';
  }finally{llmBusy=false;}
}
async function doLLM(){
  if(llmBusy)return;llmBusy=true;
  const msg=document.getElementById('llm-msg');msg.textContent=' testing the model…';
  try{
    const r=await api('/api/llm',{key:document.getElementById('llm-key').value,
      provider:document.getElementById('llm-provider').value,
      base_url:document.getElementById('llm-base').value,
      model:document.getElementById('llm-model').value});
    msg.innerHTML=r.ok?' <span class="ok">connected ✓ '+esc(r.describe||'')+'</span>'
      :' <span class="err">'+esc(r.error)+'</span>';
    if(r.ok){document.getElementById('llm-key').value='';done('s-llm');}
  }finally{llmBusy=false;}
}
async function skipLLM(){done('s-llm');}
let walletMade=false, balTimer=null;
function showWallet(page,label){
  walletMade=true;
  const mw=document.getElementById('mw-msg');
  mw.innerHTML=' <span class="ok">'+label+'</span>';
  if(page){
    const a=document.createElement('a');
    a.href=page;a.target='_blank';a.rel='noopener';
    a.textContent=' open wallet page ↗';
    mw.appendChild(a);
  }
  document.getElementById('fund-box').style.display='';
  // Price-aware default: ONE ~$10 day pass at the CURRENT BTC price
  // (clamped to the instance's per-invoice cap when known) — unless the
  // user already typed an amount.
  api('/api/wallet/quote?sats=0').then(function(r){
    if(r&&r.ok&&r.suggested_sats&&!satsEdited)
      document.getElementById('fund-sats').value=r.suggested_sats;
    updateQuote();
  }).catch(function(){});
  // No invoice generated yet this session — show balance, hide the pay link
  // until "Show invoice" creates a fresh bolt11.
  document.getElementById('fund-link').style.display='none';
  api('/api/wallet/balance').then(function(b){
    if(b&&b.ok){
      document.getElementById('fund-bal').textContent=b.sats.toLocaleString();
      document.getElementById('fund-open').style.display='';
    }
  }).catch(function(){});
}
let walletBusy=false;
async function makeWallet(replace){
  // The agent already has a wallet: replace it only on a clear yes.
  const ASK='Your agent already has a wallet. Replace it with a new, empty one?'
    +' Sats in the current wallet are NOT moved to the new one. The current'
    +" wallet's details are saved to a file in the agent's folder first, so"
    +' you can still open it and move them yourself. If you only want to add'
    +' sats, press Cancel. OK = replace the wallet. Cancel = keep it.';
  if(walletBusy)return;
  const mw=document.getElementById('mw-msg');
  // this page already knows a wallet is connected: ask before anything is sent
  if(walletMade&&replace!==true){if(!confirm(ASK))return;replace=true;}
  walletBusy=true;
  mw.textContent=' creating…';
  let r;
  try{r=await api('/api/wallet/create',replace===true?{replace:true}:{});}
  catch(e){r={ok:false,error:'could not reach the setup server'};}
  walletBusy=false;
  if(!r.ok&&replace!==true){
    // refused because the agent already has a wallet (this page didn't
    // know)? Then ask, and only replace it on a yes.
    const refused=!!(r.exists||r.needs_replace);
    let has=refused;
    if(!has){try{const s=await api('/api/state');has=!!(s.wallet&&s.wallet.connected);
      if(has)showWallet(s.wallet.page,'wallet connected ✓');}catch(e){}}
    if(has){
      if(confirm(ASK))return makeWallet(true);
      if(refused)mw.textContent=' Kept your current wallet — nothing changed.';
      return;}}
  if(r.ok){showWallet(r.page,'created ✓');
    if(r.note)mw.innerHTML+='<div class="muted" style="margin-top:.5rem">'+esc(r.note)+'</div>';
    if(r.warning)mw.innerHTML+=
      '<div class="err" style="margin-top:.5rem">⚠️ '+esc(r.warning)+'</div>';}
  else mw.innerHTML=' <span class="err">'+esc(r.error)+'</span>';
}
async function fundInvoice(){
  const sats=document.getElementById('fund-sats').value;
  const r=await api('/api/wallet/fund',{sats:sats});
  if(!r.ok){document.getElementById('mw-msg').innerHTML=' <span class="err">'+esc(r.error)+'</span>';return;}
  const pre=document.getElementById('bolt11');pre.style.display='';pre.textContent=r.bolt11;
  renderQr(r.qr);
  const fn=document.getElementById('fund-note');
  if(r.note){fn.textContent=r.note;fn.style.display='';}
  else fn.style.display='none';
  document.getElementById('copy-msg').textContent='';
  document.getElementById('fund-open').style.display='';
  const fl=document.getElementById('fund-link');
  fl.href='lightning:'+r.bolt11;fl.style.display='';
  if(balTimer)balTimer();
  // every 4 s until the balance changes or 10 minutes pass (paused while the
  // page is hidden), then a Refresh link
  let start=null,now=null;
  document.getElementById('fund-refresh').style.display='none';
  balTimer=poll(async()=>{now=await fundBalance();if(start===null)start=now;},
    ms=>ms<600000?4000:null,
    {immediate:false,stopWhen:()=>now!==null&&now!==start,
     onStop:()=>{balTimer=null;document.getElementById('fund-refresh').style.display='';}});
}
async function fundBalance(){
  const b=await api('/api/wallet/balance');
  if(!b.ok)return null;
  document.getElementById('fund-bal').textContent=b.sats.toLocaleString();
  document.getElementById('fund-bal-at').textContent=' (as of '+
    new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})+')';
  return b.sats;
}
async function doSource(){
  const v=document.getElementById('src').value;
  const r=await api('/api/source',{source:v,
    scope:document.getElementById('hc-scope').value,
    lnbits_url:document.getElementById('ln-url').value,
    lnbits_key:document.getElementById('ln-key').value,
    agenthc_key:document.getElementById('hc-key').value,
    source_url:document.getElementById('src-url-input').value});
  document.getElementById('src-msg').innerHTML=r.ok?'<span class="ok">'+esc(r.note)+'</span>'
    :'<span class="err">'+esc(r.error)+'</span>';
  if(r.ok)done('s-source');
}
llmLoadProviders();
let BRK=[];
async function loadBrokers(){
  const r=await api('/api/brokers');BRK=r.brokers||[];
  const sel=document.getElementById('brk-sel');
  sel.innerHTML='<option value="">— choose a key-based platform —</option>'+
    BRK.map(b=>'<option value="'+b.id+'">'+esc(b.name)+'</option>').join('');
  brkFields();
}
function brkFields(){
  const id=document.getElementById('brk-sel').value;
  const b=BRK.find(x=>x.id===id);const box=document.getElementById('brk-fields');
  document.getElementById('brk-connect').style.display=b?'':'none';
  if(!b){box.innerHTML='';return;}
  box.innerHTML=b.fields.map(f=>{
    if(f.type==='checkbox')return '<label class="check"><input type="checkbox" id="bf-'+f.id+'"'+
      (f.default?' checked':'')+'><span>'+esc(f.label)+'</span></label>';
    return '<div class="field"><label for="bf-'+f.id+'">'+esc(f.label)+'</label><input type="'+
      (f.type==='password'?'password':'text')+
      '" id="bf-'+f.id+'" placeholder="'+esc(f.label)+'" autocomplete="off"></div>';
  }).join('');
}
async function brkConnect(){
  const id=document.getElementById('brk-sel').value;
  const b=BRK.find(x=>x.id===id);if(!b)return;
  const vals={};b.fields.forEach(f=>{const el=document.getElementById('bf-'+f.id);
    vals[f.id]=(f.type==='checkbox')?el.checked:el.value;});
  const r=await api('/api/broker/connect',{broker:id,values:vals});
  document.getElementById('brk-msg').innerHTML=r.ok?' <span class="ok">'+esc(r.note)+'</span>'
    :' <span class="err">'+esc(r.error)+'</span>';
  if(r.ok){document.getElementById('brk-status').innerHTML='<span class="ok">Connected: '+
    esc(b.name||id)+' ✓</span>';done('s-broker');}
}
async function rhStart(){
  const r=await api('/api/rh/start');
  if(r.url){document.getElementById('rh-msg').textContent='waiting for approval…';
    window.open(r.url,'_blank');poll();}
  else document.getElementById('rh-msg').innerHTML='<span class="err">'+esc(r.error)+'</span>';
}
async function poll(){
  const st=await api('/api/state');
  if(st.broker){
    document.getElementById('rh-msg').textContent='';
    document.getElementById('brk-status').innerHTML='<span class="ok">Connected: Robinhood account ····'+
      esc(st.broker)+' ✓</span>';
    if(st.rh_warning)document.getElementById('rh-accounts').innerHTML=
      '<p class="err">'+esc(st.rh_warning)+'</p>';
    done('s-broker');}
  else setTimeout(poll,1500);
}
// Sizing form: each book (main / shadow "other trades") picks its own mode.
// The label + placeholder follow the mode so the number's unit is obvious.
const SZ_UNITS={budget:['Dollars per trade','e.g. 500','1'],
  contracts:['Contracts per trade','e.g. 1','1'],
  percent:['Percent of account per trade','e.g. 2','0.1']};
function szModeChanged(book){
  const main=book==='main';
  const sel=document.querySelector('input[name='+(main?'szmode':'szomode')+']:checked');
  const mode=sel?sel.value:(main?'budget':'same');
  if(!main)document.getElementById('sz-other-field').style.display=mode==='same'?'none':'';
  const u=SZ_UNITS[mode];if(!u)return;
  document.getElementById(main?'sz-val-lbl':'sz-other-lbl').textContent=u[0];
  const inp=document.getElementById(main?'sz-val':'sz-other-val');
  inp.placeholder=u[1];inp.step=mode==='contracts'?'1':'any';
  inp.max=mode==='percent'?'100':'';
}
// Pre-fill the sizing form from the saved config (structured, from
// /api/state) so re-saving never silently clears a book's size. Nothing on
// file = un-check the main modes rather than claim a choice never made.
function restoreSizing(sz){
  const rb=(n,v)=>document.querySelector('input[name='+n+'][value='+v+']');
  if(!sz||!sz.main||!rb('szmode',sz.main.mode)){
    document.querySelectorAll('input[name=szmode]').forEach(e=>{e.checked=false;});return;}
  rb('szmode',sz.main.mode).checked=true;
  document.getElementById('sz-val').value=sz.main.value;
  const o=sz.other&&rb('szomode',sz.other.mode)?sz.other:{mode:'same',value:''};
  rb('szomode',o.mode).checked=true;
  document.getElementById('sz-other-val').value=o.value;
  szModeChanged('main');szModeChanged('other');
}
async function doSizing(){
  const sel=document.querySelector('input[name=szmode]:checked');
  if(!sel){document.getElementById('sz-msg').innerHTML='<span class="err">choose how the main book is sized</span>';return;}
  const osel=document.querySelector('input[name=szomode]:checked');
  const r=await api('/api/sizing',{main:{mode:sel.value,value:document.getElementById('sz-val').value},
    other:{mode:osel?osel.value:'same',value:document.getElementById('sz-other-val').value}});
  document.getElementById('sz-msg').innerHTML=r.ok?'<span class="ok">saved ✓</span>'
    :'<span class="err">'+esc(r.error)+'</span>';
  if(r.ok)done('s-sizing');
}
let SF={dry:null,cap:null,loss:''};
async function doSafety(){
  const dry=document.getElementById('sf-dry').checked,cap=document.getElementById('sf-cap').value;
  let loss=document.getElementById('sf-maxloss').value;
  if(!String(loss).trim()&&SF.loss)loss='0';  // you cleared a limit that was set: off
  const body={};
  // mode and cap are sent only when you changed them here (or never set them)
  if(SF.dry===null||dry!==SF.dry)body.dry=dry;
  if(SF.cap===null||String(cap)!==SF.cap)body.cap=cap;
  const r=await api('/api/safety',{...body,seeded:booted,
    max_loss:loss,
    discord:document.getElementById('nt-discord').value,
    ntfy:document.getElementById('nt-ntfy').value,
    tg_token:document.getElementById('nt-tg-token').value,
    tg_chat:document.getElementById('nt-tg-chat').value,
    policy:document.getElementById('pol-text').value,
    anthropic_key:document.getElementById('pol-key').value});
  if(r.ok){done('s-safety');
    SF={dry:dry,cap:String(cap),loss:(Number(loss)>0)?String(loss):''};
    document.getElementById('done-body').innerHTML=
     '<p class="ok">Setup complete — pick where your agent should live.</p>'+
     '<details><summary class="muted">…or start it from a terminal instead</summary>'+
     '<pre>'+esc(r.next)+'</pre></details>';}
  else document.getElementById('sf-msg').innerHTML='<span class="err">'+esc(r.error)+'</span>';
}
async function startLocal(){
  const el=document.getElementById('start-msg');
  el.textContent=' starting…';
  let r;
  try{r=await api('/api/start-local',{autostart:document.getElementById('autostart').checked});}
  catch(e){el.innerHTML=' <span class="err">could not reach setup server</span>';return;}
  if(!r.ok){el.innerHTML=' <span class="err">'+esc(r.error)+'</span>';return;}
  el.textContent=' starting… (the agent takes over this address in a few seconds)';
  // The wizard hands its port to the running agent; poll until the agent's
  // dashboard answers with a real mode, then hop over to it.
  const hop=setInterval(async()=>{
    try{const s=await fetch('/api/status');
      if(s.ok){const j=await s.json();
        if(j.mode&&j.mode!=='OFFLINE'){clearInterval(hop);location='/dash';}}}
    catch(e){}
  },1500);
}
async function deploy(){
  const el=document.getElementById('deploy-msg');
  el.innerHTML='creating server… (takes 1–3 minutes)';
  const r=await api('/api/deploy',{token:document.getElementById('do-token').value,
    region:document.getElementById('do-region').value});
  if(!r.ok){el.innerHTML='<span class="err">'+esc(r.error)+'</span>';return;}
  const here=r.here?'<br><b>'+esc(r.here)+'</b>':'';
  const poll2=async()=>{
    let s;
    try{s=await api('/api/deploy/status');}
    catch(e){el.innerHTML='Server created. The agent on this computer has stopped, '
      +'so find the server address under Droplets at cloud.digitalocean.com.'+here;return;}
    if(s.ip){el.innerHTML='<span class="ok">Server created at '+esc(s.ip)
      +' ✓</span><br><span class="muted">It is installing the agent now (about 5 '
      +'minutes); you get the "heartbeat started" notification when it is running. '
      +'No notification after 15 minutes? Log in with the command below and run: '
      +'sudo tail -50 /var/log/cloud-init-output.log. Dashboard from here: '
      +esc(s.ssh||('ssh -L 8721:127.0.0.1:8721 trader@'+s.ip))
      +' then open http://127.0.0.1:8721/dash</span>'+here;
      api('/api/finish',{});}
    else{el.innerHTML='server status: '+esc(s.status)+' — waiting…'+here;setTimeout(poll2,5000);}
  };
  poll2();
}
async function ask(){
  const box=document.getElementById('cmd');const q=box.value.trim();if(!q)return;
  box.value='';const log=document.getElementById('chatlog');
  const bub=(cls,t)=>{log.insertAdjacentHTML('beforeend','<div class="msg '+cls+'"><div class="who">'+
    (cls==='me'?'You':'Agent')+'</div><div class="body">'+esc(t)+'</div></div>');log.scrollTop=log.scrollHeight;};
  bub('me',q);
  const r=await api('/api/ask',{question:q});
  bub('agent',r.reply);
}
boot();
</script>
<script>
/* Ambient background: a slowly drifting 3D node network, rendered in raw WebGL
   (no library, no network, no external script) so it stays self-contained and
   works offline. Degrades silently to the flat dark background if WebGL is
   unavailable, and holds a single static frame when the OS asks to reduce
   motion. Purely decorative — aria-hidden, never blocks the wizard. */
(function(){
  var c=document.getElementById('bg'); if(!c||!window.WebGLRenderingContext) return;
  if(getComputedStyle(c).display==='none') return;  // hidden in the current design: skip the GPU work
  var gl=null; try{ gl=c.getContext('webgl',{alpha:true,antialias:true,premultipliedAlpha:false}); }catch(e){}
  if(!gl) return;
  // Accessibility: honour reduced-motion by skipping the animation entirely —
  // the static CSS glow behind the page is the background in that case.
  if(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  function sh(t,src){var s=gl.createShader(t);gl.shaderSource(s,src);gl.compileShader(s);return s;}
  var vs=sh(gl.VERTEX_SHADER,
    'attribute vec2 a_pos;attribute float a_size;attribute float a_alpha;varying float v_a;'+
    'void main(){v_a=a_alpha;gl_Position=vec4(a_pos,0.0,1.0);gl_PointSize=a_size;}');
  var fs=sh(gl.FRAGMENT_SHADER,
    /* rgb=u_col (not u_col*a): additive blend already scales by SRC_ALPHA, so
       multiplying here too would darken to alpha^2 and vanish. */
    'precision mediump float;varying float v_a;uniform int u_point;uniform vec3 u_col;'+
    'void main(){if(u_point==1){vec2 d=gl_PointCoord-0.5;float r=length(d);if(r>0.5)discard;'+
    'float a=v_a*(1.0-smoothstep(0.05,0.5,r));gl_FragColor=vec4(u_col,a);}'+
    'else{gl_FragColor=vec4(u_col,v_a);}}');
  var p=gl.createProgram();gl.attachShader(p,vs);gl.attachShader(p,fs);gl.linkProgram(p);
  if(!gl.getProgramParameter(p,gl.LINK_STATUS)) return;
  gl.useProgram(p);
  var aPos=gl.getAttribLocation(p,'a_pos'),aSize=gl.getAttribLocation(p,'a_size'),
      aAlpha=gl.getAttribLocation(p,'a_alpha'),uPoint=gl.getUniformLocation(p,'u_point'),
      uCol=gl.getUniformLocation(p,'u_col');
  gl.uniform3f(uCol,0.36,0.56,1.0);
  var buf=gl.createBuffer();
  gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE);gl.clearColor(0,0,0,0);
  var DPR=Math.min(window.devicePixelRatio||1,2),W=0,H=0;
  function resize(){W=c.clientWidth;H=c.clientHeight;
    c.width=Math.max(1,Math.floor(W*DPR));c.height=Math.max(1,Math.floor(H*DPR));
    gl.viewport(0,0,c.width,c.height);}
  var N=58,nodes=[];
  for(var i=0;i<N;i++)nodes.push({x:Math.random()*2-1,y:Math.random()*2-1,z:Math.random()*2-1,
    vx:(Math.random()*2-1)*6e-4,vy:(Math.random()*2-1)*6e-4,vz:(Math.random()*2-1)*6e-4});
  var pts=new Float32Array(N*4),lines=new Float32Array(N*N*8),ang=0;
  var sx=new Float32Array(N),sy=new Float32Array(N),sd=new Float32Array(N);
  function frame(){
    ang+=0.0015;var ca=Math.cos(ang),sa=Math.sin(ang),asp=W/Math.max(H,1);
    for(var i=0;i<N;i++){var n=nodes[i];
      n.x+=n.vx;n.y+=n.vy;n.z+=n.vz;
      if(n.x>1)n.x-=2;else if(n.x<-1)n.x+=2; if(n.y>1)n.y-=2;else if(n.y<-1)n.y+=2;
      if(n.z>1)n.z-=2;else if(n.z<-1)n.z+=2;
      var xr=n.x*ca-n.z*sa,zr=n.x*sa+n.z*ca,pp=1.0/(2.4-zr);
      sx[i]=(xr*pp)/(asp>1?asp:1)*1.15; sy[i]=(n.y*pp)*(asp<1?asp:1)*1.15; sd[i]=pp;}
    for(var i=0;i<N;i++){pts[i*4]=sx[i];pts[i*4+1]=sy[i];
      pts[i*4+2]=Math.max(2.2,sd[i]*5.0*DPR);pts[i*4+3]=0.22+sd[i]*0.45;}
    var li=0,TH=0.27;
    for(var i=0;i<N;i++)for(var j=i+1;j<N;j++){
      var dx=sx[i]-sx[j],dy=sy[i]-sy[j],d=Math.sqrt(dx*dx+dy*dy);
      if(d<TH){var a=(1.0-d/TH)*0.42*Math.min(sd[i],sd[j]);
        lines[li++]=sx[i];lines[li++]=sy[i];lines[li++]=0;lines[li++]=a;
        lines[li++]=sx[j];lines[li++]=sy[j];lines[li++]=0;lines[li++]=a;}}
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf);
    gl.enableVertexAttribArray(aPos);gl.enableVertexAttribArray(aSize);gl.enableVertexAttribArray(aAlpha);
    gl.bufferData(gl.ARRAY_BUFFER,lines.subarray(0,li),gl.DYNAMIC_DRAW);
    gl.vertexAttribPointer(aPos,2,gl.FLOAT,false,16,0);gl.vertexAttribPointer(aSize,1,gl.FLOAT,false,16,8);
    gl.vertexAttribPointer(aAlpha,1,gl.FLOAT,false,16,12);gl.uniform1i(uPoint,0);
    gl.drawArrays(gl.LINES,0,li/4);
    gl.bufferData(gl.ARRAY_BUFFER,pts,gl.DYNAMIC_DRAW);
    gl.vertexAttribPointer(aPos,2,gl.FLOAT,false,16,0);gl.vertexAttribPointer(aSize,1,gl.FLOAT,false,16,8);
    gl.vertexAttribPointer(aAlpha,1,gl.FLOAT,false,16,12);gl.uniform1i(uPoint,1);
    gl.drawArrays(gl.POINTS,0,N);
    requestAnimationFrame(frame);
  }
  window.addEventListener('resize',resize);
  resize();requestAnimationFrame(frame);
})();
</script></body></html>"""

_DASH_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentic-trader</title>""" + _STYLE + """
<style>
 .wrap{max-width:1360px;margin:0 auto;padding:var(--s5) 24px var(--s8)}
 .wrap>*,.grid>*,.col>*{min-width:0}

 /* mode banner */
 .modebar{display:flex;align-items:center;gap:var(--s3);padding:9px var(--s4);margin-bottom:var(--s4);
   border:1px solid var(--accent-line);background:linear-gradient(90deg,var(--accent-soft),transparent 70%);
   border-radius:var(--r-lg);font-size:var(--fs);color:var(--fg-2)}
 .modebar>svg{color:var(--accent);width:16px;height:16px}
 .modebar b{color:var(--accent);font-weight:600}
 .modebar .mb-txt{flex:1;min-width:0;display:flex;justify-content:space-between;gap:4px var(--s4);flex-wrap:wrap}
 .modebar.live{border-color:var(--live-line);background:linear-gradient(90deg,var(--live-soft),transparent 70%)}
 .modebar.live>svg,.modebar.live b{color:var(--live)}
 .modebar.paused{border-color:var(--warn-line);background:linear-gradient(90deg,var(--warn-soft),transparent 70%)}
 .modebar.paused>svg,.modebar.paused b.pz{color:var(--warn)}
 /* LIVE stays red even while paused — amber only on the "Paused" word */
 .modebar.live.paused{border-color:var(--live-line);background:linear-gradient(90deg,var(--live-soft),transparent 70%)}
 .modebar.live.paused>svg{color:var(--live)}
 .modebar.off{border-color:rgba(240,96,93,.45);background:linear-gradient(90deg,var(--down-soft),transparent 70%)}
 .modebar.off>svg,.modebar.off b{color:var(--down)}
 .modebar.off .mb-txt>.muted{display:none}

 /* KPI strip */
 .kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--line);border-radius:var(--r-lg);
   background:var(--surface);margin-bottom:var(--s4);overflow:hidden}
 .kpi{padding:14px var(--s4);border-left:1px solid var(--line);min-width:0}
 .kpi:first-child{border-left:0}
 .kpi .k{font:600 var(--fs-xs)/1 var(--font);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
 .kpi .v{font:600 var(--fs-xl)/1 var(--mono);letter-spacing:-.02em;margin-top:10px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .kpi .v small{font:500 13px/1 var(--font);letter-spacing:0;color:var(--muted)}
 .kpi .v.txt{font-family:var(--font);letter-spacing:-.01em;font-size:var(--fs-xl);line-height:1.1;white-space:normal;overflow-wrap:anywhere}
 #t-sizing{white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;line-height:1.1}
 #t-sizing-s{white-space:normal;overflow-wrap:anywhere}
 .kpi .s{font-size:var(--fs-sm);color:var(--muted);margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .kpi.bad{box-shadow:inset 0 2px 0 var(--down)} .kpi.bad .v{color:var(--down)}
 .meter{height:3px;border-radius:2px;background:var(--line-2);margin-top:10px;overflow:hidden}
 .meter i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
 .meter.hot i{background:var(--warn)}
 .ok-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--up);margin-right:8px;vertical-align:2px}
 .off-dot{display:inline-block;width:7px;height:7px;border-radius:50%;border:1px solid var(--faint);margin-right:8px;vertical-align:2px}

 .grid{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:var(--s4);align-items:start}
 .col{display:flex;flex-direction:column;gap:var(--s4);min-width:0}

 /* proposal */
 .panel.prop{border-color:var(--warn-line)}
 .panel.prop .ph{background:var(--warn-soft);border-bottom-color:var(--warn-line);border-radius:var(--r-lg) var(--r-lg) 0 0}
 .panel.prop .ph h2{color:var(--warn)}
 #prop-expl{margin:0;color:var(--fg-2)}
 .prop-warn{color:var(--warn);font-size:var(--fs-sm);margin:6px 0 0}
 .prop-warn:empty{display:none}
 pre.diff{margin:var(--s3) 0 0;padding:var(--s2) 0;max-height:260px;overflow:auto;white-space:pre;font:12px/1.65 var(--mono);word-break:normal}
 .diff span{display:block;padding:0 var(--s3);min-width:max-content}
 .diff .a{background:rgba(51,193,125,.08);color:#8fe0b5} .diff .d{background:rgba(240,96,93,.08);color:#f3a3a1} .diff .h{color:var(--muted)}
 .btnrow{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap;margin-top:var(--s3)}

 /* positions */
 table.t{width:100%;border-collapse:collapse;font-size:var(--fs)}
 .t th{font:600 var(--fs-xs)/1 var(--font);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);text-align:left;
   padding:10px var(--s4);border-bottom:1px solid var(--line);white-space:nowrap}
 .t td{padding:10px var(--s4);border-bottom:1px solid var(--line);white-space:nowrap}
 .t tr:last-child td{border-bottom:0}
 .t tbody tr:hover td{background:var(--surface-2)}
 .t .r{text-align:right}
 .t td.empty{color:var(--muted);text-align:center;padding:18px var(--s4)}
 .t tbody tr:hover td.empty{background:none}
 .sym{font-weight:600;letter-spacing:.01em}

 /* activity chart */
 .chart-wrap{padding:var(--s3) var(--s4) var(--s2);border-bottom:1px solid var(--line)}
 .chart-head{display:flex;align-items:baseline;gap:var(--s3);margin-bottom:6px;flex-wrap:wrap}
 .chart-head .big{font:600 20px/1 var(--mono);letter-spacing:-.02em}
 .chart-head .meta{font:var(--fs-sm) var(--mono);color:var(--muted)}
 #spark{width:100%;height:110px;display:block}
 .chart-axis{display:flex;justify-content:space-between;gap:var(--s2);font:11px/1.4 var(--mono);color:var(--faint);margin-top:6px}
 #pnl-total{font-size:var(--fs-sm)}

 /* activity feed */
 .feed{list-style:none;margin:0;padding:0}
 .feed li{display:grid;grid-template-columns:112px 22px minmax(0,1fr) auto;gap:var(--s3);align-items:start;
   padding:10px var(--s4);border-bottom:1px solid var(--line)}
 .feed li:last-child{border-bottom:0}
 .feed li:hover{background:var(--surface-2)}
 .feed li.empty{display:block;color:var(--muted);text-align:center;padding:24px var(--s4)}
 .feed li.empty:hover{background:none}
 .feed .when{font:12px/20px var(--mono);color:var(--muted);white-space:nowrap}
 .feed .when .wd,.feed .when .wt{display:block}
 .ic{width:20px;height:20px;border-radius:4px;display:grid;place-items:center}
 .ic svg.i{width:12px;height:12px;stroke-width:2}
 .ic.in{background:var(--up-soft);color:var(--up)} .ic.out{background:var(--accent-soft);color:var(--accent)}
 .ic.skip{background:var(--raise);color:var(--muted)}
 .feed .t{line-height:20px;color:var(--fg-2);overflow-wrap:anywhere} .feed .t b{color:var(--fg);font-weight:600}
 .feed .n{font-size:var(--fs-sm);color:var(--muted);margin-top:2px;overflow-wrap:anywhere}
 .feed .t .tags{white-space:nowrap}
 .feed .pnl{font:600 var(--fs)/20px var(--mono);text-align:right;min-width:72px;white-space:nowrap}

 /* chat */
 .chips{display:flex;gap:6px;flex-wrap:wrap}
 .chips button{height:24px;padding:0 9px;font:500 var(--fs-sm)/1 var(--mono);color:var(--fg-2)}
 .chips button:hover{color:var(--fg);border-color:var(--accent-line)}
 #p-chat #chatlog{margin:var(--s3) 0}
 #p-chat .hint{margin-top:var(--s3)}

 /* help */
 .help-actions{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center}
 #help p{margin:0 0 var(--s3);color:var(--fg-2)}
 #help p.hint{margin:var(--s3) 0 0;color:var(--muted)}
 #help-out{margin:var(--s3) 0 0;max-height:22rem}

 @media (max-width:1100px){
   .grid{grid-template-columns:minmax(0,1fr)}
   .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}
   .kpi:nth-child(4){border-left:0}
   .kpi:nth-child(n+4){border-top:1px solid var(--line)}
 }
 @media (max-width:640px){
   .wrap{padding:var(--s4) 16px var(--s6)}
   .modebar{align-items:flex-start}
   .modebar>svg{margin-top:2px}
   .modebar .mb-txt{flex-direction:column}
   .kpis{grid-template-columns:1fr 1fr}
   .kpi{border-left:1px solid var(--line)!important;border-top:1px solid var(--line)}
   .kpi:nth-child(odd){border-left:0!important}
   .kpi:nth-child(-n+2){border-top:0}
   .kpi:last-child{grid-column:1/-1}
   .kpi .v,.kpi .v.txt{font-size:20px}
   .t th,.t td{padding:9px 12px}
   .ph{padding:8px 12px}
   .pb,.chart-wrap{padding-left:12px;padding-right:12px}
   .feed li{grid-template-columns:20px minmax(0,1fr) auto;grid-template-rows:auto auto;padding:10px 12px;gap:4px 10px}
   .feed .when{grid-column:2/4;grid-row:2;line-height:1.3;font-size:11px}
   .feed .when .wd,.feed .when .wt{display:inline}
   .feed .ic{grid-row:1/3}
   .feed .pnl{min-width:0}
   .feed .t .tags{display:block;margin-top:2px;line-height:16px}
   .chips button{height:auto;min-height:40px;padding:0 12px}
 }
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
 <symbol id="i-up" viewBox="0 0 16 16"><path d="M8 13V3M4 7l4-4 4 4"/></symbol>
 <symbol id="i-out" viewBox="0 0 16 16"><path d="M8 3v10M4 9l4 4 4-4"/></symbol>
 <symbol id="i-skip" viewBox="0 0 16 16"><path d="M4 8h8"/></symbol>
 <symbol id="i-help" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2"/><path d="M6.3 6.3a1.8 1.8 0 0 1 3.4.6c0 1.2-1.7 1.5-1.7 2.6M8 11.6v.1"/></symbol>
 <symbol id="i-gear" viewBox="0 0 16 16"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M3.6 12.4l1.1-1.1M11.3 4.7l1.1-1.1"/></symbol>
 <symbol id="i-pause" viewBox="0 0 16 16"><path d="M6 3.5v9M10 3.5v9"/></symbol>
 <symbol id="i-play" viewBox="0 0 16 16"><path d="M5 3.5v9l7-4.5z"/></symbol>
 <symbol id="i-bolt" viewBox="0 0 16 16"><path d="M9 1.8 3.5 9h4l-.6 5.2L12.5 7h-4z"/></symbol>
 <symbol id="i-shield" viewBox="0 0 16 16"><path d="M8 1.8 2.8 3.8v4c0 3 2.2 5.4 5.2 6.4 3-1 5.2-3.4 5.2-6.4v-4z"/></symbol>
 <symbol id="mark" viewBox="0 0 24 24"><rect x=".5" y=".5" width="23" height="23" rx="5.5" fill="#12161e" stroke="#343e50"/>
  <path d="M5.5 15.5l4-4.5 3 2.6 5.5-6.6" fill="none" stroke="#5b9dff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="18" cy="7" r="1.6" fill="#5b9dff"/></symbol>
</svg>

<header class="appbar"><div class="appbar-in">
 <a class="brand" href="/dash"><svg class="mark" aria-hidden="true"><use href="#mark"/></svg>
  <span class="name">Agentic Trader</span><span class="crumb">/</span><span class="page">Dashboard</span></a>
 <span class="vsep hide-sm"></span>
 <span class="pill" id="b-mode">…</span>
 <span class="pill" id="b-market" style="display:none"></span>
 <span class="pill paused" id="b-paused" style="display:none">Paused</span>
 <span class="sp"></span>
 <div class="actions acts-a">
  <a class="btn btn-ghost" id="upd-badge" href="/setup" style="display:none;color:var(--accent)" title="Install it from settings (top of the page)">↑ Update<span class="lbl-sm">available</span></a>
  <button class="btn-ghost" id="help-btn" onclick="toggleHelp()" title="Help" aria-expanded="false" aria-controls="help"><svg class="i"><use href="#i-help"/></svg><span class="lbl-sm">Help</span></button>
  <a class="btn btn-ghost" href="/setup" title="Settings"><svg class="i"><use href="#i-gear"/></svg><span class="lbl-sm">Settings</span></a>
  <a class="btn btn-ghost" href="/v2" title="A redesigned dashboard. You can come back here at any time.">Try the new dashboard</a>
 </div>
 <span class="vsep hide-sm"></span>
 <div class="actions acts-b">
  <button id="q-pause" onclick="quick(this.dataset.cmd)" data-cmd="pause"><svg class="i"><use href="#i-pause"/></svg>Pause</button>
  <button id="q-dry" onclick="quick(this.dataset.cmd)" data-cmd="dry on">Dry-run</button>
 </div>
</div></header>

<main class="wrap">
 <div id="modebar" class="modebar dry" role="status"><svg class="i"><use href="#i-shield"/></svg>
  <div class="mb-txt"><span id="mb-main"><b>Dry-run.</b> The agent logs every decision but places no orders.</span>
  <span class="muted">Going live sends real orders with real money — your decision, not advice.</span></div></div>

 <div class="kpis">
  <div class="kpi"><div class="k">Entries today</div>
   <div class="v" id="t-entries">–</div><div class="meter" id="m-entries"><i></i></div>
   <div class="s" id="t-cap"></div></div>
  <div class="kpi"><div class="k">Open positions</div>
   <div class="v" id="t-pos">–</div><div class="s">opened by this agent</div></div>
  <div class="kpi" id="tile-feed"><div class="k">Signal feed</div>
   <div class="v txt" id="t-feed">–</div><div class="s" id="t-feed-s"></div></div>
  <div class="kpi"><div class="k">Size per trade</div>
   <div class="v" id="t-sizing">–</div><div class="s" id="t-sizing-s">your choice — not advice</div></div>
  <div class="kpi"><div class="k">Your rules</div>
   <div class="v txt" id="t-policy">–</div><div class="s" id="t-policy-hint"></div></div>
 </div>

 <div class="grid">
  <div class="col">
   <section class="panel prop" id="p-proposal" style="display:none">
    <div class="ph"><h2>Proposed code change — review before it applies</h2></div>
    <div class="pb">
     <p id="prop-expl"></p><p id="prop-warn" class="prop-warn"></p>
     <pre id="prop-diff" class="diff" tabindex="0" aria-label="Proposed code diff"></pre>
     <div class="btnrow">
      <button class="btn-primary" onclick="propApply()">Apply &amp; restart</button>
      <button class="back" onclick="propReject()">Reject</button>
      <span class="muted">Nothing changes until you apply.</span>
      <span id="prop-msg" role="status" aria-live="polite"></span>
     </div>
    </div>
   </section>

   <section class="panel" id="p-positions">
    <div class="ph"><h2>Open positions</h2><span class="count" id="pos-count" style="display:none"></span></div>
    <table class="t"><thead><tr><th style="width:24%">Symbol</th><th style="width:18%">Type</th><th style="width:24%">Expiry</th><th class="r">Strike</th></tr></thead>
     <tbody id="positions"><tr><td colspan=4 class=empty>No open positions</td></tr></tbody></table>
   </section>

   <section class="panel" id="p-activity">
    <div class="ph"><h2>Activity</h2><span class="count" style="display:none"></span><span class="sp"></span><span id="pnl-total" class="muted"></span></div>
    <div class="chart-wrap" style="display:none">
     <div class="chart-head"><span class="big" id="pnl-big"></span><span class="meta"></span></div>
     <svg id="spark" viewBox="0 0 600 110" preserveAspectRatio="none" aria-hidden="true"></svg>
     <div class="chart-axis"><span></span><span>┄ break-even</span><span></span></div>
    </div>
    <ul class="feed" id="trades"><li class="empty">Nothing yet — signals and the agent's decisions show up here</li></ul>
   </section>
  </div>

  <div class="col">
   <section class="panel" id="p-chat">
    <div class="ph"><h2>Talk to your agent</h2></div>
    <div class="pb">
     <div class="chips">
      <button onclick="ask('status')">status</button>
      <button onclick="ask('set cap 3')">set cap 3</button>
      <button onclick="ask('set positions 5')">set positions 5</button>
      <button onclick="ask('why did you skip the last signal?')">why the last skip?</button>
     </div>
     <div id="chatlog" role="log" aria-live="polite" tabindex="0" aria-label="Conversation with your agent"><span class="hint">Ask anything, or give a command —
     <code>pause</code>, <code>set budget 500</code>, <code>set percent 2</code>, <code>set cap 3</code>,
     <code>dry off</code>, <code>stop</code>.</span></div>
     <div class="chatrow">
      <input type="text" id="cmd" aria-label="Ask your agent or give a command" placeholder="Ask or command…"
       onkeydown="if(event.key==='Enter')send()">
      <button class="btn-primary" onclick="send()">Send</button>
     </div>
     <div class="hint">Code changes: run Claude Code in the agent's folder on
     this computer, then restart the agent.</div>
    </div>
   </section>

   <section class="panel" id="help" style="display:none">
    <div class="ph"><h2 tabindex="-1">Help</h2><span class="sp"></span><button class="btn-ghost btn-sm" onclick="toggleHelp()">Close</button></div>
    <div class="pb">
     <p>Something not working? These two buttons usually find the
     cause without any typing.</p>
     <div class="help-actions"><button onclick="helpNet()">Check my connection</button>
     <button onclick="helpReport()">Copy support report</button>
     <span id="help-msg" class="muted"></span></div>
     <pre id="help-out" style="display:none" tabindex="0" aria-label="Help output"></pre>
     <p class="hint">The report contains no keys, tokens,
     invoices or wallet IDs — they are removed before it's shown. Paste it to
     whoever helps you with the agent.</p>
    </div>
   </section>
  </div>
 </div>
</main>
<script>""" + _POLL_JS + """
async function api(p, body){
  const r=await fetch(p, body?{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}:{});
  return await r.json();
}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function $(id){return document.getElementById(id);}
function toggleHelp(){const h=$('help');const show=h.style.display==='none';h.style.display=show?'':'none';
  const btn=$('help-btn');if(btn)btn.setAttribute('aria-expanded',show?'true':'false');
  if(show){
    // on narrow layouts the panel sits below Activity + Chat: move focus to
    // it so keyboard / screen-reader users land on what just opened
    const hd=h.querySelector('.ph h2');
    try{if(hd)hd.focus({preventScroll:true});}catch(e){}
    try{h.scrollIntoView({behavior:'smooth',block:'nearest'});}catch(e){}
  }else if(btn){try{btn.focus({preventScroll:true});}catch(e){}}}
async function helpGet(path,label){
  const msg=$('help-msg'),out=$('help-out');
  msg.textContent=label+'… (up to a minute)';out.style.display='none';
  try{
    const r=await fetch(path);const j=await r.json();
    out.textContent=j.text||j.error||'no answer';out.style.display='';msg.textContent='';
    return j.text||'';
  }catch(e){msg.textContent='Could not reach the agent on this computer — is it still running?';return '';}
}
async function helpNet(){await helpGet('/api/help/netcheck','checking the connection');}
async function helpReport(){
  const t=await helpGet('/api/help/report','building the report');
  if(!t)return;
  try{await navigator.clipboard.writeText(t);$('help-msg').textContent='copied ✓ — paste it to support';}
  catch(e){$('help-msg').textContent='select the text below and copy it';}
}
// "update available" badge: one quiet check per page load; never installs.
(async function(){try{const r=await (await fetch('/api/update/check')).json();
  if(r.ok&&r.behind)$('upd-badge').style.display='';}catch(e){}})();

const SOURCES={agenthc:'AgentHC feed',url:'Your JSON feed',manual:'Manual commands'};
const SKIPS={skip_stale:'signal was too old',skip_daily_cap:'daily entry cap reached',
  skip_position_cap:'max open positions reached',skip_daily_loss:'daily loss limit reached',
  policy_veto:'your rules said no',skip_paused:'the agent is paused'};
const ICON=n=>'<svg class="i"><use href="#i-'+n+'"/></svg>';
function cap1(s){s=String(s||'');return s.charAt(0).toUpperCase()+s.slice(1);}
function shortDate(d){return d.toLocaleDateString([], {month:'short',day:'numeric'});}
function hhmm(d){return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false});}
function when(x){
  // the agent's ET date + time ("Nov 6, 3:55:17 PM ET"; "—" when unknown):
  // date over time on wide screens, one line on phones; your own time on hover
  const s=String(x.event_at_et||'');if(!s)return '<span class="when">—</span>';
  const d=new Date(x.event_at||''),k=s.lastIndexOf(', ');
  if(k<0)return '<span class="when">'+esc(s)+'</span>';
  const tip=isNaN(d)?'':d.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',
    minute:'2-digit',second:'2-digit',timeZoneName:'short'})+' (your time)';
  return '<span class="when" title="'+esc(tip)+'"><span class="wd">'+esc(s.slice(0,k))+',</span> '+
    '<span class="wt">'+esc(s.slice(k+2))+'</span></span>';
}
function axisLabel(ts,withTime){
  const d=new Date(ts);if(isNaN(d))return '';
  const today=new Date().toDateString()===d.toDateString();
  if(today)return hhmm(d);
  return shortDate(d)+(withTime?' '+hhmm(d):'');
}
function money(v){const n=Number(v);return (n>=0?'+$':'−$')+Math.abs(n).toFixed(2);}
// one strike format everywhere: "$290" for whole strikes, "$717.50" otherwise
function fmtStrike(v){const n=Number(v);if(!isFinite(n))return esc(String(v));
  return '$'+(Number.isInteger(n)?String(n):n.toFixed(2));}
function contractHtml(c,extra){
  // "NVDA 2026-10-16 $215 PUT" -> NVDA Oct 16 $215 [PUT] [DRY-RUN|LIVE]
  // tags sit in one no-wrap group so PUT + DRY-RUN never split across lines
  const m=String(c||'').match(/^(\\S+)\\s+(\\d{4})-(\\d{2})-(\\d{2})\\s+\\$?([\\d.]+)\\s+(CALL|PUT)$/i);
  const tags=t=>(t?' <span class="tags">'+t+'</span>':'');
  if(!m)return '<b>'+esc(c||'')+'</b>'+tags(extra||'');
  const d=new Date(+m[2],+m[3]-1,+m[4]);
  return '<b>'+esc(m[1])+'</b> <span class="num">'+esc(shortDate(d))+' '+fmtStrike(m[5])+'</span>'+
    tags('<span class="tag '+(m[6].toUpperCase()==='CALL'?'call':'put')+'">'+
    esc(m[6].toUpperCase())+'</span>'+(extra?' '+extra:''));
}
function feedTile(text){
  // "prepaid pass · 4 signal days left" / "wallet 41,000 sats" / "Premium API key"
  const t=String(text||'');
  const tile=$('tile-feed'),v=$('t-feed');
  const m=t.match(/(\\d+)\\s+signal day/);
  if(m){const n=parseInt(m[1],10);v.className='v';
    v.innerHTML=n+'<small> day'+(n===1?'':'s')+' left</small>';
    $('t-feed-s').textContent='prepaid signal days';}
  else{v.className='v txt';v.textContent=t||'–';$('t-feed-s').textContent='';}
  v.title=t;
  const bad=/expired|used up|not paid|none|no access|exits only/i.test(t);
  tile.classList.toggle('bad',bad);
}
function spark(points){
  const svg=$('spark'),wrap=svg.closest('.chart-wrap');
  if(points.length<2){wrap.style.display='none';svg.innerHTML='';return;}
  const min=Math.min(0,...points),max=Math.max(0,...points),span=(max-min)||1;
  const x=i=>i*600/(points.length-1),y=v=>102-(v-min)*94/span;
  const d=points.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)).join(' ');
  const z=y(0).toFixed(1);
  const col=points[points.length-1]>=0?'#33c17d':'#f0605d';
  svg.innerHTML='<path d="'+d+' L600 '+z+' L0 '+z+'Z" fill="'+col+'" fill-opacity=".10" stroke="none"/>'+
    '<line x1="0" x2="600" y1="'+z+'" y2="'+z+'" stroke="#343e50" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>'+
    '<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
  wrap.style.display='';
}
function setMode(kind,live,paused,note,pauseNote,restarting,hold){
  // kind: 'ok' | 'off' | 'unreachable'
  // #modebar is a live region polled every 5s: only touch the DOM when the
  // text actually changes, or screen readers re-announce it on every poll.
  const mb=$('modebar'),main=$('mb-main'),sub=mb.querySelector('.mb-txt>.muted');
  const put=(cls,h)=>{if(mb.className!==cls)mb.className=cls;if(main.innerHTML!==h)main.innerHTML=h;};
  if(kind==='off'){put('modebar off','<b>Agent not running.</b> Start it: <code>python agent.py start</code>');return;}
  if(kind==='unreachable'){put('modebar off','<b>Can\\'t reach the agent on this computer</b> — is it still running?');return;}
  put('modebar '+(live?'live':'dry')+(paused?' paused':''),
    (live?'<b>LIVE.</b> Real orders with real money are sent to your broker.':
    note?'<b>Dry-run.</b> '+esc(note):
    '<b>Dry-run.</b> The agent logs every decision but places no orders.')+
    (hold?' <b class="pz">On hold</b> — '+esc(pauseNote||'the agent places no orders at all.'):
     paused?' <b class="pz">Paused</b> — no new entries. Exit signals and the expiry guard keep'+
      ' running for positions you already have. Stays paused, also after a restart, until you press Resume.'+
      (pauseNote?' '+esc(pauseNote):''):'')+
    (restarting?' <b class="pz">Restarting</b> — the agent reads no signals and places no orders until it is back (a few seconds).':''));
  const st=live?'Switch to dry-run any time to stop new trades (positions already open are still sold on their exit signal) — your decision, not advice.':
    'Going live sends real orders with real money — your decision, not advice.';
  if(sub.textContent!==st)sub.textContent=st;
}
function kpisBlank(){
  ['t-entries','t-pos','t-sizing','t-policy','t-feed'].forEach(id=>{$(id).textContent='–';});
  ['t-cap','t-feed-s','t-policy-hint'].forEach(id=>{$(id).textContent='';});
  $('m-entries').firstChild.style.width='0%';$('tile-feed').classList.remove('bad');
}
function badMode(text,kind){
  const bm=$('b-mode');bm.textContent=text;bm.className='pill bad';
  $('b-market').style.display='none';$('b-paused').style.display='none';
  setMode(kind);kpisBlank();
}
async function refresh(){
  try{
    const s=await api('/api/status');
    if(s.mode==='OFFLINE'){badMode('Agent not running','off');return;}
    const f=s.fields||{};const live=s.mode==='LIVE';
    const bm=$('b-mode');
    LAST=s;const note=live?'':(s.dry_run_note||'');
    bm.innerHTML=live?'<span class="dot"></span>LIVE · real orders':
      '<span class="dot"></span>Dry-run · '+(note?'no new trades':'no orders');
    bm.className='pill '+(live?'live':'dry');
    const mk=$('b-market'),open=(f['market']||'').startsWith('open');
    mk.innerHTML=open?'<span class="dot"></span>Market open':'Market closed';
    mk.className='pill'+(open?' open':'');mk.style.display='';
    $('b-paused').style.display=s.paused?'':'none';
    $('b-paused').textContent=s.hold?'On hold':'Paused';
    setMode('ok',live,!!s.paused,note,s.pause_note||'',!!s.restarting,!!s.hold);
    const qp=$('q-pause');
    qp.innerHTML=s.paused?ICON('play')+'Resume':ICON('pause')+'Pause';
    qp.dataset.cmd=s.paused?'resume':'pause';qp.className=s.paused?'btn-primary':'';
    const qd=$('q-dry');
    qd.innerHTML=live?'Switch to dry-run':ICON('bolt')+'Go live';
    qd.dataset.cmd=live?'dry on':'dry off';qd.className=live?'':'btn-warn';
    const ent=(f['entries today']||'').split('/');
    const n=parseInt(ent[0],10)||0,cap=parseInt((ent[1]||'').replace(/\\D+/g,''),10)||0;
    $('t-entries').innerHTML=isNaN(parseInt(ent[0],10))?'–':(n+(cap?'<small> / '+cap+'</small>':''));
    $('t-cap').textContent=cap?('of '+cap+' allowed today'):'';
    const mt=$('m-entries');mt.firstChild.style.width=(cap?Math.min(100,n*100/cap):0)+'%';
    mt.className='meter'+(cap&&n>=cap?' hot':'');
    const pos=(f['open positions']||'none');
    const list=pos==='none'?[]:pos.split(', ');
    $('t-pos').textContent=list.length;
    const pc=$('pos-count');pc.textContent=list.length;pc.style.display=list.length?'':'none';
    $('positions').innerHTML=list.length?list.map(p=>{
      const a=p.split('|');
      if(a.length<4)return '<tr><td class=sym colspan=4>'+esc(p)+'</td></tr>';
      const d=new Date(a[1]+'T12:00:00');const k=Number(a[2]);
      return '<tr><td class=sym>'+esc(a[0])+'</td><td><span class="tag '+(a[3]==='C'?'call':'put')+'">'+
        (a[3]==='C'?'CALL':'PUT')+'</span></td><td class=num>'+esc(isNaN(d)?a[1]:shortDate(d))+
        '</td><td class="r num">'+(isNaN(k)?esc(a[2]):fmtStrike(k))+'</td></tr>';
    }).join(''):'<tr><td colspan=4 class=empty>No open positions</td></tr>';
    // "$500/trade budget (other trades: $100/trade)" / "1 contract(s)/trade":
    // the main sizing goes in the value, the other-trades part in the caption
    // (no hover on touch screens), and "contract(s)" is pluralised from n.
    const szRaw=String(f['sizing']||'–');
    const plur=s=>s.replace(/(\\d+) contract\\(s\\)(\\/trade)?/g,(m,n)=>n+' contract'+(n==='1'?'':'s'));
    const oi=szRaw.indexOf(' (other trades:');
    const sz=plur(oi>=0?szRaw.slice(0,oi):szRaw);
    const szOther=oi>=0?plur(szRaw.slice(oi+2).replace(/\\)\\s*$/,'')):'';
    const sm=sz.match(/^(\\S+)\\s+(.+)$/);const sv=$('t-sizing');sv.title=szRaw+' — your choice, not advice';
    sv.innerHTML=sm?esc(sm[1])+'<small> '+esc(sm[2])+'</small>':esc(sz);
    $('t-sizing-s').textContent=szOther||'your choice — not advice';
    $('t-sizing-s').title=szOther?szOther+' — your choice, not advice':'';
    const polOn=f['policy brain']==='on', polBlk=f['policy brain']==='blocked';
    $('t-policy').innerHTML=polBlk?'<span class="off-dot"></span>Blocking entries':polOn?'<span class="ok-dot"></span>On':'<span class="off-dot"></span>Off';
    $('t-policy-hint').textContent=polBlk?'no AI connected: every entry is skipped. Connect your AI in Setup':polOn?'checked before every entry':'add rules in settings → Safety';
    if(f['source']==='agenthc')feedTile(f['feed access']);
    else{const v=$('t-feed');v.className='v txt';v.textContent=SOURCES[f['source']]||f['source']||'–';
      $('t-feed-s').textContent='signal source';$('tile-feed').classList.remove('bad');}
    try{
      const p=await api('/api/proposal');const panel=$('p-proposal');
      if(p.pending){panel.style.display='';
        $('prop-expl').textContent=p.explanation;
        $('prop-warn').textContent=(p.warnings||[]).join('  ');
        $('prop-diff').innerHTML=String(p.diff||'').split('\\n').map(l=>{
          const c=l.charAt(0);
          const k=(l.startsWith('+++')||l.startsWith('---')||c==='@')?'h':c==='+'?'a':c==='-'?'d':'';
          return '<span'+(k?' class="'+k+'"':'')+'>'+(esc(l)||' ')+'</span>';}).join('');}
      else panel.style.display='none';
    }catch(e){}
    const t=await api('/api/trades');
    const rows=t.trades||[];
    let cum=0;const pts=[0],exTs=[];
    rows.slice().reverse().forEach(x=>{if(x.action==='exit'&&x.est_pnl_usd!=null){cum+=Number(x.est_pnl_usd);pts.push(cum);exTs.push(x.ts);}});
    spark(pts);
    const ac=document.querySelector('#p-activity .ph .count');ac.textContent=rows.length;ac.style.display=rows.length?'':'none';
    if(pts.length>1){
      $('pnl-total').textContent='est. P&L of exits shown';
      const big=$('pnl-big');big.textContent=money(cum);big.className='big '+(cum>=0?'up':'down');
      const nx=exTs.length,a1=axisLabel(exTs[0],false),a2=axisLabel(exTs[nx-1],true);
      document.querySelector('#p-activity .chart-head .meta').textContent=nx+' exit'+(nx===1?'':'s')+(a1?' · '+a1+' → '+a2:'');
      const ax=document.querySelectorAll('#p-activity .chart-axis>span');ax[0].textContent=a1;ax[2].textContent=a2;
    }else{$('pnl-total').textContent='';$('pnl-big').textContent='';}
    $('trades').innerHTML=rows.length?rows.map(x=>{
      const a=x.action||'';let ic='skip',icon='skip',title,pnl='<span class="pnl faint">—</span>';
      const note=(a==='policy_veto'||a==='entry'||a==='exit')&&x.reason?esc(String(x.reason).slice(0,120)):'';
      let n=note;
      // real-money rows get an explicit LIVE tag — absence of DRY-RUN is not a signal
      const mt=x.dry?'<span class="tag dry">DRY-RUN</span>':(x.dry===false?'<span class="tag live">LIVE</span>':'');
      if(a==='entry'){ic='in';icon='up';title='Entered '+contractHtml(x.contract,mt);}
      else if(a==='exit'){ic='out';icon='out';title='Exited '+contractHtml(x.contract,mt);
        if(x.est_pnl_usd!=null){const v=Number(x.est_pnl_usd);
          pnl='<span class="pnl '+(v>=0?'up':'down')+'">'+money(v)+'</span>';}}
      else{title='Skipped '+contractHtml(x.contract,x.dry?'<span class="tag dry">DRY-RUN</span>':'');
        n=esc(cap1(SKIPS[a]||a.replace(/_/g,' ')))+(note?' — '+note:'');}
      return '<li>'+when(x)+'<span class="ic '+ic+'">'+ICON(icon)+'</span><div><div class="t">'+title+'</div>'+
        (n?'<div class="n">'+n+'</div>':'')+'</div>'+pnl+'</li>';
    }).join(''):'<li class="empty">Nothing yet — signals and the agent\\'s decisions show up here</li>';
  }catch(e){badMode('Agent unreachable','unreachable');}
}
async function propApply(){
  $('prop-msg').textContent='applying…';
  const r=await api('/api/proposal/apply',{});
  $('prop-msg').textContent=r.reply;
}
async function propReject(){await api('/api/proposal/reject',{});refresh();}
let chatStarted=false;
function chatAdd(cls,text){
  const log=$('chatlog');
  if(!chatStarted){log.innerHTML='';chatStarted=true;}
  log.insertAdjacentHTML('beforeend','<div class="msg '+cls+'"><div class="who">'+(cls==='me'?'You':'Agent')+
    '</div><div class="body">'+esc(text)+'</div></div>');
  log.scrollTop=log.scrollHeight;
}
let LAST=null;  // the latest /api/status
async function quick(cmd){
  // Real positions are open: say what dry-run does with them before switching.
  if(cmd==='dry on'&&LAST&&LAST.dry_run_note&&!confirm('Switch to dry-run? '+LAST.dry_run_note))return;
  // Going LIVE is a real-money action: never on a single (mis-)tap.
  if(cmd==='dry off'&&!confirm('Go LIVE? The agent will send real orders with real money to your broker from now on. Your decision — not advice.'))return;
  chatAdd('me',cmd);
  const r=await api('/api/command',{text:cmd});chatAdd('agent',r.reply);refresh();
}
function ask(text){$('cmd').value=text;send();}
async function send(){
  const box=$('cmd');const text=box.value.trim();if(!text)return;
  box.value='';chatAdd('me',text);
  const r=await api('/api/command',{text:text});chatAdd('agent',r.reply);refresh();
}
poll(refresh,5000);  // paused while the page is hidden, at once when shown
</script></body></html>"""


# ── shared handler plumbing ──────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    routes_get = {}
    routes_post = {}
    pages = {}
    landing = "/setup"
    v2 = False  # serve the redesigned dashboard at /v2 (start_app turns it on)

    def _remembered(self):
        """The dashboard the owner chose to land on ("classic" | "v2");
        start_app reads it from the settings. "v2" only while /v2 is built."""
        return "classic"

    def log_message(self, *a):  # quiet
        pass

    def _guard(self, check_origin):
        """Reject cross-site / rebinding requests. Returns False (and sends 403)
        if the request must not be served."""
        host = self.headers.get("Host")
        if host and host not in _ALLOWED_HOSTS:
            self._send({"error": "forbidden host"}, 403)
            return False
        if check_origin:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in _ALLOWED_ORIGINS:
                self._send({"error": "forbidden origin"}, 403)
                return False
        return True

    def _authed(self):
        from http.cookies import SimpleCookie
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
        except Exception:
            return False
        m = c.get(_SESSION_COOKIE)
        return _token_ok(m.value if m else "")

    def _login_from_query(self):
        """`?k=<token>` → set the session cookie and redirect to the clean
        path (keeps the secret out of the address bar). Returns True if it
        handled the request."""
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        k = (parse_qs(u.query).get("k") or [""])[0]
        if not k:
            return False
        if not _token_ok(k):
            self._send(None, 403, html=_LOCKED_PAGE)
            return True
        to = u.path or "/"
        if (to == "/dash" and self.v2 and self.landing == "/dash"
                and self._remembered() == "v2"):
            # the agent's login link (agent.py link, start, opening the
            # dashboard at login) opens the dashboard the owner chose;
            # /dash typed without it is always the classic one
            to = "/v2"
        self.send_response(302)
        self.send_header("Set-Cookie", f"{_SESSION_COOKIE}={k}; Path=/; HttpOnly; "
                         "SameSite=Lax; Max-Age=31536000")
        self.send_header("Location", to)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return True

    def _send(self, obj, status=200, html=None):
        body = html.encode() if html is not None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type",
                         "text/html; charset=utf-8" if html is not None
                         else "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, body, ctype, status=200, cache="no-store", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._guard(check_origin=False):
            return
        if self._login_from_query():
            return
        path = self.path.split("?")[0]
        if not self._authed():
            if path.startswith("/api/"):
                return self._send({"error": "locked — open the dashboard "
                                   "login link (python agent.py link)"}, 401)
            return self._send(None, 401, html=_LOCKED_PAGE)
        if path == "/":
            self.send_response(302)
            # the agent's own server lands on the dashboard the owner chose;
            # the setup server always on the wizard
            self.send_header("Location", "/v2" if self.landing == "/dash"
                             and self.v2 and self._remembered() == "v2"
                             else self.landing)
            self.end_headers()
            return
        if (path == "/setup" and self.v2 and _return_to(self.path) is None
                and "from=" not in self.path and self._remembered() == "v2"):
            # the wizard's "Dashboard" links and its hop after "Start my
            # agent" go to the dashboard the owner chose (?from=v2, the
            # fixed value; anything else in the address is kept as it is)
            sep = "&" if "?" in self.path else "?"
            self.send_response(302)
            self.send_header("Location", f"{self.path}{sep}from=v2")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path in self.pages:
            app = None if "ui=classic" in self.path else _app_index()
            if app is None:
                page = self.pages[path]
                if path == "/setup":
                    page = _wizard_page(page, self.path)
                return self._send(None, html=page)
            return self._send_raw(app.encode(), "text/html; charset=utf-8",
                                  extra={"Content-Security-Policy": _APP_CSP,
                                         "X-Frame-Options": "DENY",
                                         "Referrer-Policy": "no-referrer"})
        if path == "/v2" and self.v2:
            app = _app_v2()
            if app is None:
                # a build without it: the dashboard that is there
                self.send_response(302)
                self.send_header("Location", self.landing)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._send_raw(app.encode(), "text/html; charset=utf-8",
                                  extra={"Content-Security-Policy": _APP_CSP,
                                         "X-Frame-Options": "DENY",
                                         "Referrer-Policy": "no-referrer"})
        if path.startswith("/assets/"):
            full = _asset_path(path)
            if full is None:
                return self._send({"error": "not found"}, 404)
            with open(full, "rb") as f:
                body = f.read()
            # file names carry a content hash: safe to cache for good
            return self._send_raw(
                body, _ASSET_TYPES[os.path.splitext(full)[1].lower()],
                cache="private, max-age=31536000, immutable")
        fn = self.routes_get.get(path)
        if fn is None:
            return self._send({"error": "not found"}, 404)
        try:
            res = fn(self)
            if isinstance(res, RawResponse):
                return self._send_raw(res.body, res.ctype, extra=res.headers)
            return self._send(res)
        except Exception as exc:
            return self._send({"ok": False, "error": str(exc)[:200]}, 500)

    def do_POST(self):
        if not self._guard(check_origin=True):
            return
        if not self._authed():
            return self._send({"error": "locked — open the dashboard login "
                               "link (python agent.py link)"}, 401)
        fn = self.routes_post.get(self.path.split("?")[0])
        if fn is None:
            return self._send({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            return self._send(fn(self, data))
        except Exception as exc:
            return self._send({"ok": False, "error": str(exc)[:200]}, 500)


# ── phone remote (remote_access.py) ──────────────────────────────────────────
# A second listener, also loopback-only, for the phone app. The owner
# publishes it on their own tailnet (`tailscale serve`); nothing here is on
# the internet. It serves an ALLOWLIST of the dashboard's own v2 routes, and
# only to a paired phone's bearer token: no cookies, and any request that
# carries a browser Origin is refused, so no web page can drive it.
REMOTE_GET = frozenset({
    "/api/v2/status", "/api/v2/activity", "/api/v2/settings",
    "/api/v2/agreement"})
REMOTE_POST = frozenset({
    "/api/v2/command", "/api/v2/agent/start", "/api/v2/settings/limits",
    "/api/v2/settings/rules", "/api/v2/settings/alerts",
    "/api/v2/settings/signals", "/api/v2/pass/buy", "/api/v2/ask"})
# NOT reachable from a phone: code edits and proposals, moving the install,
# wallet keys, broker sign-in, AI keys, computer settings and the consent
# itself. Those stay on the owner's computer.
REMOTE_MAX_BODY = 64 * 1024
REMOTE_API_VERSION = 1


def remote_handler(base):
    """The phone API handler, built on the app's handler so every allowed
    route runs exactly as it does for the dashboard."""
    import remote_access as RA

    class R(base):
        device = None

        def _no_browser(self):
            if self.headers.get("Origin") is not None:
                self._send({"error": "forbidden origin"}, 403)
                return False
            return True

        def _bearer(self):
            h = self.headers.get("Authorization") or ""
            return h[7:].strip() if h[:7].lower() == "bearer " else ""

        def _remote_ok(self):
            if not self._no_browser():
                return False
            self.device = RA.device_for_token(self._bearer())
            if not self.device:
                self._send({"error": "this phone is not paired — run "
                            "`python agent.py pair` and scan the code"}, 401)
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length < 0 or length > REMOTE_MAX_BODY:
                raise ValueError("request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _me(self):
            return {"id": self.device.get("id"), "name": self.device.get("name")}

        def do_GET(self):
            path = self.path.split("?")[0]
            if not self._remote_ok():
                return
            if path == "/api/remote/whoami":
                return self._send({"ok": True, "device": self._me(),
                                   "api": REMOTE_API_VERSION})
            fn = self.routes_get.get(path) if path in REMOTE_GET else None
            if fn is None:
                return self._send({"error": "not found"}, 404)
            try:
                res = fn(self)
                if isinstance(res, RawResponse):
                    return self._send({"error": "not available on the phone"}, 404)
                return self._send(res)
            except Exception as exc:
                return self._send({"ok": False, "error": str(exc)[:200]}, 500)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/remote/pair":
                if not self._no_browser():
                    return
                try:
                    data = self._body()
                except Exception as exc:
                    return self._send({"error": str(exc)[:200]}, 400)
                r = RA.pair(data.get("code"), data.get("name"),
                            data.get("push_token"))
                return self._send(r, 200 if "token" in r else 403)
            if not self._remote_ok():
                return
            try:
                data = self._body()
            except Exception as exc:
                return self._send({"error": str(exc)[:200]}, 400)
            if path == "/api/remote/push":
                tok = data.get("push_token") or None
                ok = RA.set_push_token(self.device["id"], tok)
                return self._send({"ok": ok} if ok else
                                  {"ok": False, "error": "not an Expo push token"},
                                  200 if ok else 400)
            if path == "/api/remote/unpair":
                return self._send({"ok": RA.revoke(self.device["id"])})
            fn = self.routes_post.get(path) if path in REMOTE_POST else None
            if fn is None:
                return self._send({"error": "not found"}, 404)
            try:
                return self._send(fn(self, data))
            except Exception as exc:
                return self._send({"ok": False, "error": str(exc)[:200]}, 500)

    return R


class _RemoteServer(_AppServer):
    """The phone API's own server class (so code that hooks the dashboard
    server, e.g. a test capturing _AppServer, never mistakes one for the
    other)."""


def start_remote(base, port=None):
    """Serve the phone API on 127.0.0.1:<port>. Returns the server, or None
    when turned off (AGENT_REMOTE=0) or the port is taken. Never raises."""
    import remote_access as RA
    if os.getenv("AGENT_REMOTE", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        srv = _RemoteServer(("127.0.0.1", RA.REMOTE_PORT if port is None else port),
                            remote_handler(base))
    except OSError as exc:
        print(f"Phone remote not started (port {RA.REMOTE_PORT}: {exc}). "
              "The dashboard is unaffected.")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── the setup wizard ─────────────────────────────────────────────────────────

def _install_requirements():
    """pip-install requirements.txt into the running interpreter after a code
    pull. Returns a note suffix: '' on success/nothing-to-do, a warning
    string on failure (never raises — deps are fail-soft by design)."""
    import os
    import subprocess
    import sys
    req = os.path.join(_repo_dir(), "requirements.txt")
    if not os.path.isfile(req):
        return ""
    cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", req]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 and "externally-managed" in (r.stderr or ""):
            r = subprocess.run(cmd + ["--break-system-packages"],
                               capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return (" · dependency install FAILED — run manually: "
                    "pip install -r requirements.txt")
        return " · dependencies installed"
    except Exception:
        return (" · dependency install skipped — run manually: "
                "pip install -r requirements.txt")


# Live BTC/USD for the funding box (5-min cache; two independent public
# sources, fail-soft). Sats are meaningless to most users — the wizard shows
# "≈ $X · covers ~N day-passes" next to the amount instead.
_btc_usd_cache = {"t": 0.0, "usd": 0.0}
_DAY_PASS_USD = 10.0        # feed day-pass ballpark, floats with BTC


def _invoice_cap_from_error(msg):
    """Per-invoice cap (sats) parsed from an LNbits 'amount too high' error,
    or 0. E.g. demo.lnbits.com: 'Invoice amount 328000 sats is too high.
    Max allowed: 100000 sats.'"""
    import re
    m = re.search(r"[Mm]ax allowed:\s*([0-9][0-9,]*)", str(msg))
    return int(m.group(1).replace(",", "")) if m else 0


def _btc_usd():
    import time
    if time.time() - _btc_usd_cache["t"] < 300 and _btc_usd_cache["usd"] > 0:
        return _btc_usd_cache["usd"]
    import requests
    for fetch in (
        lambda: float(requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=5).json()["data"]["amount"]),
        lambda: float(requests.get(
            "https://blockchain.info/ticker",
            timeout=5).json()["USD"]["last"]),
    ):
        try:
            usd = fetch()
            if usd > 0:
                _btc_usd_cache.update(t=time.time(), usd=usd)
                return usd
        except Exception:
            continue
    return 0.0


def _fund_quote(sats, invoice_cap=0):
    """USD context for a sats amount: value, ~day-passes covered, and the
    sats price of ONE ~$10 day pass at the current BTC price (the funding
    default — this is a day-pass product). suggested_sats is the day pass
    clamped to the instance's per-invoice cap when one is known.
    {ok: False} when no live price is available (the funding box just omits
    the hint)."""
    usd_per_btc = _btc_usd()
    if usd_per_btc <= 0:
        return {"ok": False, "error": "no live BTC price"}
    usd = sats * usd_per_btc / 1e8
    day_sats = int(-(-_DAY_PASS_USD / usd_per_btc * 1e8 // 100) * 100)  # ceil→100
    suggested = min(day_sats, invoice_cap) if invoice_cap else day_sats
    return {"ok": True, "btc_usd": round(usd_per_btc, 2),
            "usd": round(usd, 2),
            "day_passes": int(usd // _DAY_PASS_USD),
            "day_pass_sats": day_sats,
            "suggested_sats": suggested,
            "invoice_cap": invoice_cap or None}


# One-shot guard: attempt the missing-dependency bootstrap at most once per
# process so a broken pip can't stall every invoice render.
_qr_bootstrap_attempted = False


_NET_DIAG_CACHE = {"at": 0.0, "text": ""}


def _net_error_text(exc, cache_s=0):
    """Error text for the wizard; connection failures get the diagnosed
    cause + fix (antivirus, VPN, DNS filter…) instead of a raw SSL error.
    `cache_s` reuses a recent diagnosis — for routes the page polls every
    few seconds (a diagnosis takes seconds and makes network probes)."""
    import time
    import net_check
    if net_check.is_connection_error(exc):
        if cache_s and _NET_DIAG_CACHE["text"] and \
                time.time() - _NET_DIAG_CACHE["at"] < cache_s:
            return _NET_DIAG_CACHE["text"]
        try:
            from agent import _agenthc_host
            diag = net_check.diagnose(_agenthc_host())
        except Exception:
            diag = None
        if diag and not diag["ok"]:
            text = net_check.summary(diag)
            _NET_DIAG_CACHE.update(at=time.time(), text=text)
            return text
    return str(exc)[:200]


def _qr_matrix(data):
    """QR module matrix (list of 0/1 rows) for `data`, or None when the
    qrcode package is unavailable (fail-soft — the wizard still offers the
    copy button and lightning: link). Self-healing: installs on existing
    setups that predate the qrcode dependency (their update pull ran the
    old handler that didn't install requirements), then retries the import
    once. Rendered client-side as an SVG path — no image bytes or external
    assets."""
    global _qr_bootstrap_attempted
    try:
        import qrcode
    except ImportError:
        if _qr_bootstrap_attempted:
            return None
        _qr_bootstrap_attempted = True
        _install_requirements()
        import importlib
        importlib.invalidate_caches()
        try:
            import qrcode
        except ImportError:
            return None
    try:
        q = qrcode.QRCode(border=2)
        q.add_data(data)
        q.make(fit=True)
        return [[1 if cell else 0 for cell in row] for row in q.get_matrix()]
    except Exception:
        return None


# ── tips (dashboard "Tip" button) ────────────────────────────────────────────
# The owner tips AgentHC $100, $500 or $1,000, paid in sats at the live
# Bitcoin price, by paying an invoice from their own wallet (QR / copy /
# lightning: link). AgentHC prices the invoice; these routes only ask for it
# and its status, check it, and never pay, never touch the agent's wallet
# (a tip is far above max_autopay_sats, and is never paid from it anyway),
# and write nothing to config, state or the trade log.

# $ tiers AgentHC offered on the last /api/tip/options read, and the BTC
# price they came with. A create is only sent for one of these tiers that is
# also on hc.TIP_TIERS_USD.
_tip_offered = {"usd": (), "btc": None}
# AgentHC's BTC price may differ by at most this much from the independent
# price (_btc_usd: Coinbase / blockchain.info, cached 5 min). AgentHC's quote
# can be up to 15 minutes old; a bigger gap means something is off. Checked
# before asking for an invoice (against the /options price, so a refusal
# doesn't leave an unpaid invoice counting against AgentHC's per-IP cap) and
# again on the invoice's own price. No independent price, no invoice.
_TIP_XCHECK_BAND = 0.03
_TIP_PICK = "Pick one of the tip amounts shown."
_TIP_NOT_OFFERED = ("That tip amount isn't on offer right now. Pick one of "
                    "the amounts shown.")
_TIP_PRICE_ODD = ("AgentHC's Bitcoin price for this tip is too far from the "
                  "market price, so the invoice wasn't shown. Nothing was "
                  "paid. Try again in a few minutes.")
_TIP_SATS_ODD = ("The Bitcoin price AgentHC sent looks wrong. Try again in "
                 "a minute.")
_TIP_NO_MARKET = "Can't check the Bitcoin price right now. Try again in a minute."


def _tip_price_far(btc, market):
    return abs(btc / market - 1) > _TIP_XCHECK_BAND


def _tip_label(usd, sats):
    """"$500 (~643,500 sats)": the sats rounded to 100 for the tier buttons;
    the invoice itself shows the exact amount."""
    label = f"${usd:,}"
    if type(sats) is int and sats > 0:
        label += f" (~{round(sats, -2):,} sats)"
    return label


def _tip_options_route(_h):
    from sources import agenthc_day_trade_ideas as hc
    try:
        body = hc.tip_options()
    except hc.TipsUnavailable:
        return {"ok": False, "unavailable": True, "error": hc.TIPS_NOT_YET}
    except Exception as exc:
        return {"ok": False, "error": _net_error_text(exc)}
    tiers = []
    for t in body.get("tiers") or []:
        sats = t["amount_sats"]  # None unless it matches the quote (hc checked)
        available = t.get("available") is True and sats is not None
        reason = str(t["reason"])[:200] if t.get("reason") else None
        if not available and not reason:
            reason = _TIP_SATS_ODD
        tiers.append({"amount_usd": t["amount_usd"], "amount_sats": sats,
                      "available": available,
                      "reason": None if available else reason,
                      "label": _tip_label(t["amount_usd"], sats)})
    q = body.get("quote")
    _tip_offered.update(usd=tuple(t["amount_usd"] for t in tiers),
                        btc=q["btc_usd"] if q else None)
    return {"ok": True, "tiers": tiers,
            "quote": {"btc_usd": q["btc_usd"], "age_s": q["age_seconds"],
                      "source": q["source"]} if q else None,
            "valid_seconds": body.get("invoice_valid_seconds", 3600)}


def _tip_create(_h, data):
    from sources import agenthc_day_trade_ideas as hc
    n = (data or {}).get("amount_usd")
    if type(n) is not int or n not in hc.TIP_TIERS_USD:
        return {"ok": False, "error": _TIP_PICK}
    if n not in _tip_offered["usd"]:
        return {"ok": False, "error": _TIP_NOT_OFFERED, "refresh_tiers": True}
    # Fail closed: without an independent price nothing bounds AgentHC's.
    market = _btc_usd()
    if not market > 0:
        return {"ok": False, "error": _TIP_NO_MARKET}
    offered = _tip_offered.get("btc")
    if offered and _tip_price_far(offered, market):
        return {"ok": False, "error": _TIP_PRICE_ODD, "refresh_tiers": True}
    try:
        body = hc.create_tip(n)
    except hc.TipRefused as exc:
        return {"ok": False, "error": str(exc), "refresh_tiers": exc.refresh}
    except hc.TipsUnavailable:
        return {"ok": False, "unavailable": True, "error": hc.TIPS_NOT_YET}
    except Exception as exc:
        return {"ok": False, "error": _net_error_text(exc)}
    # create_tip checked the invoice against AgentHC's own price; AgentHC may
    # have re-priced since /options, so check that price independently too.
    btc = float(body["btc_usd"])
    if _tip_price_far(btc, market):
        return {"ok": False, "error": _TIP_PRICE_ODD, "refresh_tiers": True}
    bolt11 = body["payment_request"].strip()
    return {"ok": True, "bolt11": bolt11, "qr": _qr_matrix(bolt11.upper()),
            "payment_hash": body["payment_hash"], "amount_usd": n,
            "amount_sats": body["amount_sats"], "btc_usd": btc,
            "expires_in": body.get("invoice_valid_seconds", 3600)}


def _tip_status_route(_h, data):
    from sources import agenthc_day_trade_ideas as hc
    h = (data or {}).get("payment_hash")
    if not isinstance(h, str) or not hc._HEX64.fullmatch(h):
        return {"ok": False, "error": "That invoice id doesn't look right."}
    try:
        st = hc.tip_status(h)
    except hc.TipStatusUnknown:
        # Never "expired" here: the invoice may still be payable.
        return {"ok": False, "retry": True, "unknown": True,
                "error": hc.TIP_STATUS_UNKNOWN}
    except Exception as exc:
        return {"ok": False, "retry": True, "error": _net_error_text(exc, cache_s=60)}
    status = st.get("status") if isinstance(st, dict) else None
    if status not in ("pending", "paid", "expired"):
        status = "pending"
    return {"ok": True, "status": status}


_PASS_INVOICE_S = 3600 + 300    # a pass invoice can be paid for 1h (+ slack)
_PASS_REUSE_S = 3000            # hand the same invoice back while 10+ min remain
_MONEY_LOCK = threading.RLock()


def _one_at_a_time(fn):
    """Pass and wallet requests run one after the other (the server answers
    each request on its own thread): the second click of a double click must
    see what the first one did, and a status check must not write back a
    list that a purchase changed in the meantime."""
    def run(*args, **kwargs):
        with _MONEY_LOCK:
            return fn(*args, **kwargs)
    run.__name__, run.__doc__ = fn.__name__, fn.__doc__
    return run


@_one_at_a_time
def _pass_buy(cfg, save, data):
    """Invoice for N prepaid signal days; the user pays from any wallet.

    Asking again never loses an earlier invoice: the same size (a double
    click) gets the invoice already made, and a different size is added
    next to it — every unpaid invoice stays on file (feed_pass_pending =
    the newest, feed_pass_extra = the others) until it is paid or expires,
    so whichever one the owner pays, the agent finds the pass."""
    from sources import agenthc_day_trade_ideas as hc
    import time as _t
    try:
        days = int(data.get("days") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "pick 1, 5, 20 or 60 days"}
    current = (cfg.get("feed_pass") or {}).get("token")
    extends = hc._token_id(current) if current else ""
    last = cfg.get("feed_pass_pending")
    if (isinstance(last, dict) and last.get("days") == days
            and last.get("extends") == extends
            and isinstance(last.get("invoice"), dict)
            and isinstance(last["invoice"].get("bolt11"), str)
            and _t.time() - float(last.get("bought_at") or 0) < _PASS_REUSE_S):
        return {**last["invoice"], "qr": _qr_matrix(last["invoice"]["bolt11"].upper())}
    try:
        body = hc.buy_pass(days, extend_token=current)
    except hc.PassSizeRefilling as exc:
        # Size briefly off sale (node refilling) — the wizard re-asks
        # which sizes are on sale so the card disappears until it's back.
        return {"ok": False, "error": str(exc), "refresh_sizes": True}
    except hc.PassSizeUnavailable as exc:
        return {"ok": False, "error": str(exc), "refresh_sizes": True}
    except hc.PassUnavailable as exc:
        return {"ok": False, "error": str(exc), "unavailable": True}
    except Exception as exc:
        return {"ok": False, "error": _net_error_text(exc)}
    bolt11 = body["payment_request"]
    reply = {"ok": True, "bolt11": bolt11,
             "amount_sats": body.get("amount_sats"),
             "amount_usd": body.get("amount_usd_approx"),
             "extends": bool(body.get("extends_current_pass")),
             "carried": body.get("carried_over_days") or 0,
             "access_until": body.get("access_until")}
    cfg["source"] = "agenthc"
    if isinstance(last, dict) and last.get("token"):
        # the earlier invoice may still get paid: keep watching its pass
        cfg["feed_pass_extra"] = [last] + [
            p for p in (cfg.get("feed_pass_extra") or []) if isinstance(p, dict)]
    cfg["feed_pass_pending"] = {
        "token": body["pass_token"], "days": days, "bought_at": _t.time(),
        "access_until": body.get("access_until"),
        "extends": extends, "invoice": reply}
    save()
    return {**reply, "qr": _qr_matrix(bolt11.upper())}


@_one_at_a_time
def _pass_status(cfg, save):
    """Every purchase on file is checked with AgentHC. A paid one becomes
    the pass (newest first); an unpaid one stays while its invoice can still
    be paid; a paid pass that isn't the main one stays until AgentHC says it
    is finished. Nothing paid for is ever dropped."""
    from sources import agenthc_day_trade_ideas as hc
    import time as _t
    usable = ("active", "exits_only")

    def _reply(st, status=None):
        return {"ok": True, "status": status or st.get("status"),
                "days_left": st.get("days_left", st.get("trading_days_left")),
                "access_until": st.get("access_until")}

    try:
        pend = cfg.get("feed_pass_pending")
        extra = [p for p in (cfg.get("feed_pass_extra") or [])
                 if isinstance(p, dict) and p.get("token")]
        unpaid = ([pend] if pend else []) + [p for p in extra if not p.get("paid")]
        spare = [p for p in extra if p.get("paid")]   # paid, not the main pass
        waiting, promoted = [], None
        for p in unpaid:
            st = hc.pass_status(p["token"])
            if st.get("status") in usable:
                paid = {k: v for k, v in p.items() if k != "invoice"}
                paid["access_until"] = st.get("access_until")
                if promoted is None:
                    promoted = (paid, st)
                else:
                    spare.insert(0, {**paid, "paid": True})
            elif (st.get("status") == "pending" and _t.time()
                  - float(p.get("bought_at") or 0) < _PASS_INVOICE_S):
                waiting.append(p)
            # else: expired unpaid / invalid — nothing there to keep
        spare = [p for p in spare
                 if hc.pass_status(p["token"]).get("status") in usable]
        reply = None
        if promoted:
            paid, st = promoted
            old = cfg.get("feed_pass")
            # Renewing replaces the pass it extends (its days moved over).
            # Any other pass still on file was paid for separately: keep it.
            if (isinstance(old, dict) and old.get("token") and "extends" in paid
                    and paid["extends"] != hc._token_id(old["token"])):
                spare.insert(0, {**old, "paid": True})
            cfg["feed_pass"] = paid
            reply = _reply(st, "active")
        new_pend = waiting[0] if waiting else None
        new_extra = waiting[1:] + spare
        if (reply or new_pend != pend
                or new_extra != (cfg.get("feed_pass_extra") or [])):
            for key, val in (("feed_pass_pending", new_pend),
                             ("feed_pass_extra", new_extra)):
                if val:
                    cfg[key] = val
                else:
                    cfg.pop(key, None)
            save()
        if reply:
            return reply
        if new_pend:
            return {"ok": True, "status": "pending"}
        cur = cfg.get("feed_pass")
        if not cur:
            return {"ok": True, "status": "unpaid" if pend else "none"}
        st = hc.pass_status(cur["token"])
        if st.get("status") not in usable and spare:
            # the main pass is finished: the other paid one takes over
            cfg["feed_pass"] = {k: v for k, v in spare[0].items() if k != "paid"}
            rest = waiting[1:] + spare[1:]
            if rest:
                cfg["feed_pass_extra"] = rest
            else:
                cfg.pop("feed_pass_extra", None)
            save()
            st = hc.pass_status(cfg["feed_pass"]["token"])
        return _reply(st)
    except Exception as exc:
        return {"ok": False, "status": "error", "error": _net_error_text(exc, cache_s=60)}


_WALLET_EXISTS = ("Your agent already has a wallet, so no new one was created "
                  "— a new wallet starts empty, and any sats in the one you "
                  "have would be left behind. To keep using it, just add "
                  "sats to it. To replace it anyway (for example because "
                  "that wallet no longer exists): run `python agent.py "
                  "setup` in the agent's folder and type REPLACE when it "
                  "asks about the wallet. The old wallet's details are "
                  "saved to a file first.")


@_one_at_a_time
def _wallet_create(cfg, save, data, switching=None):
    """Create the agent's own wallet. Never replaces one that is already set
    up unless the request says {"replace": true} — and then the old wallet's
    keys are saved to a file in the agent's private folder before the agent
    switches, so sats left in it can still be moved out. No file, no
    replacement. The file is only written once the new wallet exists: a
    failed try leaves no file calling a wallet "replaced" that is still the
    agent's. switching: for /v2, a context the switch (old-wallet file,
    keys, save) runs inside once LNbits has answered; it may raise to stop
    it, and then the new wallet is empty and simply goes unused."""
    import contextlib
    import lightning_wallet
    had = bool(cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"))
    if had and (data or {}).get("replace") is not True:
        # "needs_replace": the React wizard's name for the same refusal
        return {"ok": False, "exists": True, "needs_replace": True,
                "error": _WALLET_EXISTS,
                "page": cfg.get("lnbits_wallet_page", "")}
    old_page, old_file = cfg.get("lnbits_wallet_page", ""), ""
    try:
        url, key, page = lightning_wallet.create_wallet()
    except lightning_wallet.WalletError as exc:
        return {"ok": False, "error": str(exc)[:200]
                + (" Your current wallet was not changed." if had else "")}
    with (switching() if switching else contextlib.nullcontext()):
        if had:
            try:
                old_file = lightning_wallet.keep_old_wallet(cfg)
            except OSError as exc:
                # the new wallet is empty and simply goes unused
                return {"ok": False, "error":
                        "Your wallet was not replaced: its details could not be "
                        f"saved first ({str(exc)[:120]}). Nothing changed — "
                        "your agent still uses the wallet it had."}
        cfg["source"] = "agenthc"
        cfg["lnbits_url"] = url
        cfg["lnbits_admin_key"] = key
        cfg.pop("lnbits_wallet_page", None)   # never show the old wallet's page
        if page:
            cfg["lnbits_wallet_page"] = page
        cfg.setdefault("max_autopay_sats", 30_000)
        save()
    out = {"ok": True, "page": page, "warning": lightning_wallet.demo_warning(url)}
    if had:
        out.update({
            "old_wallet_file": old_file, "old_wallet_page": old_page,
            "note": "New wallet created. It starts empty. Your old wallet "
                    "was not deleted: if it still holds sats, open it"
                    + (f" ({old_page})" if old_page else "")
                    + " and send them to the new one. The old wallet's "
                    f"details are saved in {old_file}."})
    return out


def _use_pasted_wallet(cfg, url, new_key):
    """Point the agent at an LNbits wallet the owner pasted (address + admin
    key). When that is a different wallet from the one the agent has, the
    old one's details are saved to a file in the agent's private folder
    first — sats may be left in it. Returns "" when done, or the reason
    nothing was changed (the old details could not be saved)."""
    if (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")
            and cfg["lnbits_admin_key"] != new_key):
        import lightning_wallet
        try:
            lightning_wallet.keep_old_wallet(cfg)
        except OSError as exc:
            return ("Your wallet was not changed: the current wallet's "
                    f"details could not be saved first ({str(exc)[:120]}).")
        cfg.pop("lnbits_wallet_page", None)  # the old wallet's page
    cfg["lnbits_url"] = url
    cfg["lnbits_admin_key"] = new_key
    cfg.setdefault("max_autopay_sats", 30_000)
    return ""


def _set_recurring(cfg, save, on):
    """Switch 'Recurring day-pass' on or off. Also notes when it was last
    switched ON: that is the owner's "try again" for a wallet the agent
    stopped buying with because it paid without giving a receipt (see
    _no_receipt_stop in sources/agenthc_day_trade_ideas.py)."""
    cfg["day_pass_recurring"] = bool(on)
    if cfg["day_pass_recurring"]:
        import time
        cfg["day_pass_recurring_on_at"] = time.time()
    save()
    return {"ok": True, "on": cfg["day_pass_recurring"]}


def _restore_notes(cfg, acceptance=None):
    """Per-tab 'already configured' summaries for the wizard restore path.
    Non-secret only — these render as plain text at the top of each completed
    tab so a returning user can see WHAT is configured, not just a checkmark."""
    notes = {}
    if acceptance and acceptance.get("accepted"):
        when = str(acceptance.get("accepted_at", ""))[:10]
        notes["consent"] = ("Agreement signed"
                            + (f" on {when}" if when else "") + " — on file")
    import llm_client
    if llm_client.configured(cfg):
        notes["llm"] = "AI connected — " + llm_client.describe(cfg)
    src = cfg.get("source")
    if src:
        detail = ""
        if src == "agenthc":
            if cfg.get("agenthc_api_key"):
                detail = " · API key on file"
            elif cfg.get("lnbits_url"):
                detail = " · Lightning wallet connected"
            if cfg.get("include_other_trades"):
                detail += " · main + other trades"
        elif src == "url" and cfg.get("source_url"):
            detail = f" · {cfg['source_url']}"
        notes["source"] = f"Source saved: {src}{detail}"
    def _book_note(prefix):
        mode = cfg.get(f"{prefix}sizing_mode")
        if mode == "budget":
            return f"up to ${cfg.get(f'{prefix}budget_per_trade_usd', 0):g} per trade"
        if mode == "percent":
            return f"{cfg.get(f'{prefix}percent_per_trade', 0):g}% of account per trade"
        n = cfg.get(f"{prefix}contracts_per_trade", 1)
        return f"{n} contract{'s' if n != 1 else ''} per trade"
    if cfg.get("sizing_mode"):
        notes["sizing"] = "Sizing saved: " + _book_note("")
        if cfg.get("other_sizing_mode"):
            notes["sizing"] += " · shadow book " + _book_note("other_")
    if cfg.get("max_entries_per_day") is not None:
        notes["safety"] = (
            f"Safety saved: {'DRY-RUN' if cfg.get('dry_run', True) else 'LIVE'}"
            f" · max {cfg.get('max_entries_per_day')} entries/day")
    return notes


_SZ_VALUE_KEY = {"budget": "budget_per_trade_usd",
                 "contracts": "contracts_per_trade",
                 "percent": "percent_per_trade"}


def _sizing_detail(cfg):
    """Saved sizing per book, for the wizard to pre-fill its form."""
    def book(prefix):
        mode = cfg.get(f"{prefix}sizing_mode")
        if mode not in _SZ_VALUE_KEY:
            return None
        return {"mode": mode, "value": cfg.get(f"{prefix}{_SZ_VALUE_KEY[mode]}", "")}
    return {"main": book(""), "other": book("other_")}


def _reuse_saved_llm(cfg, pid, key, base):
    """No new key typed: reuse the connected one so the owner can switch to
    any model that key can use without pasting it again. The saved key only
    goes back to its own provider and endpoint — never to a different
    provider or a URL typed into the form. Returns (provider, key, base)."""
    import llm_client
    if key:
        return pid, key, base
    saved = llm_client.resolve(cfg)
    if not saved:
        return pid, key, base
    if pid and pid != saved["provider"]:
        return pid, key, base
    preset_base = llm_client.PROVIDERS[saved["provider"]]["base_url"].rstrip("/")
    if base.rstrip("/") not in ("", saved["base_url"].rstrip("/"), preset_base):
        return pid, key, base
    return saved["provider"], saved["key"], cfg.get("llm_base_url", "")


def _policy_save_error(cfg, policy, key):
    """Safety step: refuse a trading policy that no AI could ever check.
    With a policy and no AI, the agent skips EVERY entry (fail-safe), which
    looks to the owner like the agent silently never trades."""
    import llm_client
    if not str(policy or "").strip():
        return None
    if llm_client.configured(cfg):
        return None
    if str(cfg.get("llm_fallback", "skip")).lower() == "act":
        return None
    key = str(key or "").strip()
    if key and llm_client.detect_provider(key) == "anthropic":
        return None
    if key:
        return ("That key isn't a Claude key. Connect other AI providers in "
                "the AI step (Settings → AI, optional), then save again.")
    return ("A trading policy needs a connected AI to check it. Without one, "
            "every entry would be skipped and the agent would never trade. "
            "Connect your AI in the AI step first, or clear the rules box. "
            "(The built-in rules in Safety need no AI.)")


# ── one implementation per setting ───────────────────────────────────────────
# The Safety step's pieces, shared by the classic /api/safety route and the
# /v2 settings routes, so the same values always make the same config.json.

def _apply_entry_cap(cfg, raw_cap, seeded):
    """The daily entry cap from a form value. Not a number: keep what is
    set. Only a form filled in from the current settings (`seeded`) may
    RAISE the cap: a Safety page still open from before an update sends its
    own default (5), not your cap. Never set before: 5."""
    raw_cap = str(raw_cap or "").strip()
    try:
        cap = max(1, int(float(raw_cap))) if raw_cap else None
    except (TypeError, ValueError, OverflowError):
        cap = None  # not a number: keep what is set
    old_cap = cfg.get("max_entries_per_day")
    if (cap is not None and seeded is not True
            and isinstance(old_cap, int) and cap > old_cap):
        cap = None
    if cap is not None:
        cfg["max_entries_per_day"] = cap
    elif old_cap is None:
        cfg["max_entries_per_day"] = 5


def _apply_daily_loss(cfg, raw_loss):
    """The daily loss limit from a form value; blank keeps any existing
    limit (re-running the wizard), 0 = off."""
    raw_loss = str(raw_loss or "").strip()
    if raw_loss:
        try:
            cfg["max_daily_loss_usd"] = max(
                0.0, float(raw_loss.lstrip("$").replace(",", "")))
        except ValueError:
            pass


# (form field, config.json key) of each alert target
_ALERT_KEYS = (("discord", "discord_webhook_url"), ("ntfy", "ntfy_topic"),
               ("tg_token", "telegram_bot_token"), ("tg_chat", "telegram_chat_id"))


def _apply_alert_targets(cfg, data):
    """Alert targets from a form: a blank field keeps what is set."""
    for src_key, cfg_key in _ALERT_KEYS:
        v = str(data.get(src_key, "")).strip()
        if v:
            cfg[cfg_key] = v


def _apply_keep_awake(cfg, data):
    if "keep_awake" in data:
        cfg["keep_awake"] = bool(data.get("keep_awake"))


def _write_policy_file(policy):
    """The owner's written trading policy (plain English), as the policy
    brain reads it."""
    import llm_policy
    path = llm_policy.policy_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(policy + "\n")


# ── /v2 settings API: typed payloads and the two-step confirmation ──────────
# A money-affecting /v2 write is never applied by its first POST: that one
# answers with a preview and a token. Only the same payload sent again with
# that token (and nothing changed in the settings meanwhile) is applied.

_V2_CONFIRM_S = 120
_V2_RESERVED = ("confirm", "acknowledge")
# how the last Robinhood sign-in ended: none yet (GET /api/v2/settings)
_RH_SIGNIN_NONE = {"seq": 0, "connected": None, "account_mask": None,
                   "options_off": False, "error": None}
_REVIEW_AGAIN = "Your settings changed since you reviewed this. Review again."
_TOKEN_SPENT = ("That confirmation has expired or was already used. Nothing "
                "was changed. Review again.")
_TOKEN_OTHER = ("That confirmation is for a different change than this one. "
                "Nothing was changed. Review again.")


def _clock():
    import time
    return time.time()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


class _Confirmations:
    """Single-use confirmation tokens. A token is
    "<expiry>.<nonce>.<HMAC(key, route + payload + fingerprint + expiry +
    nonce)>" under a random key made when the server starts; the server
    keeps each open nonce with the settings fingerprint it was issued
    against, and any attempt to use one spends it."""

    MAX_OPEN = 500

    def __init__(self):
        import secrets
        self.key = secrets.token_bytes(32)
        self._open = {}  # nonce -> (expiry, fingerprint)
        self._lock = threading.Lock()

    def _mac(self, route, payload, fingerprint, expiry, nonce):
        import hashlib
        import hmac
        msg = _canonical([route, payload, fingerprint, expiry, nonce])
        return hmac.new(self.key, msg.encode(), hashlib.sha256).hexdigest()

    def issue(self, route, payload, fingerprint):
        import secrets
        now = _clock()
        expiry = int(now) + _V2_CONFIRM_S
        nonce = secrets.token_hex(12)
        with self._lock:
            for n in [n for n, (exp, _fp) in self._open.items() if exp < now]:
                del self._open[n]
            while len(self._open) >= self.MAX_OPEN:
                del self._open[next(iter(self._open))]
            self._open[nonce] = (expiry, fingerprint)
        return f"{expiry}.{nonce}.{self._mac(route, payload, fingerprint, expiry, nonce)}"

    def redeem(self, token, route, payload, fingerprint_now):
        """None when the token confirms exactly this change (it is spent
        now), else what to tell the owner. Nothing may be saved then."""
        import hmac
        parts = token.split(".") if isinstance(token, str) else []
        if len(parts) != 3 or not parts[0].isdigit():
            return _TOKEN_SPENT
        expiry, nonce, mac = int(parts[0]), parts[1], parts[2]
        with self._lock:
            rec = self._open.pop(nonce, None)  # single use: any try spends it
        if rec is None or rec[0] != expiry or _clock() > expiry:
            return _TOKEN_SPENT
        if not hmac.compare_digest(
                mac, self._mac(route, payload, rec[1], expiry, nonce)):
            return _TOKEN_OTHER
        if not hmac.compare_digest(rec[1], fingerprint_now):
            return _REVIEW_AGAIN
        return None


class _Invalid(Exception):
    """A /v2 payload that is refused as a whole (nothing is saved)."""


def _v2_payload(data, allowed):
    """The payload without the confirmation fields; refuses unknown keys."""
    if not isinstance(data, dict):
        raise _Invalid("Send the settings as a JSON object.")
    extra = sorted(k for k in data if k not in allowed and k not in _V2_RESERVED)
    if extra:
        raise _Invalid(f"Unknown setting: {', '.join(map(str, extra))[:120]}. "
                       "Nothing was changed.")
    return {k: v for k, v in data.items() if k not in _V2_RESERVED}


def _v2_number(v, name, low, high, whole=False, low_open=False):
    """A finite JSON number in [low, high] ((low, high] with low_open)."""
    import math
    import numbers
    kind = "a whole number" if whole else "a number"
    if (isinstance(v, bool) or not isinstance(v, numbers.Real)
            # a JSON whole number is finite however long (math.isfinite
            # would raise OverflowError past a float's range)
            or not (isinstance(v, int) or math.isfinite(v))
            or (whole and v != int(v))):
        raise _Invalid(f"{name} must be {kind}. Nothing was changed.")
    if (v <= low if low_open else v < low) or v > high:
        rng = (f"more than {low:g} and at most {high:,g}" if low_open
               else f"from {low:g} to {high:,g}")
        raise _Invalid(f"{name} must be {rng}. Nothing was changed.")
    return int(v) if whole else float(v)


def _v2_text(v, name, longest=500):
    if not isinstance(v, str) or not v.strip():
        raise _Invalid(f"{name} must be filled in (send null to clear it). "
                       "Nothing was changed.")
    if len(v) > longest or any(ord(ch) < 32 for ch in v):
        raise _Invalid(f"{name} is not a valid value. Nothing was changed.")
    return v.strip()


# What a /v2 limits save accepts (low, high) — the classic form clamps or
# ignores what is outside; /v2 refuses it and says the range.
_V2_LIMIT_RANGES = {"max_entries_per_day": (1, 1000),
                    "max_daily_loss_usd": (0, 1_000_000_000),  # 0 = no limit
                    "max_open_positions": (1, 1000)}
# trade size: mode -> (low, high, whole number, name); budget and percent
# must be more than `low`, contracts at least `low`
_V2_SIZE_RANGES = {"budget": (0, 10_000_000, False, "dollars per trade"),
                   "contracts": (1, 100_000, True, "contracts per trade"),
                   "percent": (0, 100, False, "percent of the account")}


def _secret(value, show_last4=True):
    """How a secret appears in a GET: whether it is set, and at most its
    last four characters (none for a short one or a password)."""
    s = str(value or "")
    return {"set": bool(s),
            "last4": s[-4:] if show_last4 and len(s) >= 12 else None}


# The settings a /v2 preview depends on: the fingerprint changes whenever
# one of them does. Raw values, secrets included — the fingerprint is an
# HMAC under the server's own random key, so it gives none of them away.
_FP_KEYS = (
    "sizing_mode", "budget_per_trade_usd", "contracts_per_trade",
    "percent_per_trade", "max_contracts_per_trade", "other_sizing_mode",
    "other_budget_per_trade_usd", "other_contracts_per_trade",
    "other_percent_per_trade", "other_max_contracts_per_trade",
    "max_entries_per_day", "max_daily_loss_usd", "max_open_positions",
    "dry_run", "paused", "rule_tickers", "rule_skip_tickers", "rule_sides",
    "rule_min_strike", "rule_skip_0dte", "rule_loss_streak",
    "discord_webhook_url", "ntfy_topic", "telegram_bot_token",
    "telegram_chat_id", "keep_awake", "llm_provider", "llm_api_key",
    "llm_model", "llm_base_url", "anthropic_api_key", "llm_fallback",
    "broker", "robinhood_account", "source", "include_other_trades",
    "day_pass_recurring", "lnbits_url", "lnbits_admin_key",
    "agenthc_api_key", "max_autopay_sats")


def _broker_field_keys():
    try:
        from brokers import key_brokers
        return [f["id"] for w in key_brokers() for f in w.get("fields", [])
                if isinstance(f, dict) and f.get("id")]
    except Exception:
        return []


def _settings_fingerprint(cfg, key):
    import hashlib
    import hmac
    import llm_policy
    keys = sorted(set(_FP_KEYS) | set(_broker_field_keys()))
    picked = {k: cfg[k] for k in keys if k in cfg}
    msg = _canonical({"cfg": picked, "policy": llm_policy.policy_text()})
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:32]


def _risk_direction(old, new, none_is_unlimited=False):
    """raises_risk / lowers_risk / neutral for a bigger-is-riskier number.
    none_is_unlimited: None or 0 means no limit at all (the loss limit)."""
    def eff(v):
        if none_is_unlimited and not v:
            return float("inf")
        return v
    a, b = eff(old), eff(new)
    if a is None or b is None or a == b:
        return "neutral"
    return "raises_risk" if b > a else "lowers_risk"


def _book_size(cfg, prefix):
    """One book's saved trade size as {mode, value}; None when not set (for
    the other-trades book: it sizes the same as the main pick)."""
    mode = cfg.get(f"{prefix}sizing_mode")
    if mode not in _SZ_VALUE_KEY:
        return None
    return {"mode": mode, "value": cfg.get(f"{prefix}{_SZ_VALUE_KEY[mode]}")}


def _hhmm(mins):
    """Minutes after midnight as "HH:MM"; None (off) stays None."""
    return None if mins is None else f"{mins // 60:02d}:{mins % 60:02d}"


def _expiry_direction(old, new):
    """The expiry-day sell time ("HH:MM" or None = off): switching it off
    raises the risk (an expiring contract is then sold only by an exit
    signal, else exercised or left to expire), switching it on lowers it;
    a later time holds the contract longer into its last day."""
    if old == new:
        return "neutral"
    if new is None:
        return "raises_risk"
    if old is None:
        return "lowers_risk"
    return "raises_risk" if new > old else "lowers_risk"


def _size_direction(old, new):
    if old and new and old["mode"] == new["mode"]:
        return _risk_direction(old.get("value"), new.get("value"))
    return "neutral"  # a different kind of size can't be compared


def _broker_direction(before, after):
    """A broker change's risk: to a real-money account raises it, to a
    paper-trading one (from a broker) lowers it; same kind, or not known,
    can't be compared."""
    if after["paper"] is None or before["paper"] == after["paper"]:
        return "neutral"
    if after["paper"]:
        return "lowers_risk" if before["id"] else "neutral"
    return "raises_risk"


# The dashboard "/" lands on (config key "dashboard"): classic unless the
# owner chose the new one in /v2 Settings → This dashboard.
_DASHBOARDS = ("classic", "v2")


def _dashboard_pref(cfg):
    """The saved landing choice; "classic" when absent or not a known one."""
    v = cfg.get("dashboard") if isinstance(cfg, dict) else None
    return v if v in _DASHBOARDS else "classic"


# /setup?from=<one of these>: where the wizard's "Dashboard" links and its
# hop after "Start my agent" go. A fixed list: never an open redirect.
_RETURN_TO = {"v2": "/v2", "dash": "/dash"}


def _return_to(raw_path):
    """The allowed return page a /setup address asks for, or None."""
    from urllib.parse import parse_qs, urlparse
    try:
        vals = parse_qs(urlparse(raw_path).query).get("from") or []
    except ValueError:
        return None
    return _RETURN_TO.get(vals[0]) if len(vals) == 1 else None


def _wizard_page(html, raw_path):
    """The built-in setup wizard, with its way back to the dashboard the
    owner came from (?from=v2): unchanged without it."""
    to = _return_to(raw_path)
    if to is None or to == "/dash":
        return html
    return (html.replace('href="/dash"', f'href="{to}"')
            .replace("location='/dash'", f"location='{to}'"))


def _broker_display(cfg, last4=""):
    """What the wizard's broker step shows as connected: the in-session hint,
    else the Robinhood account last4, else the persisted key-based broker id
    (alpaca/moomoo/… write cfg["broker"] on connect). Without that last
    fallback, a saved key-based connection looked unconfigured on every new
    wizard session and re-locked the steps after it."""
    if last4:
        return last4
    if cfg.get("robinhood_account"):
        return cfg["robinhood_account"][-4:]
    return cfg.get("broker", "")


def start_app(get_status=None, get_trades=None, apply_command=None,
              cfg_getter=None, wait_finish=False, get_view=None,
              get_activity=None):
    """One server, one port: /setup (wizard) + /dash (dashboard) + all APIs.
    get_view / get_activity: the running agent's view-model and its pages of
    older activity (agent.cmd_run), served at /api/v2/status and
    /api/v2/activity; without them those routes answer from the files, as
    for a stopped agent."""
    import agent as A
    from brokers.robinhood import RobinhoodMCP, _token_path, content_json
    from lightning_wallet import LNbitsWallet, WalletError

    pending = {"oauth": None, "broker_last4": "", "rh_warning": "",
               "finished": threading.Event(),
               # where the browser lands after the Robinhood sign-in: "/"
               # (classic), or "/v2" for a sign-in started from /v2
               "oauth_return": None,
               # how the last Robinhood sign-in ended, for /v2 to watch
               # (seq moves with every finished sign-in, even one that
               # changed nothing: the same account again, or none allowed)
               "rh_signin": dict(_RH_SIGNIN_NONE)}

    def _rh_signin_done(connected, account="", options_off=False, error=None):
        pending["rh_signin"] = {
            "seq": pending["rh_signin"]["seq"] + 1, "connected": connected,
            "account_mask": account[-4:] or None, "options_off": options_off,
            "error": error}

    # config.json is shared with the chat commands and the dashboard buttons,
    # which write it too (agent._apply_command). `cfg` is only this server's
    # working copy of it: brought up to date from the file when a request
    # starts, and save() writes back ONLY the keys this server changed, onto
    # the file as it is at that moment, under the lock the commands use.
    # (Saving the whole copy undid the owner's commands: a settings save
    # after "dry on" put the agent back in LIVE mode.)
    # What a request changed in the copy and did not save is taken out again
    # when that request ends, so a rejected form (a broker whose keys were
    # refused, a feed address that was not accepted) can never ride along
    # with the next save — not even one made by a request that was running
    # at the same time.
    import copy
    touched = {}  # request thread -> the keys it has changed in the copy
    _gone = object()

    class _WorkingCopy(dict):
        """A dict that notes which request changed which key."""

        def _note(self, *keys):
            mine = touched.get(threading.get_ident())
            if mine is not None:
                mine.update(keys)

        def __setitem__(self, k, v):
            self._note(k)
            dict.__setitem__(self, k, v)

        def __delitem__(self, k):
            self._note(k)
            dict.__delitem__(self, k)

        def pop(self, k, *default):
            self._note(k)
            return dict.pop(self, k, *default)

        def setdefault(self, k, default=None):
            self._note(k)
            return dict.setdefault(self, k, default)

        def update(self, *a, **kw):
            new = dict(*a, **kw)
            self._note(*new)
            dict.update(self, new)

        def __ior__(self, other):
            self.update(other)
            return self

        def clear(self):
            self._note(*self)
            dict.clear(self)

    on_file = A._load(A.CONFIG_PATH, {})
    cfg = _WorkingCopy(on_file if isinstance(on_file, dict) else {})
    synced = {"cfg": copy.deepcopy(dict(cfg))}  # config.json as of the last sync

    def _own_changes():
        """What this server changed in its copy and hasn't saved yet:
        ({key: value}, [removed keys])."""
        base = synced["cfg"]
        changed = {k: v for k, v in list(cfg.items())
                   if k not in base or base[k] != v}
        return changed, [k for k in list(base) if k not in cfg]

    def _put(k, src):
        """cfg[k] = what `src` has for it (no note: not a request's change)."""
        if k in src:
            dict.__setitem__(cfg, k, copy.deepcopy(src[k]))
        else:
            dict.pop(cfg, k, None)

    def _pull(disk, keep_own=True):
        """Bring `cfg` up to date with config.json (`disk`), one key at a
        time so another request never sees it empty. keep_own: leave this
        server's unsaved changes in place (a request is still working on
        them); False drops them all (a whole new config was imported)."""
        base = synced["cfg"]
        if keep_own:
            changed, removed = _own_changes()
            keys = [k for k in set(base) | set(disk)
                    if base.get(k, _gone) != disk.get(k, _gone)
                    and k not in changed and k not in removed]
        else:
            keys = [k for k in set(cfg) | set(disk)
                    if cfg.get(k, _gone) != disk.get(k, _gone)]
        for k in keys:
            _put(k, disk)
        synced["cfg"] = copy.deepcopy(disk)

    def _drop_unsaved(mine):
        """A request is over: what it changed in the copy and did not save
        goes back to what is on file. Keys another request is still working
        on are left to that request; with no other request running, nothing
        unsaved stays at all."""
        base = synced["cfg"]
        if touched:
            busy = set().union(*touched.values())
            keys = [k for k in mine if k not in busy]
        else:
            changed, removed = _own_changes()
            keys = list(changed) + removed
        for k in keys:
            if cfg.get(k, _gone) != base.get(k, _gone):
                _put(k, base)

    def _setup_restart_from():
        """Setup wizard only, with no agent running on this computer: the
        settings to start again from when config.json can't be read (a
        typo) — the ones this wizard last read. The damaged file is kept
        next to the new one. A running agent never does this (None): its
        settings pages refuse to save until the file is fixed."""
        if get_status is not None:
            return None
        try:
            import keep_running
            if keep_running.is_running():
                return None
        except Exception:
            return None
        return copy.deepcopy(synced["cfg"])

    def save():
        """Write what THIS request changed in the copy onto config.json as
        it is now. Another request's unfinished changes are not written."""
        mine = touched.get(threading.get_ident())

        def own_keys_only(disk):
            changed, removed = _own_changes()
            for k in removed:
                if mine is None or k in mine:
                    disk.pop(k, None)
            disk.update(copy.deepcopy({k: v for k, v in changed.items()
                                       if mine is None or k in mine}))
        with A.CONFIG_LOCK:
            _pull(A.update_config(own_keys_only, if_damaged=_setup_restart_from))

    def _fresh(fn, plain_errors=True):
        """A route that starts from the settings as they are on disk now,
        and leaves nothing unsaved behind in the copy when it ends."""
        def run(*args):
            me = threading.get_ident()
            if me in touched:  # already inside a request on this thread
                return fn(*args)
            with A.CONFIG_LOCK:
                touched[me] = set()
                try:
                    _pull(A._read_config())
                except OSError:
                    pass  # unreadable right now (locked, or a typo): answer
                    # from the copy; a save in this request is refused
            try:
                return fn(*args)
            except A.ConfigUnreadable as exc:
                if not plain_errors:
                    raise
                # a save was refused: say why, in full (nothing was changed)
                return {"ok": False, "error": str(exc)}
            finally:
                with A.CONFIG_LOCK:
                    _drop_unsaved(touched.pop(me, set()))
        return run

    class W(_Handler):
        pages = {"/setup": _WIZARD_HTML, "/dash": _DASH_HTML}
        landing = "/dash" if get_status else "/setup"
        v2 = True
        routes_get, routes_post = {}, {}

        def _remembered(self):
            try:
                saved = _dashboard_pref(A._read_config())
            except Exception:
                return "classic"  # unreadable now: the classic landing
            # no /v2 in this build: the classic one, whatever was chosen
            return saved if saved == "classic" or _app_v2() is not None \
                else "classic"

    def state(_h):
        return {
            "disclaimer": A._disclaimer_text(),
            "consent": A.consent_ok(),
            "source": cfg.get("source"),
            "broker": _broker_display(cfg, pending["broker_last4"]),
            "rh_warning": pending["rh_warning"],
            "llm": __import__("llm_client").configured(cfg),
            "policy_blocks_entries": __import__("llm_policy").entries_blocked(cfg),
            "sizing": bool(cfg.get("sizing_mode")),
            "sizing_detail": _sizing_detail(cfg),
            "include_other_trades": bool(cfg.get("include_other_trades")),
            "safety": cfg.get("max_entries_per_day") is not None,
            "notes": _restore_notes(cfg, A._load(A.ACCEPTANCE_PATH, {})),
            "wallet": {
                "connected": bool(cfg.get("lnbits_url")
                                  and cfg.get("lnbits_admin_key")),
                "page": cfg.get("lnbits_wallet_page", ""),
            },
            "recurring": bool(cfg.get("day_pass_recurring")),
            "pass": bool(cfg.get("feed_pass") or cfg.get("feed_pass_pending")),
            "autostart": __import__("keep_running").autostart_enabled(),
            "rules": {k: cfg.get(k) for k in (
                "rule_tickers", "rule_skip_tickers", "rule_sides",
                "rule_min_strike", "rule_skip_0dte", "rule_loss_streak")},
            "rules_summary": __import__("rules").describe(cfg),
            "policy_on": __import__("llm_policy").enabled(cfg),
            # the agent itself is serving this page (vs. the setup wizard)
            "running": get_status is not None,
            "keep_awake": cfg.get("keep_awake", True) is not False,
            # the mode and limits as they are set now (None = never set), so
            # the Safety form shows them instead of its own defaults
            "dry_run": (bool(cfg["dry_run"]) if cfg.get("dry_run") is not None
                        else None),
            "max_entries_per_day": cfg.get("max_entries_per_day"),
            "max_daily_loss_usd": cfg.get("max_daily_loss_usd"),
        }

    def consent(_h, data):
        phrase = "I AGREE AND ACCEPT ALL LIABILITY"
        if str(data.get("phrase", "")).strip() != phrase:
            return {"ok": False, "error": "phrase does not match exactly"}
        import hashlib
        from datetime import datetime, timezone
        A._save(A.ACCEPTANCE_PATH, {
            "accepted": True,
            "terms_version": A.TERMS_VERSION,
            "disclaimer_sha256": hashlib.sha256(
                A._disclaimer_text().encode()).hexdigest(),
            "accepted_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds"),
            "via": "web_wizard",
        })
        return {"ok": True}

    def source(_h, data):
        return _source_save(data, save)

    def _source_save(data, save_now):
        """The classic /api/source. save_now: save(), or for /v2 a save that
        first checks nothing changed since the preview."""
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        src = data.get("source")
        if src not in ("agenthc", "manual", "url"):
            return {"ok": False, "error": "unknown source"}
        cfg["source"] = src
        note = "saved ✓"
        if src == "url":
            url = str(data.get("source_url", "")).strip()
            if not (url.startswith("https://") or url.startswith("http://127.0.0.1")
                    or url.startswith("http://localhost")):
                return {"ok": False, "error": "feed URL must be https:// (or localhost)"}
            cfg["source_url"] = url
        if src == "agenthc":
            cfg["include_other_trades"] = data.get("scope") == "all"
            if data.get("agenthc_key"):
                cfg["agenthc_api_key"] = str(data["agenthc_key"]).strip()
                note = "API key saved ✓"
            elif data.get("lnbits_url") and data.get("lnbits_key"):
                url = str(data["lnbits_url"]).strip()
                if not (url.startswith("https://") or url.startswith("http://127.0.0.1")
                        or url.startswith("http://localhost")):
                    return {"ok": False, "error": "wallet URL must be https://"}
                try:
                    bal = LNbitsWallet(url, str(data["lnbits_key"]).strip()).balance_sats()
                except WalletError as exc:
                    return {"ok": False, "error": f"wallet check failed: {exc}"}
                # a different wallet replaces the agent's: the old one's
                # keys are kept on file first (sats may be left in it)
                problem = _use_pasted_wallet(
                    cfg, url, str(data["lnbits_key"]).strip())
                if problem:
                    return {"ok": False, "error": problem}
                note = f"wallet connected ✓ balance {bal:,} sats"
            elif cfg.get("feed_pass"):
                note = "prepaid pass ✓"
            elif cfg.get("feed_pass_pending"):
                note = ("saved ✓ — the agent starts reading signals once your "
                        "pass invoice is paid")
            elif cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
                note = "using the wallet created above ✓"
            else:
                return {"ok": False,
                        "error": "buy a pass above (or, under Advanced, set up a "
                                 "wallet or API key)"}
        save_now()
        return {"ok": True, "note": note}

    def llm_providers(_h):
        import llm_client
        return {"providers": {pid: {"name": p["name"], "base_url": p["base_url"],
                                    "keys_url": p["keys_url"]}
                              for pid, p in llm_client.PROVIDERS.items()}}

    def _llm_target(data):
        import llm_client
        key = str(data.get("key", "")).strip()
        pid = str(data.get("provider", "")).strip()
        base = str(data.get("base_url", "")).strip()
        pid, key, base = _reuse_saved_llm(cfg, pid, key, base)
        pid = pid or llm_client.detect_provider(key)
        if not pid:
            return None, key, base, ("couldn't tell which provider that key is "
                                     "for — pick it under Provider")
        if pid not in llm_client.PROVIDERS:
            return None, key, base, "unknown provider"
        if not key and not llm_client.PROVIDERS[pid].get("no_key"):
            return None, key, base, "paste an API key first"
        if base and not llm_client.safe_base_url(base):
            return None, key, base, ("the endpoint must be https:// (or this "
                                     "computer: http://localhost)")
        if pid == "custom" and not base:
            return None, key, base, "enter the endpoint URL for a custom provider"
        return pid, key, base, None

    def llm_models(_h, data):
        import llm_client
        pid, key, base, err = _llm_target(data)
        if err:
            return {"ok": False, "error": err, "need_provider": pid is None}
        try:
            models = llm_client.list_models(pid, key, base or None)
        except llm_client.LLMError as exc:
            msg = {"auth": "the provider rejected that key — check it, or pick "
                           "the right provider under Provider",
                   "network": "couldn't reach the provider (check the URL / your "
                              "connection)"}.get(exc.reason, str(exc)[:160])
            return {"ok": False, "error": msg, "need_provider": exc.reason == "auth"}
        if not models:
            return {"ok": False, "error": "the provider listed no models for this key"}
        suggested = llm_client.suggest_model(pid, models)
        saved = llm_client.resolve(cfg) or {}
        if saved.get("provider") == pid and saved.get("model") in models:
            suggested = saved["model"]  # switching models: start from the current one
        return {"ok": True, "provider": pid, "models": models,
                "suggested": suggested}

    def llm(_h, data):
        import llm_client
        pid, key, base, err = _llm_target(data)
        if err:
            return {"ok": False, "error": err}
        model = str(data.get("model", "")).strip()
        if not model:
            return {"ok": False, "error": "pick a model"}
        trial = {"llm_provider": pid, "llm_api_key": key, "llm_model": model}
        if base:
            trial["llm_base_url"] = base
        try:  # one tiny real call: catches a typo'd model or a key without access
            llm_client.complete(trial, "Reply with the word OK.", "ping",
                                max_tokens=256, timeout=45)
        except llm_client.LLMError as exc:
            return {"ok": False, "error": f"that model didn't answer ({str(exc)[:140]})"}
        for k in ("llm_provider", "llm_api_key", "llm_model", "llm_base_url",
                  "anthropic_api_key"):
            cfg.pop(k, None)
        cfg.update(trial)
        cfg.setdefault("llm_fallback", "skip")
        save()
        return {"ok": True, "describe": llm_client.describe(cfg)}

    deploy_lock = threading.Lock()

    def deploy(_h, data):
        # One at a time. A second click while the first is still talking to
        # DigitalOcean (nothing is marked as moved yet) would create a second
        # server, and two servers place every order twice.
        if not deploy_lock.acquire(blocking=False):
            return {"ok": False, "error": (
                "a server is already being created from the first click — "
                "wait for it to finish. No second server was created.")}
        try:
            return deploy_once(data)
        finally:
            deploy_lock.release()

    def deploy_once(data):
        import provision
        import llm_policy
        if llm_policy.entries_blocked(cfg):
            return {"ok": False, "error": llm_policy.BLOCKED_MSG}
        token = str(data.get("token", "")).strip()
        if not token:
            return {"ok": False, "error": "paste a DigitalOcean API token"}
        import transfer
        moved = transfer.moved_away()
        if not moved and pending.get("deploy_unmarked"):
            moved = {"moved_at": "a moment ago"}  # marker couldn't be saved
        if moved:  # e.g. a second click: two servers would double every order
            return {"ok": False, "error": (
                "this setup was already moved to a server or another "
                f"computer ({moved.get('moved_at', '?')}). A second copy "
                "would place every order twice, so no new server was "
                "created. If that one is gone (destroyed or stopped), run: "
                "python agent.py resume-here — then deploy again.")}
        import keep_running
        running_here = get_status is not None
        if not running_here and keep_running.is_running():
            # This page can't hold an agent that runs in another process, so
            # it could trade while its memory is being copied to the server.
            return {"ok": False, "error": (
                "the agent is running on this computer right now, in the "
                "background or another window. Stop it first with: "
                "python agent.py stop — then press the button again. That "
                "way nothing can be traded here while its positions are "
                "being copied to the server. No server was created.")}
        # The server gets a copy of this computer's memory (positions, signals
        # already handled, today's counters). Put the heartbeat on hold —
        # "resume" can't lift this — and wait until the cycle it is in right
        # now (maybe an order being placed) has finished and saved, so
        # nothing is traded here that the server wouldn't know about.
        # A full stop: no entries, no exits, no expiry-guard sells (the
        # owner's own Pause, in config.json, would still sell on exits).
        # The dashboard shows it from the agent's status (agent._handed_off).
        transfer.HANDOFF["hold"] = True  # this is what stops the heartbeat

        def carry_on_here():
            transfer.HANDOFF["hold"] = False
        if not transfer.CYCLE_IDLE.wait(provision.CYCLE_WAIT_S):
            carry_on_here()
            return {"ok": False, "error": (
                "the agent is in the middle of handling a trade signal, so "
                "its positions can't be copied safely right now. No server "
                "was created and nothing changed. Wait a minute, then press "
                "the button again.")}
        first_try = not provision.deployed_before()
        try:
            droplet_id, msg = provision.create_droplet(
                token, str(data.get("region", "nyc3")))
        except Exception as exc:
            carry_on_here()  # nothing was created (or: not known)
            if first_try and not isinstance(exc, provision.OutcomeUnknown):
                provision.mark_deploy_checked()  # no earlier server to warn of
            if not isinstance(exc, RuntimeError):
                raise
            return {"ok": False, "error": str(exc)[:500]}
        # ── The server now runs this setup. From here on NOTHING may leave
        # this computer trading as well (two copies on one broker account
        # place every order twice), whatever else goes wrong. ──
        pending["deploy"] = (token, droplet_id)
        pending.pop("deploy_shown", None)
        provision.mark_deploy_checked()
        # Retire THIS computer exactly like "move to another computer" does.
        try:
            retired = transfer.mark_moved_away()
        except Exception as exc:
            retired = (
                "IMPORTANT: this computer could not save its \"moved\" note "
                f"({str(exc)[:120]}), so do NOT start the agent on this "
                "computer again while the server exists, and turn off "
                "start-at-login with: python agent.py autostart off")
            pending["deploy_unmarked"] = True  # still refuse a second server
            try:
                keep_running.disable_autostart()
            except Exception:
                pass
        if running_here:
            # It stays on hold (no orders) and stops by itself — not at once,
            # because this same process serves the page that is about to
            # show the owner the server's address and login command.
            def _stop_here():
                import time as _time
                end = _time.time() + provision.LOCAL_STOP_CAP_S
                while _time.time() < end and not CONTROLS["stop"]:
                    shown = pending.get("deploy_shown")
                    if (shown and _time.time() - shown
                            >= provision.LOCAL_STOP_AFTER_SHOWN_S):
                        break
                    _time.sleep(0.2)
                CONTROLS["stop"] = True
            threading.Thread(target=_stop_here, daemon=True).start()
            retired += ("; the agent here is on hold now (it places no more "
                        "orders, exits included, and \"resume\" is switched "
                        "off) and stops "
                        "by itself about a minute after this page shows the "
                        "server's address ("
                        f"{max(1, round(provision.LOCAL_STOP_CAP_S / 60))} "
                        "minutes from now at the latest) — this dashboard "
                        "stops answering then")
        else:
            carry_on_here()  # nothing runs in this process
        here = (f"This computer is now retired so the two can never both "
                f"trade: {retired}. Until the server sends its \"heartbeat "
                "started\" notification (about 5 minutes), nothing is "
                "watching your open positions. To run the agent on this "
                "computer again instead: destroy the server in DigitalOcean "
                "first, then run: python agent.py resume-here")
        note = f"{msg}. {here}"
        try:
            print(note)
            A.notify(cfg, "agentic-trader: " + note)
        except Exception:
            pass
        return {"ok": True, "note": note, "retired": True, "here": here}

    def deploy_status(_h):
        import provision
        if not pending.get("deploy"):
            return {"status": "no deploy in progress", "ip": ""}
        token, droplet_id = pending["deploy"]
        if not droplet_id:  # created, but DigitalOcean's reply was unreadable
            return {"status": "created — find its address under Droplets at "
                              "cloud.digitalocean.com", "ip": ""}
        st = provision.droplet_status(token, droplet_id)
        if st.get("ip"):
            st["ssh"] = provision.ssh_command(st["ip"])
            # the agent on this computer may stop once the page has this
            import time as _time
            pending.setdefault("deploy_shown", _time.time())
        return st

    def brokers_list(_h):
        from brokers import key_brokers
        return {"brokers": key_brokers()}

    def broker_cash(_h):
        """Buying power for the Launch step: warn before going live with an
        account that can't pay for a single trade."""
        from brokers import BROKERS
        mod = BROKERS.get(cfg.get("broker", ""))
        if mod is None or not hasattr(mod, "buying_power"):
            return {"known": False}
        try:
            cl = mod.client(cfg)
        except Exception:
            cl = None
        bp = A._buying_power(mod, cl, cfg)
        return {"known": bp is not None, "buying_power": bp,
                "warning": A._cash_warning(cfg, bp),
                "dry_run": bool(cfg.get("dry_run"))}

    def broker_connect(_h, data):
        return _broker_connect(data, save)

    def _broker_connect(data, save_now):
        """The classic connect (/api/broker/connect). save_now: save(), or
        for /v2 a save that first checks nothing changed since the preview."""
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from brokers import BROKERS
        mod = BROKERS.get(str(data.get("broker", "")))
        if mod is None or not callable(getattr(mod, "connect", None)):
            return {"ok": False, "error": "unknown broker"}
        ok, msg = mod.connect(cfg, data.get("values", {}) or {})
        if not ok:
            return {"ok": False, "error": msg}
        pending["broker_last4"] = str(data.get("broker", ""))[:6]
        save_now()
        return {"ok": True, "note": msg}

    def update_check(_h):
        # "?force=1": the owner asked to check now (else a recent answer)
        from urllib.parse import parse_qs, urlparse
        try:
            q = parse_qs(urlparse(getattr(_h, "path", "") or "").query)
        except Exception:
            q = {}
        return check_code_updates(force=q.get("force", [""])[0] in ("1", "true"))

    def help_netcheck(_h):
        import support
        from agent import _agenthc_host
        return {"ok": True, "text": "\n".join(support.connection_check(_agenthc_host()))}

    def help_report(_h):
        import support
        from agent import _agenthc_host
        return {"ok": True, "text": support.report(_repo_dir(), A.doctor_checks,
                                                   cfg, _agenthc_host())}

    def update_pull(_h, data):
        expect = str((data or {}).get("expect") or "").strip()
        if not expect:
            return {"ok": False, "error": "check for updates first, review the "
                    "commit list, then pull"}
        ok, note = pull_code_updates(expect=expect)
        if not ok:
            return {"ok": False, "error": note}
        import os
        # New code may carry new dependencies (e.g. qrcode) — a pull without
        # a dep install restarts onto ImportErrors or silently-degraded
        # features. Fail-soft: a pip failure never blocks the update.
        note += _install_requirements()
        if os.getenv("AGENT_UPDATE_NO_RESTART"):
            # Test/automation escape hatch — never re-exec the test runner.
            return {"ok": True, "note": note + " — restart skipped "
                    "(AGENT_UPDATE_NO_RESTART)"}
        # Restart onto the new code in BOTH modes (running agent AND the
        # standalone setup wizard) so server + UI changes apply immediately —
        # a pulled webui.py otherwise keeps serving the old page from memory.
        # Re-exec the exact original invocation (agent.py run / agent.py
        # setup / …); config lives on disk and the wizard restores completed
        # steps, so a setup-mode restart is safe. The page polls /api/state
        # and reloads itself once the server is back.
        import sys
        import time as _time
        # Stop new event polls so an in-flight order can finish its atomic
        # state save before the exec; all durable state (config, positions,
        # seen-events, trade log, daily cap) already lives on disk. The hold
        # is in memory only, so polling resumes automatically after the
        # restart (and the owner's own Pause, in config.json, is untouched).
        # Long enough for the restart to wait for an order that is still
        # being placed; released at once if the restart gives up.
        import platform_compat
        hold_orders(_HOLD_S + platform_compat.ORDER_WAIT_SECONDS)

        def _restart():
            _time.sleep(3.0)  # response flush
            # waits for an order that is still being placed (never restarts
            # in the middle of one)
            _restart_after_update(list(sys.argv))
        threading.Thread(target=_restart, daemon=True).start()
        return {"ok": True, "restarting": True,
                "note": note + " — restarting with the new code; this page "
                "will reload in a few seconds"}

    def proposal_get(_h):
        import self_edit
        p = self_edit.current()
        if not p:
            return {"pending": False}
        return {"pending": True, "explanation": p.get("explanation", ""),
                "warnings": p.get("warnings", []),
                "diff": self_edit.shown_diff(p),
                # the diff above is cut: /v2 says so and never applies it
                "truncated": self_edit.diff_truncated(p)}

    def proposal_apply(_h, _data):
        import self_edit
        ok, msg = self_edit.apply_and_restart(restart=get_status is not None)
        return {"ok": ok, "reply": msg}

    def proposal_reject(_h, _data):
        import self_edit
        self_edit.reject()
        return {"ok": True, "reply": "proposal rejected"}

    def pass_buy(_h, data):
        """Invoice for N prepaid signal days; the user pays from any wallet."""
        return _pass_buy_route(data, save)

    def _pass_buy_route(data, save_now):
        """The classic /api/pass/buy. save_now: as in _broker_connect."""
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        return _pass_buy(cfg, save_now, data)

    def pass_options_route(_h):
        from sources import agenthc_day_trade_ideas as hc
        return {"options": hc.pass_options()}

    def pass_status_route(_h):
        return _pass_status(cfg, save)

    def wallet_create(_h, data):
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        return _wallet_create(cfg, save, data)

    def wallet_fund(_h, data):
        w = _wallet()
        if w is None:
            return {"ok": False, "error": "no wallet yet"}
        try:
            sats = max(1000, int(data.get("sats") or 50000))
        except (TypeError, ValueError):
            return {"ok": False, "error": "enter a sats amount"}
        return _wallet_fund_invoice(w, sats, save)

    def _wallet_fund_invoice(w, sats, save_now):
        """The classic /api/wallet/fund's invoice for `sats` into wallet `w`
        (learning the instance's per-invoice cap from a refusal: save_now
        saves it). /v2 calls it after the owner confirmed the amount."""
        note = ""
        cap = int(cfg.get("lnbits_max_invoice_sats") or 0)
        if cap and sats > cap:
            note = (f"this wallet instance caps invoices at {cap:,} sats — "
                    f"generated {cap:,}; pay it and repeat to reach {sats:,}")
            sats = cap
        try:
            bolt11 = w.create_invoice(sats, memo="fund agentic-trader")
        except (WalletError, ValueError) as exc:
            # Learn the instance's per-invoice cap from the error, remember
            # it (clamps future suggestions too), and retry once at the cap.
            new_cap = _invoice_cap_from_error(exc)
            if not new_cap or new_cap >= sats:
                return {"ok": False, "error": str(exc)[:200]}
            cfg["lnbits_max_invoice_sats"] = new_cap
            save_now()
            try:
                bolt11 = w.create_invoice(new_cap, memo="fund agentic-trader")
            except (WalletError, ValueError) as exc2:
                return {"ok": False, "error": str(exc2)[:200]}
            note = (f"this wallet instance caps invoices at {new_cap:,} sats "
                    f"— generated {new_cap:,}; pay it and repeat to reach "
                    f"{sats:,}")
            sats = new_cap
        # Uppercase bolt11 in the QR = alphanumeric mode = denser, easier
        # scan (bech32 is case-insensitive; wallet scanners expect this).
        return {"ok": True, "bolt11": bolt11, "sats": sats, "note": note,
                "qr": _qr_matrix(bolt11.upper())}

    def wallet_quote(h):
        from urllib.parse import parse_qs, urlparse
        try:
            sats = int((parse_qs(urlparse(h.path).query).get("sats")
                        or ["0"])[0])
        except ValueError:
            sats = 0
        return _fund_quote(max(0, sats),
                           int(cfg.get("lnbits_max_invoice_sats") or 0))

    def wallet_recurring(_h, data):
        # Recurring day-pass consent: ON = the agent may auto-pay each day's
        # ~$10 pass from its wallet (still capped by max_autopay_sats);
        # OFF = it never spends without the operator. The run loop re-reads
        # config every poll, so this applies live without a restart.
        return _set_recurring(cfg, save, data.get("on"))

    def wallet_balance(_h):
        w = _wallet()
        if w is None:
            return {"ok": False, "error": "no wallet"}
        try:
            return {"ok": True, "sats": w.balance_sats()}
        except WalletError as exc:
            return {"ok": False, "error": str(exc)[:150]}

    def _wallet():
        if cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
            return LNbitsWallet(cfg["lnbits_url"], cfg["lnbits_admin_key"])
        return None

    def rh_start(_h):
        rh = RobinhoodMCP(_token_path())
        url, p = rh.auth_start()
        pending["oauth"] = (rh, p)
        pending["oauth_return"] = None  # the classic landing, as always
        return {"url": url}

    def rh_callback(h):
        # Robinhood redirected the browser here — finish the exchange inline.
        from urllib.parse import parse_qs, urlparse
        if not pending["oauth"]:
            return {"ok": False, "error": "no auth in progress"}
        rh, p = pending["oauth"]
        qs = parse_qs(urlparse(h.path).query)
        redirect = "http://127.0.0.1:8721/callback?" + "&".join(
            f"{k}={v[0]}" for k, v in qs.items())
        rh.auth_finish(p, redirect)
        payload = content_json(rh.call_tool("get_accounts", {})) or {}
        warning = ""
        found = {}  # the account this sign-in saved, if any (for /v2)
        for acct in ((payload.get("data") or {}).get("accounts") or []):
            if acct.get("agentic_allowed") and acct.get("state") == "active":
                cfg["robinhood_account"] = str(acct["account_number"])
                found["robinhood_account"] = cfg["robinhood_account"]
                cfg["broker"] = "robinhood"
                pending["broker_last4"] = cfg["robinhood_account"][-4:]
                if not acct.get("option_level") or acct.get("option_level") == "option_level_0":
                    warning = ("Options are NOT enabled on your Agentic account — "
                               "orders will be rejected until you apply: "
                               "https://applink.robinhood.com/upgrade_options"
                               f"?account_number={cfg['robinhood_account']}")
        pending["rh_warning"] = warning
        save()
        _rh_signin_done("robinhood_account" in found, found.get("robinhood_account", ""),
                        bool(warning))
        # human-friendly landing: bounce back to the wizard tab
        raise _Redirect()

    def sizing(_h, data):
        """Each book sized on its own: main = the daily pick, other = the
        shadow "other trades" journal (mode "same" = follow main).
        Validated on a copy so a bad shadow-book value never half-saves."""
        main = data.get("main") if isinstance(data.get("main"), dict) else {}
        other = data.get("other") if isinstance(data.get("other"), dict) else {}
        new = dict(cfg)
        ok, err = A.set_book_sizing(new, "main", main.get("mode"), main.get("value"))
        if not ok:
            return {"ok": False, "error": f"main book: {err}"}
        ok, err = A.set_book_sizing(new, "other", other.get("mode") or "same",
                                    other.get("value"))
        if not ok:
            return {"ok": False, "error": f"shadow book: {err}"}
        _adopt(new)
        save()
        return {"ok": True}

    def _adopt(new):
        """Make the working copy `new` (a changed copy of it), touching only
        the keys that differ."""
        for k in [k for k in list(cfg) if k not in new]:
            cfg.pop(k, None)  # (never clear(): another request may be reading)
        cfg.update({k: v for k, v in new.items()
                    if k not in cfg or cfg[k] != v})  # only what sizing changed

    def source_scope(_h, data):
        """Just the "which signals" choice (main / main + other trades), so the
        Sizing step can fix a mismatch without re-saving the whole source."""
        if cfg.get("source", "agenthc") != "agenthc":
            return {"ok": False, "error": "only the AgentHC source has this setting"}
        if data.get("scope") not in ("main", "all"):
            return {"ok": False, "error": "scope must be main or all"}
        cfg["include_other_trades"] = data["scope"] == "all"
        save()
        return {"ok": True}

    def safety(_h, data):
        perr = _policy_save_error(cfg, data.get("policy", ""),
                                  data.get("anthropic_key", ""))
        if perr:
            return {"ok": False, "error": perr}
        # Mode and cap change only when the form sends them (it sends what
        # the owner changed). Left out, they stay as they are — saving a
        # ticker rule must never switch the mode or reset the cap. Never
        # set before (first setup): dry-run, 5 entries a day.
        if isinstance(data.get("dry"), bool):
            cfg["dry_run"] = data["dry"]
        elif cfg.get("dry_run") is None:
            cfg["dry_run"] = True
        _apply_entry_cap(cfg, data.get("cap"), data.get("seeded"))
        _apply_daily_loss(cfg, data.get("max_loss"))
        cfg.setdefault("max_event_age_s", 300)
        import rules
        rules.apply_settings(cfg, data)
        _apply_keep_awake(cfg, data)
        _apply_alert_targets(cfg, data)
        policy = str(data.get("policy", "")).strip()
        if policy:
            _write_policy_file(policy)
            key = str(data.get("anthropic_key", "")).strip()
            import llm_client
            if key and not llm_client.configured(cfg):
                pid = llm_client.detect_provider(key)
                if pid == "anthropic":  # other providers need a model pick:
                    cfg["llm_provider"] = pid          # use Connect your AI
                    cfg["llm_api_key"] = key
            cfg.setdefault("llm_fallback", "skip")
        cfg["poll_seconds"] = int(cfg.get("poll_seconds", 30))
        save()
        import platform_compat
        nxt = ("cd " + _repo_dir() + "\n"
               + platform_compat.venv_python() + " agent.py start\n"
               + platform_compat.venv_python() + " agent.py autostart on"
               "   # optional: start at every login\n\n"
               "(start = background; closing the window doesn't stop it. "
               "Servers: the systemd service or Docker — see GETTING_STARTED.md)")
        return {"ok": True, "next": nxt}

    def policy_save(_h, data):
        """Plain-English rules from the optional AI step. Needs a connected
        AI (or they'd veto every entry); blank = leave as is."""
        policy = str(data.get("policy", "")).strip()
        if not policy:
            return {"ok": True, "note": "no plain-English rules saved"}
        perr = _policy_save_error(cfg, policy, "")
        if perr:
            return {"ok": False, "error": perr}
        _store_policy(policy)
        save()
        return {"ok": True, "note": "plain-English rules saved"}

    def _store_policy(policy):
        """A checked, non-empty policy: on file, and the AI's fallback set
        (not saved: the caller saves)."""
        _write_policy_file(policy)
        cfg.setdefault("llm_fallback", "skip")

    # ── move to another computer ──
    def move_export(_h, data):
        import transfer
        try:
            blob = transfer.export_bundle(str(data.get("passphrase") or ""))
        except transfer.TransferError as exc:
            return {"ok": False, "error": str(exc)}
        pending["move_blob"] = blob
        return {"ok": True, "filename": transfer.export_filename(),
                "kb": max(1, len(blob) // 1024)}

    def move_download(_h):
        import transfer
        blob = pending.get("move_blob")
        if not blob:
            return {"ok": False, "error": "create the move file first"}
        return RawResponse(blob, "application/octet-stream", {
            "Content-Disposition":
                f'attachment; filename="{transfer.export_filename()}"'})

    def move_finish(_h, _data):
        """After the owner saved the file: retire this computer so the two
        can never both trade."""
        import transfer
        note = transfer.mark_moved_away()
        pending.pop("move_blob", None)
        if get_status is not None:  # the agent is running: stop it
            CONTROLS["stop"] = True
            note += "; the agent here is stopping"
        return {"ok": True, "note": note}

    def move_import(_h, data):
        import base64
        import keep_running
        import transfer
        running_here = get_status is not None
        if not running_here and keep_running.is_running():
            return {"ok": False, "error": "stop the agent on this computer first "
                    "(dashboard: stop, or python agent.py stop)"}
        try:
            blob = base64.b64decode(str(data.get("data") or ""), validate=False)
            pw = str(data.get("passphrase") or "")
            transfer.decrypt(blob, pw)  # fail fast before pausing anything
            if transfer.existing_setup() and not data.get("replace"):
                raise transfer.TransferError("NEEDS_REPLACE")
            if running_here:
                # The agent is serving this page: stop new polls and let any
                # in-flight event finish its state save, so nothing in memory
                # overwrites the imported files. A restart then loads them.
                import time as _t
                # no time limit: once the files are replaced, the state in
                # memory must never be saved over them (released on failure)
                hold_orders(float("inf"))
                _t.sleep(2.0)
            try:
                info = transfer.import_bundle(blob, pw,
                                              replace=bool(data.get("replace")))
            except Exception:
                if running_here:
                    # nothing changed: carry on as before — the owner's
                    # Pause (config.json) and a damaged-memory hold
                    # (agent._state_held) were never touched
                    release_orders()
                raise
            if running_here:
                # the files are the imported setup now: what this process
                # has in memory is never saved over them (see setup_replaced)
                CONTROLS["replaced"] = True
        except transfer.TransferError as exc:
            if str(exc) == "NEEDS_REPLACE":
                return {"ok": False, "needs_replace": True,
                        "error": "this computer already has a setup — replace it?"}
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"import failed: {str(exc)[:150]}"}
        # the wizard keeps a copy of the config: reload it from the imported file
        try:
            with A.CONFIG_LOCK:
                _pull(A._read_config(), keep_own=False)
        except OSError:
            # The file is busy this instant (antivirus, a sync program).
            # Never stop here: the import is done and the agent is on hold
            # until it restarts. The copy is refreshed by the next request.
            pass
        if running_here:
            import sys as _sys

            def _restart():
                import time as _t
                _t.sleep(1.5)  # let this response reach the browser
                # waits for an order that is still being placed; tells the
                # owner if it had to give up, and leaves if it failed
                _restart_after_import(list(_sys.argv))
            try:
                threading.Thread(target=_restart, daemon=True).start()
            except Exception:
                _restart()  # no new thread to be had: restart from this one
            info["restarting"] = True
        return {"ok": True, **info}

    def policy_disable(_h, _data):
        import llm_policy
        moved = llm_policy.disable_policy()
        return {"ok": True, "note": ("policy turned off (saved as policy.md.disabled)"
                                     if moved else "no policy was set")}

    def finish(_h, _data):
        pending["finished"].set()
        return {"ok": True}

    def _start_refused():
        """Why the agent can't be started here now (start_local's checks),
        or None."""
        if not A.consent_ok():
            return "accept the agreement first"
        from brokers import broker_ready
        if not (cfg.get("source") and broker_ready(cfg)):
            return "finish the source and broker steps first"
        import llm_policy
        if llm_policy.entries_blocked(cfg):
            return llm_policy.BLOCKED_MSG
        import transfer
        if transfer.moved_away():
            return ("this setup was moved to another computer, so it won't start "
                    "here — to run it here again, stop the other computer and run: "
                    "python agent.py resume-here")
        return None

    def start_local(_h, data):
        """Option A as one click: start the agent in the background (under
        keep_running's supervisor, so closing the terminal doesn't stop it
        and a crash restarts it), optionally at every login too, then hand
        it this port. The supervisor waits until the wizard's socket is
        free before running `agent.py run`, so the dashboard (same port)
        can't lose the bind race and fall back to headless."""
        refused = _start_refused()
        if refused:
            return {"ok": False, "error": refused}
        save()
        import keep_running
        auto_note = ""
        if "autostart" in (data or {}):
            _ok, auto_note = keep_running.set_autostart(bool(data["autostart"]))
        try:
            keep_running.start_background(port=APP_PORT)
        except Exception as exc:
            return {"ok": False, "error": f"could not start the agent: {exc}"}
        # release the port AFTER this response has flushed
        threading.Timer(0.5, pending["finished"].set).start()
        return {"ok": True, "autostart_note": auto_note}

    def autostart_get(_h):
        import keep_running
        return keep_running.autostart_status()

    def autostart_set(_h, data):
        import keep_running
        ok, note = keep_running.set_autostart(bool(data.get("enabled")))
        return {"ok": ok, "note": note, **keep_running.autostart_status()}

    def d_status(_h):
        if get_status is None:
            return {"mode": "OFFLINE", "paused": False, "fields": {}}
        return get_status()

    def d_trades(h):
        """Newest trade-log rows; ?before=<n>&limit=<k> pages back through
        the whole log (the dashboard's "Load older")."""
        if not get_trades:
            # setup wizard / agent stopped: say so (an empty page would read
            # as "no older rows" to a dashboard tab left open)
            return {"ok": False, "error": "the agent isn't running"}
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(h.path).query)
        before = (qs.get("before") or [None])[0]
        limit = (qs.get("limit") or [50])[0]
        return A.trade_page(before=before, limit=limit)

    def d_status_v2(_h):
        """The dashboard's state as typed data (view_model.py, schema 2).
        /api/status above is unchanged and stays for the current pages."""
        if get_view is not None:
            return get_view()
        # no agent behind this server (setup): "stopped", from the files.
        # An agent that handed over no view is running but says nothing
        # about itself, which the view-model reports as "not answering".
        return A.view_snapshot({"running": True} if get_status else None)

    def d_activity_v2(h):
        """Older trade-log rows in the same typed shape as the status's
        `activity`: ?before=<n>&limit=<k>, like /api/trades."""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(h.path).query)
        page = get_activity if get_activity is not None else A.view_activity
        return page(before=(qs.get("before") or [None])[0],
                    limit=(qs.get("limit") or [None])[0])

    def d_pnl(_h):
        """All-time est. P&L from the whole trade log, incl. pass days."""
        if not get_trades:
            return {"passes": False, "live": None, "dry": None}
        return A.pnl_summary(cfg_getter() if cfg_getter else cfg)

    def d_command(_h, data):
        text = str(data.get("text", "")).strip()
        if apply_command is None:
            return {"reply": "The agent isn't running yet — finish setup, then "
                             "start it with: agent.py start"}
        handled, reply = apply_command(text)
        if handled:
            return {"reply": reply}
        s = d_status(None)
        return {"reply": _ask_llm((cfg_getter() if cfg_getter else cfg), text,
                                  s.get("fields", {}),
                                  get_trades() if get_trades else [])}

    # ── /v2 settings API ─────────────────────────────────────────────────────
    # Typed payloads in; underneath, the code the classic pages and the chat
    # commands run (one implementation per setting). A money-affecting write
    # is applied only by its second POST, carrying the token its preview
    # gave (_Confirmations).
    confirms = _Confirmations()
    rule_keys = ("rule_tickers", "rule_skip_tickers", "rule_sides",
                 "rule_min_strike", "rule_skip_0dte", "rule_loss_streak")
    does_not_trade = "It does not buy or sell anything."

    def _fingerprint():
        """Of config.json as it is on disk now (not of the working copy,
        which may hold another request's unsaved changes)."""
        with A.CONFIG_LOCK:
            try:
                disk = A._read_config()
            except OSError:
                disk = dict(cfg)
        return _settings_fingerprint(disk, confirms.key)

    def _sync():
        """The working copy brought up to config.json as it is now (under
        CONFIG_LOCK). Raises ConfigUnreadable: nothing may be saved then."""
        _pull(A._read_config())

    def _v2(fn):
        def run(_h, data):
            try:
                return fn(data)
            except _Invalid as exc:
                return {"ok": False, "error": str(exc)}
        run.__name__ = fn.__name__
        return run

    def _two_step(route, payload, data, changes, does_not, extra=None,
                  bind=None):
        """Under CONFIG_LOCK, after _sync(). None: go ahead (the token in
        `data` confirms exactly this payload against these settings). Else
        the reply: the preview (no token sent yet) or why not. bind: more
        facts the preview showed (the real positions a stop leaves unsold):
        when they differ at the confirm, the owner reviews again."""
        import hashlib
        fp = _fingerprint()
        if bind is not None:
            fp += ":" + hashlib.sha256(_canonical(bind).encode()).hexdigest()[:16]
        if "confirm" not in data:
            return {"ok": True, "needs_confirm": True, "preview": {
                "changes": changes, "does_not": does_not, **(extra or {}),
                "token": confirms.issue(route, payload, fp),
                "expires_in_s": _V2_CONFIRM_S}}
        problem = confirms.redeem(data.get("confirm"), route, payload, fp)
        return None if problem is None else {"ok": False, "error": problem}

    class _Changed(Exception):
        """config.json changed between a redeemed token and its save."""

    def _save_if_unchanged(fp):
        """save(), for a confirmed write whose classic handler asks the
        broker or AgentHC something before it saves (outside CONFIG_LOCK,
        so a slow answer never holds up the agent's own commands): it
        saves only while config.json still has the fingerprint the token
        was redeemed against, checked and written in one lock hold. Else
        _Changed, and nothing is saved."""
        def guarded():
            with A.CONFIG_LOCK:
                if _fingerprint() != fp:
                    raise _Changed()
                save()
        return guarded

    def _unchanged_since(fp):
        """As _save_if_unchanged, for a classic helper that does more than
        save once the slow answer is in (writes a file first): the whole
        step runs in one CONFIG_LOCK hold, and only while config.json still
        has the fingerprint `fp`. Else _Changed before anything is done."""
        import contextlib

        @contextlib.contextmanager
        def held():
            with A.CONFIG_LOCK:
                if _fingerprint() != fp:
                    raise _Changed()
                yield
        return held

    def _change(key, label, saved_now, new, direction):
        return {"key": key, "label": label, "saved_now": saved_now,
                "new": new, "direction": direction}

    def _pass_purchases():
        """The signal-day purchases on file, by id (a hash of the pass
        token, never the token): paid ones (the pass and paid spares) and
        invoices that can still be paid. An unpaid invoice that expired is
        in neither: the pass check drops it."""
        from sources import agenthc_day_trade_ideas as hc
        extra = cfg.get("feed_pass_extra")
        extra = [e for e in extra if isinstance(e, dict)] if isinstance(extra, list) else []

        def ids(entries):
            return [hc._token_id(e["token"]) for e in entries
                    if isinstance(e, dict) and isinstance(e.get("token"), str)
                    and e["token"]]
        return {"paid": ids([cfg.get("feed_pass")]
                            + [e for e in extra if e.get("paid")]),
                "waiting": ids([cfg.get("feed_pass_pending")]
                               + [e for e in extra if not e.get("paid")])}

    def _settings_view():
        import keep_running
        import llm_client
        import llm_policy
        import rules
        from brokers import account_tag, broker_ready, key_brokers
        ai = llm_client.resolve(cfg) or {}
        bid = str(cfg.get("broker") or "")
        wiz = next((w for w in key_brokers() if w.get("id") == bid), None)
        fields = {}
        for f in (wiz or {}).get("fields", []):
            if f.get("type") == "checkbox":
                fields[f["id"]] = bool(cfg.get(f["id"], f.get("default", False)))
            else:  # keys, passwords, gateway address: never sent back
                fields[f["id"]] = _secret(cfg.get(f["id"]),
                                          f.get("type") != "password")
        try:
            ready = bool(broker_ready(cfg))
        except Exception:
            ready = None
        try:
            auto = keep_running.autostart_status()
            auto = {"supported": bool(auto.get("supported")),
                    "enabled": bool(auto.get("enabled")),
                    "how": str(auto.get("how") or "")}
        except Exception:
            auto = {"supported": False, "enabled": None, "how": ""}
        acct = str(cfg.get("robinhood_account") or "") if bid == "robinhood" else ""
        try:
            open_at_login = bool(A._open_at_login_wanted(cfg))
        except Exception:
            open_at_login = None
        return {
            "ok": True,
            "fingerprint": _fingerprint(),
            "agent_running": get_status is not None,
            "mode": {"mode": A._mode(cfg),
                     "explicit": isinstance(cfg.get("dry_run"), bool)},
            "paused": A._paused(cfg),
            "limits": {
                "trade_size": {"main": _book_size(cfg, ""),
                               "other": _book_size(cfg, "other_")},
                "max_entries_per_day": cfg.get("max_entries_per_day"),
                "max_daily_loss_usd": cfg.get("max_daily_loss_usd"),
                "max_open_positions": cfg.get("max_open_positions"),
                "defaults": {"max_entries_per_day": 5, "max_open_positions": 10},
                "allowed": _V2_LIMIT_RANGES,
            },
            "rules": {**{k: cfg.get(k) for k in rule_keys},
                      "summary": rules.describe(cfg) or "",
                      "policy": llm_policy.policy_text(),
                      "policy_blocks_entries": llm_policy.entries_blocked(cfg)},
            "alerts": {src: _secret(cfg.get(key)) for src, key in _ALERT_KEYS},
            "computer": {"autostart": auto,
                         "keep_awake": cfg.get("keep_awake", True) is not False,
                         "open_dashboard_at_login": open_at_login,
                         "dashboard": _dashboard_pref(cfg)},
            "expiry": _expiry_view(),
            "no_signal": _no_signal_view(),
            "code": {"self_edit_enabled": bool(cfg.get("self_edit_enabled"))},
            "consent": _consent_view(),
            "ai": {"connected": bool(ai), "provider": ai.get("provider"),
                   "name": ai.get("name"), "model": ai.get("model"),
                   "endpoint": (_site_only(cfg["llm_base_url"])
                                if cfg.get("llm_base_url") else None),
                   "key": _secret(ai.get("key"))},
            "broker": {"id": bid or None, "ready": ready,
                       "account_mask": acct[-4:] or None,
                       "paper": (account_tag(cfg).endswith(":paper")
                                 if bid else None),
                       "fields": fields,
                       "signin": dict(pending["rh_signin"]),
                       # the classic wizard's Robinhood warning, as a code:
                       # its text carries the whole account number
                       "warning": "options_off" if pending["rh_warning"] else None},
            "signals": {"source": cfg.get("source"),
                        "include_other_trades": bool(cfg.get("include_other_trades")),
                        "dry_run_preview": cfg.get("dry_run_preview", True) is not False,
                        "recurring": bool(cfg.get("day_pass_recurring")),
                        "wallet_connected": bool(cfg.get("lnbits_url")
                                                 and cfg.get("lnbits_admin_key")),
                        # a JSON feed's site only (its address may carry a token)
                        "source_site": (_host_only(cfg["source_url"])
                                        if cfg.get("source") == "url"
                                        and cfg.get("source_url") else None),
                        "wallet": _wallet_view(),
                        "api_key": _secret(cfg.get("agenthc_api_key")),
                        "pass_on_file": bool(cfg.get("feed_pass")
                                             or cfg.get("feed_pass_pending")),
                        "purchases": _pass_purchases()},
        }

    def _wallet_view():
        """The payment wallet, secret-free: its site, the test-server
        warning, whether a wallet page link is on file (the link itself only
        through POST /api/v2/wallet/page), the caps; and where a new wallet
        would be made."""
        import lightning_wallet
        url = str(cfg.get("lnbits_url") or "")
        new = str(lightning_wallet.DEFAULT_LNBITS or "")
        cap = cfg.get("max_autopay_sats")
        inv = cfg.get("lnbits_max_invoice_sats")
        whole = lambda v: (int(v) if isinstance(v, (int, float))  # noqa: E731
                           and not isinstance(v, bool) and v > 0 else None)
        return {"site": _host_only(url) if url else None,
                "warning": lightning_wallet.demo_warning(url) or None,
                "page_on_file": bool(cfg.get("lnbits_wallet_page")),
                "autopay_cap_sats": whole(cap),
                "invoice_cap_sats": whole(inv),
                "new_site": _host_only(new) if new else None,
                "new_warning": lightning_wallet.demo_warning(new) or None}

    def _expiry_view():
        """The expiry sell and its alert, as saved and as they fall today
        (an early close moves both): the readers the chat's "expiry guard"
        uses. HH:MM ET; null = off."""
        def hm(mins):
            return None if mins is None else f"{mins // 60:02d}:{mins % 60:02d}"
        try:
            day = A._et_now().date().isoformat()
            guard, alert, close = A._expiry_times(cfg, day)
            return {
                "sell_et": hm(A._et_hhmm(cfg, "expiry_guard_et",
                                         A.EXPIRY_GUARD_DEFAULT)),
                "alert_et": hm(A._et_hhmm(cfg, "expiry_alert_et",
                                          A.EXPIRY_ALERT_DEFAULT)),
                "effective_today": {"sell_et": hm(guard), "alert_et": hm(alert),
                                    "close_et": hm(close),
                                    "early_close": close != A._REGULAR_CLOSE_MIN},
                "described": A._expiry_guard_desc(cfg),
                "allowed": ["09:30", "15:59"],
            }
        except Exception:
            return None  # not known: shown as not known

    def _no_signal_view():
        """The no-signal sell (the chat's "no-signal sell"): the time saved
        and today's (an early close moves it). HH:MM ET; null = off."""
        try:
            day = A._et_now().date().isoformat()
            return {"sell_et": _hhmm(A._et_hhmm(cfg, "signal_loss_sell_et",
                                                A.SIGNAL_LOSS_DEFAULT)),
                    "today_et": _hhmm(A._signal_loss_time(cfg, day)),
                    "default": A.SIGNAL_LOSS_DEFAULT,
                    "allowed": ["09:30", "15:59"]}
        except Exception:
            return None  # not known: shown as not known

    def _consent_view():
        """The agreement on file (never its text: GET /api/v2/agreement)."""
        rec = A._load(A.ACCEPTANCE_PATH, {}) or {}
        rec = rec if isinstance(rec, dict) else {}
        return {"accepted": bool(A.consent_ok()),
                "on_file": rec.get("accepted") is True,
                "accepted_at": (str(rec["accepted_at"])[:40]
                                if rec.get("accepted_at") else None),
                "terms_version": (str(rec["terms_version"])[:80]
                                  if rec.get("terms_version") else None),
                "current_terms_version": A.TERMS_VERSION}

    def v2_agreement(_h):
        """GET /api/v2/agreement: the agreement's text, to read again."""
        return {"ok": True, "text": A._disclaimer_text(),
                "terms_version": A.TERMS_VERSION}

    def v2_settings(_h):
        """GET /api/v2/settings: the settings as they are on disk now, typed,
        with the fingerprint a preview is bound to. Secrets: set + last4."""
        with A.CONFIG_LOCK:
            try:
                _sync()
                readable = True
            except OSError:
                readable = False  # answered from the last good read
            return {**_settings_view(), "settings_readable": readable}

    def _limits_new(p):
        """The validated limits payload applied to a copy of the settings,
        by the classic code: (copy, changes)."""
        new = dict(cfg)
        changes = []
        size = p.get("trade_size")
        if "trade_size" in p:
            if not isinstance(size, dict) or not size or any(
                    b not in ("main", "other") for b in size):
                raise _Invalid("trade_size takes main and/or other. Nothing "
                               "was changed.")
            for book in ("main", "other"):
                if book not in size:
                    continue
                spec = size[book]
                if not isinstance(spec, dict) or set(spec) - {"mode", "value"}:
                    raise _Invalid(f"trade_size.{book} takes mode and value. "
                                   "Nothing was changed.")
                mode = spec.get("mode")
                if book == "other" and mode == "same":
                    if "value" in spec:
                        raise _Invalid("trade_size.other: 'same' takes no "
                                       "value. Nothing was changed.")
                    value = None
                elif mode in _V2_SIZE_RANGES:
                    low, high, whole, label = _V2_SIZE_RANGES[mode]
                    value = _v2_number(spec.get("value"),
                                       f"trade_size.{book} ({label})", low,
                                       high, whole=whole, low_open=not whole)
                else:
                    raise _Invalid(f"trade_size.{book}.mode must be budget, "
                                   "contracts or percent"
                                   + (" (or same)" if book == "other" else "")
                                   + ". Nothing was changed.")
                ok, err = A.set_book_sizing(new, book, mode, value)
                if not ok:
                    raise _Invalid(f"trade_size.{book}: {err}. Nothing was changed.")
                prefix = "" if book == "main" else "other_"
                before, after = _book_size(cfg, prefix), _book_size(new, prefix)
                if before != after:
                    changes.append(_change(
                        f"trade_size.{book}",
                        "Trade size" + (" (main pick)" if book == "main"
                                        else " (other trades)"),
                        before, after, _size_direction(before, after)))
        if "max_entries_per_day" in p:
            n = _v2_number(p["max_entries_per_day"], "max_entries_per_day",
                           *_V2_LIMIT_RANGES["max_entries_per_day"], whole=True)
            _apply_entry_cap(new, str(n), True)
        if "max_daily_loss_usd" in p:
            usd = _v2_number(p["max_daily_loss_usd"], "max_daily_loss_usd",
                             *_V2_LIMIT_RANGES["max_daily_loss_usd"])
            _apply_daily_loss(new, repr(usd))
        if "max_open_positions" in p:
            A.set_position_cap(new, _v2_number(
                p["max_open_positions"], "max_open_positions",
                *_V2_LIMIT_RANGES["max_open_positions"], whole=True))
        if "expiry_sell_et" in p:
            # the chat's "expiry guard off|HH:MM", by the same check and
            # into the same key (agent._expiry_setting)
            v = p["expiry_sell_et"]
            sell = (A._expiry_setting(v, A.EXPIRY_GUARD_DEFAULT)
                    if isinstance(v, str) and v != "on" else None)
            if sell is None:
                raise _Invalid('expiry_sell_et must be "off" or a time from '
                               "09:30 to 15:59 (HH:MM, Eastern). Nothing was "
                               "changed.")
            new["expiry_guard_et"] = sell
            before = _hhmm(A._et_hhmm(cfg, "expiry_guard_et",
                                      A.EXPIRY_GUARD_DEFAULT))
            after = _hhmm(A._et_hhmm(new, "expiry_guard_et",
                                     A.EXPIRY_GUARD_DEFAULT))
            if before != after:
                changes.append(_change("expiry_sell_et", "Expiry-day sell time",
                                       before, after,
                                       _expiry_direction(before, after)))
        if "no_signal_sell_et" in p:
            # the chat's "no-signal sell off|HH:MM", by the same check and
            # into the same key; off, or a later time, raises the risk (a
            # position nobody can get an exit signal for stays open longer)
            v = p["no_signal_sell_et"]
            sell = (A._expiry_setting(v, A.SIGNAL_LOSS_DEFAULT)
                    if isinstance(v, str) and v != "on" else None)
            if sell is None:
                raise _Invalid('no_signal_sell_et must be "off" or a time from '
                               "09:30 to 15:59 (HH:MM, Eastern). Nothing was "
                               "changed.")
            new["signal_loss_sell_et"] = sell
            before = _hhmm(A._et_hhmm(cfg, "signal_loss_sell_et",
                                      A.SIGNAL_LOSS_DEFAULT))
            after = _hhmm(A._et_hhmm(new, "signal_loss_sell_et",
                                     A.SIGNAL_LOSS_DEFAULT))
            if before != after:
                changes.append(_change("no_signal_sell_et", "No-signal sell time",
                                       before, after,
                                       _expiry_direction(before, after)))
        for key, label, default, unlimited in (
                ("max_entries_per_day", "New trades per day", 5, False),
                ("max_daily_loss_usd", "Daily loss limit", None, True),
                ("max_open_positions", "Positions open at once", 10, False)):
            if key in p and cfg.get(key, default) != new.get(key, default):
                changes.append(_change(
                    key, label, cfg.get(key, default), new.get(key, default),
                    _risk_direction(cfg.get(key, default), new.get(key, default),
                                    none_is_unlimited=unlimited)))
        return new, changes

    @_v2
    def v2_limits(data):
        """POST /api/v2/settings/limits: trade size per book, new trades per
        day, daily loss limit, positions open at once, the expiry-day sell
        time (the chat's "expiry guard") and the no-signal sell time (the
        chat's "no-signal sell"). Partial: only the keys sent change. Never
        touches Practice/LIVE or the pause."""
        p = _v2_payload(data, ("trade_size", "max_entries_per_day",
                               "max_daily_loss_usd", "max_open_positions",
                               "expiry_sell_et", "no_signal_sell_et"))
        if not p:
            raise _Invalid("Send at least one limit. Nothing was changed.")
        with A.CONFIG_LOCK:
            _sync()
            new, changes = _limits_new(p)
            if not changes:
                return {"ok": True, "saved": False,
                        "note": "Nothing to change: those are your settings already."}
            # a sell time decides whether the agent sells what it already
            # holds before the close: never "does not change" those
            sells = any(ch["key"] in ("expiry_sell_et", "no_signal_sell_et")
                        for ch in changes)
            wait = _two_step("limits", p, data, changes, [
                "It does not switch between Practice and LIVE.",
                does_not_trade]
                + ([] if sells
                   else ["It does not change positions you already hold."]))
            if wait:
                return wait
            _adopt(new)
            save()
            return {"ok": True, "saved": True, "settings": _settings_view()}

    @_v2
    def v2_rules(data):
        """POST /api/v2/settings/rules: the built-in rules (rules.apply_settings,
        as the classic Safety step) and the written policy (as the classic
        policy save; null turns it off, as /api/policy/disable)."""
        import rules
        p = _v2_payload(data, rule_keys + ("policy",))
        if not p:
            raise _Invalid("Send at least one rule. Nothing was changed.")
        for k in ("rule_tickers", "rule_skip_tickers"):
            if k in p:
                v = p[k]
                if (not isinstance(v, list) or len(v) > 200
                        or not all(isinstance(t, str) for t in v)):
                    raise _Invalid(f"{k} must be a list of tickers. Nothing "
                                   "was changed.")
                bad = [t for t in v if rules.parse_tickers([t]) == []]
                if bad:
                    raise _Invalid(f"{k}: not a ticker: {', '.join(bad)[:80]}. "
                                   "Nothing was changed.")
        if "rule_sides" in p and p["rule_sides"] not in rules.SIDES:
            raise _Invalid("rule_sides must be one of: "
                           + ", ".join(rules.SIDES) + ". Nothing was changed.")
        if "rule_min_strike" in p:
            _v2_number(p["rule_min_strike"], "rule_min_strike", 0, 100_000)
        if "rule_skip_0dte" in p and not isinstance(p["rule_skip_0dte"], bool):
            raise _Invalid("rule_skip_0dte must be true or false. Nothing was "
                           "changed.")
        if "rule_loss_streak" in p:
            _v2_number(p["rule_loss_streak"], "rule_loss_streak", 0, 100,
                       whole=True)
        policy = p.get("policy", "")
        if "policy" in p and policy is not None:
            if not isinstance(policy, str) or not policy.strip():
                raise _Invalid("policy must be your written rules (send null "
                               "to turn them off). Nothing was changed.")
            if len(policy) > 20_000:
                raise _Invalid("policy is too long (20,000 characters at "
                               "most). Nothing was changed.")
        with A.CONFIG_LOCK:
            _sync()
            if "policy" in p and policy is not None:
                perr = _policy_save_error(cfg, policy.strip(), "")
                if perr:
                    return {"ok": False, "error": perr}
            rules.apply_settings(cfg, {k: v for k, v in p.items() if k in rule_keys})
            if "policy" in p:
                if policy is None:
                    import llm_policy
                    llm_policy.disable_policy()
                else:
                    _store_policy(policy.strip())
            save()
            return {"ok": True, "saved": True, "settings": _settings_view()}

    @_v2
    def v2_alerts(data):
        """POST /api/v2/settings/alerts: Discord / ntfy / Telegram targets, as
        the classic Safety step saves them. null clears one (the classic form
        can't: a blank field there keeps what is set)."""
        p = _v2_payload(data, tuple(src for src, _key in _ALERT_KEYS)
                        + ("expiry_alert_et",))
        if not p:
            raise _Invalid("Send at least one alert target (or expiry_alert_et). "
                           "Nothing was changed.")
        keep = {}
        for src, _key in _ALERT_KEYS:
            if src in p and p[src] is not None:
                keep[src] = _v2_text(p[src], src)
        if "discord" in keep and not keep["discord"].startswith("https://"):
            raise _Invalid("discord must be the webhook address (https://…). "
                           "Nothing was changed.")
        # the expiry-day alert time: what the chat's "expiry alert off|HH:MM"
        # saves, by the same check (a notification only: no money moves)
        alert = None
        if "expiry_alert_et" in p:
            v = p["expiry_alert_et"]
            alert = (A._expiry_setting(v, A.EXPIRY_ALERT_DEFAULT)
                     if isinstance(v, str) and v != "on" else None)
            if alert is None:
                raise _Invalid('expiry_alert_et must be "off" or a time from '
                               "09:30 to 15:59 (HH:MM, Eastern). Nothing was "
                               "changed.")
        with A.CONFIG_LOCK:
            _sync()
            _apply_alert_targets(cfg, keep)
            for src, key in _ALERT_KEYS:
                if src in p and p[src] is None:
                    cfg.pop(key, None)
            if alert is not None:
                cfg["expiry_alert_et"] = alert
            save()
            return {"ok": True, "saved": True, "settings": _settings_view()}

    @_v2
    def v2_computer(data):
        """POST /api/v2/settings/computer: start at login (the classic
        /api/autostart route), keep awake (as the Safety step), open the
        dashboard at login (the chat's "open at login on|off" key) and the
        dashboard "/" lands on ("classic" | "v2"). None of them is money."""
        p = _v2_payload(data, ("autostart", "keep_awake",
                               "open_dashboard_at_login", "dashboard"))
        if not p:
            raise _Invalid("Send autostart, keep_awake, open_dashboard_at_login "
                           "and/or dashboard. Nothing was changed.")
        for k, v in p.items():
            if k == "dashboard":
                if v not in _DASHBOARDS:
                    raise _Invalid('dashboard must be "classic" or "v2". '
                                   "Nothing was changed.")
            elif not isinstance(v, bool):
                raise _Invalid(f"{k} must be true or false. Nothing was changed.")
        out = {"ok": True, "saved": True}
        if "autostart" in p:
            res = autostart_set(None, {"enabled": p["autostart"]})
            out["autostart_note"] = res.get("note", "")
            if not res.get("ok"):
                return {"ok": False, "error": res.get("note")
                        or "could not change start-at-login"}
        if {"keep_awake", "open_dashboard_at_login", "dashboard"} & set(p):
            with A.CONFIG_LOCK:
                _sync()
                if "keep_awake" in p:
                    _apply_keep_awake(cfg, p)
                if "open_dashboard_at_login" in p:
                    cfg["open_dashboard_at_login"] = p["open_dashboard_at_login"]
                if "dashboard" in p:
                    cfg["dashboard"] = p["dashboard"]
                save()
        with A.CONFIG_LOCK:
            out["settings"] = _settings_view()
        return out

    @_v2
    def v2_ai(data):
        """POST /api/v2/settings/ai: the classic /api/llm handler (it tries
        the model once before saving)."""
        p = _v2_payload(data, ("provider", "key", "model", "base_url"))
        for k, v in p.items():
            if not isinstance(v, str) or len(v) > 2000:
                raise _Invalid(f"{k} must be text. Nothing was changed.")
        res = llm(None, p)
        if res.get("ok"):
            res = {**res, "saved": True, "settings": _settings_view()}
        return res

    @_v2
    def v2_broker_connect(data):
        """POST /api/v2/broker/connect: the classic /api/broker/connect,
        applied by the second (confirmed) POST only."""
        from brokers import account_tag, key_brokers
        p = _v2_payload(data, ("broker", "values"))
        wiz = next((w for w in key_brokers()
                    if w.get("id") == p.get("broker")), None)
        if wiz is None:
            raise _Invalid("unknown broker. Nothing was changed.")
        kinds = {f["id"]: f.get("type") for f in wiz.get("fields", [])}
        values = p.get("values", {})
        if not isinstance(values, dict) or set(values) - set(kinds):
            raise _Invalid("values must be the broker's own fields: "
                           + ", ".join(kinds) + ". Nothing was changed.")
        for k, v in values.items():
            if kinds[k] == "checkbox":
                if not isinstance(v, bool):
                    raise _Invalid(f"{k} must be true or false. Nothing was "
                                   "changed.")
            elif not isinstance(v, str) or len(v) > 2000:
                raise _Invalid(f"{k} must be text. Nothing was changed.")
        p = {"broker": p["broker"], "values": values}
        # the account it would be: the broker's paper/live switch (a
        # "<broker>_paper" field; the adapter's own default when not sent)
        switch = {f["id"]: values.get(f["id"], bool(f.get("default", False)))
                  for f in wiz.get("fields", [])
                  if f.get("type") == "checkbox" and f["id"].endswith("_paper")}
        with A.CONFIG_LOCK:
            _sync()
            before = {"id": cfg.get("broker") or None,
                      "paper": (account_tag(cfg).endswith(":paper")
                                if cfg.get("broker") else None)}
            trial = {**cfg, "broker": p["broker"], **switch}
            after = {"id": p["broker"],
                     "paper": (all(switch.values()) if switch else None)}
            direction = _broker_direction(before, after)
            notes = [f"It does not switch between Practice and LIVE: "
                     f"{'Practice' if A._mode(cfg) == 'practice' else 'LIVE'} "
                     "stays.", does_not_trade]
            if before["id"] and account_tag(cfg) != account_tag(trial):
                notes.append("Positions the agent opened in the account it "
                             "uses now are not sold from the new one: look "
                             "after those at your current broker.")
            wait = _two_step("broker_connect", p, data, [_change(
                "broker", "Broker account", before, after, direction)], notes)
            fp = _fingerprint()  # what the token was just redeemed against
        if wait:
            return wait
        last4 = pending["broker_last4"]
        try:
            res = _broker_connect(p, _save_if_unchanged(fp))
        except _Changed:
            pending["broker_last4"] = last4
            return {"ok": False, "error": _REVIEW_AGAIN}
        if res.get("ok"):
            res = {**res, "saved": True, "settings": _settings_view()}
        return res

    @_v2
    def v2_robinhood(data):
        """POST /api/v2/broker/robinhood: the classic Robinhood sign-in
        (/api/rh/start) behind the same two-step confirmation as connecting
        a key-based broker. The confirmed POST answers with the address to
        sign in at; the broker changes only when the owner finishes there
        (the classic /callback, which then lands on /v2)."""
        from brokers import account_tag
        p = _v2_payload(data, ())
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        with A.CONFIG_LOCK:
            _sync()
            before = {"id": cfg.get("broker") or None,
                      "paper": (account_tag(cfg).endswith(":paper")
                                if cfg.get("broker") else None)}
            # Robinhood's agentic accounts are real-money accounts
            after = {"id": "robinhood", "paper": False}
            notes = [f"It does not switch between Practice and LIVE: "
                     f"{'Practice' if A._mode(cfg) == 'practice' else 'LIVE'} "
                     "stays.", does_not_trade,
                     "Nothing changes until you finish signing in at "
                     "Robinhood."]
            if before["id"] and before["id"] != "robinhood":
                notes.append("Positions the agent opened in the account it "
                             "uses now are not sold from the new one: look "
                             "after those at your current broker.")
            wait = _two_step("robinhood", p, data, [_change(
                "broker", "Broker account", before, after,
                _broker_direction(before, after))], notes)
        if wait:
            return wait
        try:
            res = rh_start(None)
        except Exception as exc:
            return {"ok": False, "error": "Couldn't start the Robinhood "
                    f"sign-in ({_net_error_text(exc)}). Nothing was changed."}
        pending["oauth_return"] = "/v2"
        # the sign-in this starts is the next one to finish after this seq
        return {"ok": True, "url": res["url"],
                "signin_seq": pending["rh_signin"]["seq"]}

    @_v2
    def v2_pass_buy(data):
        """POST /api/v2/pass/buy: the classic /api/pass/buy (an invoice for
        N signal days), made by the second (confirmed) POST only."""
        p = _v2_payload(data, ("days",))
        days = _v2_number(p.get("days"), "days", 1, 366, whole=True)
        p = {"days": days}
        with A.CONFIG_LOCK:
            _sync()
            wait = _two_step("pass_buy", p, data, [_change(
                "signal_days", "Signal days to buy", None, days, "neutral")], [
                "Nothing is paid yet: the next step shows a Lightning invoice "
                "with its price, and nothing is paid until you pay it from "
                "your wallet.",
                "It does not switch between Practice and LIVE.",
                does_not_trade])
            fp = _fingerprint()  # what the token was just redeemed against
        if wait:
            return wait
        try:
            res = _pass_buy_route(p, _save_if_unchanged(fp))
        except _Changed:
            return {"ok": False, "error": _REVIEW_AGAIN}
        if res.get("ok") and res.get("bolt11"):
            # which purchase this invoice is (signals.purchases has its fate)
            from sources import agenthc_day_trade_ideas as hc
            extra = cfg.get("feed_pass_extra")
            for e in [cfg.get("feed_pass_pending")] + (
                    extra if isinstance(extra, list) else []):
                if (isinstance(e, dict) and isinstance(e.get("token"), str)
                        and isinstance(e.get("invoice"), dict)
                        and e["invoice"].get("bolt11") == res["bolt11"]):
                    res = {**res, "purchase": hc._token_id(e["token"])}
                    break
        return res

    @_v2
    def v2_recurring(data):
        """POST /api/v2/wallet/recurring: the classic recurring day-pass
        switch, by the second (confirmed) POST only."""
        p = _v2_payload(data, ("on",))
        if not isinstance(p.get("on"), bool):
            raise _Invalid("on must be true or false. Nothing was changed.")
        with A.CONFIG_LOCK:
            _sync()
            if p["on"] and not (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")):
                raise _Invalid("Connect a wallet first: without one there is "
                               "nothing to pay the day-pass from. Nothing was "
                               "changed.")
            was = bool(cfg.get("day_pass_recurring"))
            if was == p["on"]:
                return {"ok": True, "saved": False,
                        "note": "Nothing to change: that is your setting already."}
            cap = cfg.get("max_autopay_sats")
            wait = _two_step("recurring", p, data, [_change(
                "day_pass_recurring", "Buy a day-pass automatically", was,
                p["on"], "raises_risk" if p["on"] else "lowers_risk")], [
                ("Each payment is capped at "
                 f"{int(cap):,} sats." if isinstance(cap, (int, float))
                 and not isinstance(cap, bool) else
                 "Each payment is capped by your wallet's auto-pay limit.")
                if p["on"] else "Signal days you already bought are kept.",
                "It does not switch between Practice and LIVE.",
                does_not_trade])
            if wait:
                return wait
            res = wallet_recurring(None, p)
            return {**res, "saved": True, "settings": _settings_view()}

    def _live_preview():
        """What the owner reviews before LIVE: the view the dashboard shows
        (account, buying power, limits, real positions open)."""
        try:
            s = d_status_v2(None)
        except Exception:
            s = {}
        b = s.get("broker") or {}
        lim = s.get("limits") or {}
        return {
            "account": {"broker": b.get("id"), "name": b.get("name"),
                        "account_mask": b.get("account_mask"),
                        "paper_account": b.get("paper_account")},
            "buying_power": b.get("buying_power"),
            "buying_power_at": b.get("buying_power_at"),
            "trade_size": {"main": _book_size(cfg, ""),
                           "other": _book_size(cfg, "other_")},
            "limits": {"max_entries_per_day": cfg.get("max_entries_per_day", 5),
                       "max_daily_loss_usd": cfg.get("max_daily_loss_usd"),
                       "max_open_positions": cfg.get("max_open_positions", 10)},
            "real_positions": s.get("real_positions"),
            "cash_low": (lim.get("cash") or {}).get("low"),
        }

    # the fixed command each action runs (never text from the request)
    commands = {"pause": "pause", "resume": "resume",
                "practice": "dry on", "live": "go live", "stop": "stop"}

    def _real_left():
        """The real positions the agent holds, as the dashboard lists them
        (what a stop leaves with nobody selling them)."""
        try:
            s = d_status_v2(None)
        except Exception:
            return None  # not known
        out = []
        for x in s.get("positions") or ():
            if isinstance(x, dict) and x.get("real"):
                out.append({"id": x.get("id"), "label": x.get("label"),
                            "contract": x.get("contract"), "qty": x.get("qty"),
                            "expires_today": bool(x.get("expires_today"))})
        return out

    def _stop_preview(p, data):
        """Stop: two steps. The preview lists the real positions nobody
        sells while the agent is stopped; the token is bound to that list."""
        left = _real_left()
        return _two_step("command", p, data, [_change(
            "agent", "Agent on this computer", "running", "stopped",
            "raises_risk" if left else "neutral")], [
            "It does not sell anything on the way out: open positions stay "
            "open at your broker.",
            "It does not switch between Practice and LIVE.",
            "It does not change any setting."],
            extra={"unsold": left, "real_positions": (None if left is None
                                                      else len(left))},
            bind={"unsold": [x["id"] for x in left or ()]})

    @_v2
    def v2_command(data):
        """POST /api/v2/command: pause / resume / set_mode, through the chat
        commands' own code (apply_command) with fixed command strings."""
        import llm_policy
        p = _v2_payload(data, ("action", "mode"))
        action = p.get("action")
        if action in ("pause", "resume") and set(p) == {"action"}:
            which = action
        elif (action == "set_mode" and set(p) == {"action", "mode"}
              and p.get("mode") in ("practice", "live")):
            which = p["mode"]
        elif action == "stop" and set(p) == {"action"}:
            which = "stop"
        else:
            raise _Invalid('Send {"action": "pause"}, {"action": "resume"}, '
                           '{"action": "stop"} or {"action": "set_mode", '
                           '"mode": "practice" | "live"}. Nothing was changed.')
        if apply_command is None:
            return {"ok": False, "error": "The agent isn't running, so this "
                    "can't change now. Start it first."}
        if which == "stop":
            # a stop changes no setting and must work even when config.json
            # can't be read (as the chat's "stop"): no _sync()
            with A.CONFIG_LOCK:
                wait = _stop_preview(p, data)
            if wait:
                return wait
            handled, reply = apply_command(commands["stop"])
            if not (handled and CONTROLS["stop"]):
                return {"ok": False, "error": reply or "The agent did not stop."}
            return {"ok": True, "reply": reply}
        with A.CONFIG_LOCK:
            _sync()
            if which == "live":
                if llm_policy.entries_blocked(cfg):
                    return {"ok": False, "error": llm_policy.BLOCKED_MSG}
                if cfg.get("dry_run") is False:
                    return {"ok": True, "saved": False, "note": "Already LIVE."}
                if "confirm" in data and data.get("acknowledge") is not True:
                    return {"ok": False, "needs_acknowledge": True, "error": (
                        "Tick the box to confirm you want real orders. "
                        "Nothing was changed.")}
                wait = _two_step("command", p, data, [_change(
                    "mode", "Mode", A._mode(cfg), "live", "raises_risk")], [
                    "It does not buy or sell anything by itself: the next "
                    "signal that passes your limits becomes a real order.",
                    "It does not change your limits or trade size."],
                    extra={**_live_preview(), "needs_acknowledge": True})
                if wait:
                    return wait
            handled, reply = apply_command(commands[which])
            try:
                now = A._read_config()
            except OSError:
                now = None
            done = isinstance(now, dict) and {
                "pause": now.get("paused") is True,
                "resume": not now.get("paused"),
                "practice": now.get("dry_run") is True,
                "live": now.get("dry_run") is False}[which]
            if not (handled and done):
                return {"ok": False, "error": reply or "Nothing was changed."}
            return {"ok": True, "reply": reply}

    @_v2
    def v2_agent_start(data):
        """POST /api/v2/agent/start: the setup wizard's "Start my agent now"
        (start_local, its refusals included). Two steps when the saved mode
        is LIVE: the agent then places real orders from its first signal."""
        p = _v2_payload(data, ())
        if get_status is not None:
            return {"ok": False, "error": "The agent is already running. "
                    "Nothing was changed."}
        with A.CONFIG_LOCK:
            _sync()
            refused = _start_refused()
            if refused:
                return {"ok": False, "error": refused}
            if A._mode(cfg) == "live":
                wait = _two_step("agent_start", p, data, [_change(
                    "agent", "Agent on this computer", "stopped", "running",
                    "raises_risk")], [
                    "It does not change a setting: it runs with them as saved.",
                    "It does not switch to Practice: it starts in LIVE, and the "
                    "next signal that passes your limits becomes a real order.",
                    "It does not sell anything by itself when it starts."],
                    extra={**_live_preview(), "mode": "live"})
                if wait:
                    return wait
        return start_local(None, {})

    def _moving_preview_facts():
        left = _real_left()
        return {"unsold": left,
                "real_positions": None if left is None else len(left),
                "agent_running": get_status is not None}

    @_v2
    def v2_move_finish(data):
        """POST /api/v2/move/finish: "I saved the file": retire this
        computer and stop the agent here (the classic /api/move/finish),
        applied by the second (confirmed) POST only."""
        p = _v2_payload(data, ())
        if not pending.get("move_blob"):
            raise _Invalid("Create and download your move file first: this "
                           "computer is retired only after that. Nothing was "
                           "changed.")
        with A.CONFIG_LOCK:
            wait = _two_step("move_finish", p, data, [_change(
                "this_computer", "This computer", "runs the agent",
                "retired: it never runs this setup again", "neutral")], [
                "It does not sell anything: open positions stay open at your "
                "broker, and the agent on the new computer sells them on "
                "their exit signals once you start it there.",
                "It does not delete anything on this computer.",
                "It does not undo by itself: to run this setup here again, "
                "stop the other computer first, then run: python agent.py "
                "resume-here"],
                extra=_moving_preview_facts())
        if wait:
            return wait
        return move_finish(None, {})

    @_v2
    def v2_move_deploy(data):
        """POST /api/v2/move/deploy: move to a new DigitalOcean server (the
        classic /api/deploy, its lock and refusals unchanged), by the second
        (confirmed) POST only. The token is never sent back."""
        import provision
        p = _v2_payload(data, ("token", "region"))
        token = _v2_text(p.get("token"), "token", longest=500)
        region = p.get("region", "nyc3")
        if region not in provision.REGIONS:
            raise _Invalid("region must be one of: " + ", ".join(provision.REGIONS)
                           + ". Nothing was changed.")
        p = {"token": token, "region": region}
        with A.CONFIG_LOCK:
            wait = _two_step("move_deploy", p, data, [_change(
                "this_computer", "Where the agent runs", "this computer",
                f"a new DigitalOcean server ({region})", "neutral")], [
                "DigitalOcean bills your own account for the server.",
                f"If DigitalOcean has no room for the $6 server in {region} "
                "right now, the agent makes it in a nearby region instead, or, "
                f"if none has room, as a bigger size in {region} that costs a "
                "little more. What it made is said once the server exists.",
                "It does not sell anything: open positions stay open at your "
                "broker; the server sells them on their exit signals.",
                "It does not leave this computer trading: once the server is "
                "made, this computer is retired and the agent here stops.",
                "It does not switch between Practice and LIVE."],
                extra=_moving_preview_facts())
        if wait:
            return wait
        return deploy(None, p)

    # ── Settings → Code changes ──
    @_v2
    def v2_code(data):
        """POST /api/v2/settings/code: let the AI helper propose code changes
        (the key the chat's "self-edit on|off" writes). Proposing changes
        nothing: every change is reviewed and applied on its own, in two
        steps."""
        p = _v2_payload(data, ("self_edit_enabled",))
        if set(p) != {"self_edit_enabled"} or not isinstance(
                p["self_edit_enabled"], bool):
            raise _Invalid("Send self_edit_enabled: true or false. Nothing was "
                           "changed.")
        with A.CONFIG_LOCK:
            _sync()
            cfg["self_edit_enabled"] = p["self_edit_enabled"]
            save()
            return {"ok": True, "saved": True, "settings": _settings_view()}

    @_v2
    def v2_proposal_request(data):
        """POST /api/v2/proposal/request: ask the AI helper to write a code
        change (the chat's "code: …"), by calling self_edit.propose itself:
        the words are only ever a request to the AI, never a command (no
        apply_command), and proposing changes nothing until it is applied."""
        import llm_client
        import self_edit
        p = _v2_payload(data, ("request",))
        req = p.get("request")
        if set(p) != {"request"} or not isinstance(req, str) or not req.strip():
            raise _Invalid("Describe the change you want. Nothing was changed.")
        if len(req) > 4000 or any(ord(ch) < 32 and ch not in "\n\t" for ch in req):
            raise _Invalid("The description must be plain text of at most 4,000 "
                           "characters. Nothing was changed.")
        if not self_edit.enabled(cfg):
            return {"ok": False, "error": "Turn on “Let the AI helper propose "
                    "code changes” first. Nothing was changed."}
        if not llm_client.configured(cfg):
            return {"ok": False, "error": "Connect an AI helper first (Settings, "
                    "under Connections). Nothing was changed."}
        # as the chat's "code: …": the settings on disk, the request trimmed
        ok, msg = self_edit.propose(A._load(A.CONFIG_PATH, {}) or {}, req.strip())
        if not ok:
            return {"ok": False, "error": msg}
        return {"ok": True, "note": msg}

    @_v2
    def v2_proposal_apply(data):
        """POST /api/v2/proposal/apply {diff_sha256}: apply the pending code
        change and restart (the classic /api/proposal/apply), by the second
        (confirmed) POST only. diff_sha256 is the sha256 of the diff the owner
        read (GET /api/proposal); a proposal with any other diff is never
        applied, however it got there."""
        import re as _re
        import self_edit
        p = _v2_payload(data, ("diff_sha256",))
        sha = p.get("diff_sha256")
        if set(p) != {"diff_sha256"} or not isinstance(sha, str) \
                or not _re.fullmatch(r"[0-9a-f]{64}", sha):
            raise _Invalid("diff_sha256 must be the sha256 of the change you "
                           "reviewed. Nothing was changed.")
        prop = self_edit.current()
        if not isinstance(prop, dict):
            return {"ok": False, "error": "No code change is waiting. Nothing "
                    "was changed."}
        if self_edit.diff_truncated(prop):
            return {"ok": False, "error": "This change is too large to show "
                    "here in full, so part of it was never shown and it can't "
                    "be applied from this page. Reject it and ask for a "
                    "smaller change. Nothing was changed."}
        if self_edit.diff_sha256(prop) != sha:
            return {"ok": False, "error": "The code change waiting is not the "
                    "one you reviewed. Read it again. Nothing was changed."}
        stale = self_edit.base_problem(prop)
        if stale:
            return {"ok": False, "error": f"This change can't be applied: "
                    f"{stale}. Reject it and ask again. Nothing was changed."}
        files = [str(ch.get("path", ""))[:200] for ch in prop.get("changes", [])
                 if isinstance(ch, dict)]
        restarts = get_status is not None
        with A.CONFIG_LOCK:
            wait = _two_step("proposal_apply", p, data, [_change(
                "code", "The agent's code", "as it is now",
                f"{len(files)} file{'s' if len(files) != 1 else ''} changed by "
                "the AI helper", "neutral")], [
                "It does not switch between Practice and LIVE, and does not "
                "change any setting.",
                "Files the agent may never rewrite (its agreement check, safety "
                "rails, order code and this dashboard's server) are refused: "
                "then nothing is applied and the change is thrown away.",
                "If the new code does not compile or load, every file is put "
                "back as it was and nothing restarts."],
                extra={"diff_sha256": sha, "files": files[:50],
                       "warnings": [str(w)[:300] for w in prop.get("warnings", [])][:20],
                       "restarts": restarts})
        if wait:
            return wait
        # the classic apply, held to the reviewed diff where it reads the file
        ok, msg = self_edit.apply_and_restart(restart=restarts, expect_sha256=sha)
        return {"ok": ok, "reply": msg} if ok else {"ok": False, "error": msg}

    # ── Signals & pass ──
    def _scope_now():
        """The saved "which signals" as the classic source form sends it, so
        a /v2 save through _source_save never changes it."""
        return "all" if cfg.get("include_other_trades") else "main"

    def _source_now():
        return {"source": cfg.get("source") or None,
                "site": (_host_only(cfg["source_url"])
                         if cfg.get("source") == "url" and cfg.get("source_url")
                         else None)}

    def _feed_url_ok(url):
        # the classic source form's rule for a feed or wallet address
        return (url.startswith("https://") or url.startswith("http://127.0.0.1")
                or url.startswith("http://localhost"))

    def _no_agenthc_access():
        return not (cfg.get("agenthc_api_key") or cfg.get("feed_pass")
                    or cfg.get("feed_pass_pending")
                    or (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")))

    @_v2
    def v2_source(data):
        """POST /api/v2/settings/source: the signal source (two steps:
        {source: "agenthc" | "manual" | "url", url}) or, on its own, the
        AgentHC subscription key ({agenthc_key}, saved straight away; never
        sent back). Both through the classic /api/source code, with the saved
        "which signals" choice passed along unchanged."""
        p = _v2_payload(data, ("source", "url", "agenthc_key"))
        if "agenthc_key" in p:
            if set(p) != {"agenthc_key"}:
                raise _Invalid("Send the subscription key on its own. Nothing "
                               "was changed.")
            key = _v2_text(p["agenthc_key"], "agenthc_key", longest=500)
            with A.CONFIG_LOCK:
                _sync()
                if cfg.get("source", "agenthc") != "agenthc":
                    raise _Invalid("The subscription key is for the AgentHC "
                                   "signal source: choose it first. Nothing was "
                                   "changed.")
                res = _source_save({"source": "agenthc", "scope": _scope_now(),
                                    "agenthc_key": key}, save)
                if res.get("ok"):
                    res = {"ok": True, "saved": True,
                           "note": "Subscription key saved.",
                           "settings": _settings_view()}
                return res
        src = p.get("source")
        if src not in ("agenthc", "manual", "url"):
            raise _Invalid('source must be "agenthc", "manual" or "url". Nothing '
                           "was changed.")
        if src == "url":
            url = _v2_text(p.get("url"), "url", longest=2000)
            if not _feed_url_ok(url):
                raise _Invalid("url must start with https:// (or be on this "
                               "computer). Nothing was changed.")
            p = {"source": "url", "url": url}
        elif set(p) != {"source"}:
            raise _Invalid("url goes with source 'url' only. Nothing was "
                           "changed.")
        with A.CONFIG_LOCK:
            _sync()
            before, after = _source_now(), {
                "source": src, "site": _host_only(p["url"]) if src == "url" else None}
            if before == after and (src != "url" or cfg.get("source_url") == p["url"]):
                return {"ok": True, "saved": False,
                        "note": "Nothing to change: that is your signal source already."}
            if src == "agenthc" and _no_agenthc_access():
                raise _Invalid("To read AgentHC's signals the agent needs signal "
                               "days, a subscription key or a payment wallet: "
                               "add one on this page first. Nothing was changed.")
            left = _real_left()
            wait = _two_step("source", p, data, [_change(
                "source", "Signal source", before, after, "neutral")], [
                "It does not switch between Practice and LIVE.", does_not_trade,
                "Positions the agent holds now are sold on exit signals from "
                "the new source only: if it never sends one for them, sell "
                "them yourself at your broker."],
                extra={"real_positions": None if left is None else len(left)})
            if wait:
                return wait
            back = {"source": src, "source_url": p.get("url", ""),
                    "scope": _scope_now()}
            saved_key = cfg.get("agenthc_api_key")
            if src == "agenthc" and isinstance(saved_key, str) and saved_key.strip():
                # the classic form with the key on file filled in: a
                # subscriber with no pass or wallet can go back to AgentHC
                # (the key is saved again as it is, nothing else changes)
                back["agenthc_key"] = saved_key
            res = _source_save(back, save)
            if res.get("ok"):
                res = {"ok": True, "saved": True, "settings": _settings_view()}
            return res

    @_v2
    def v2_signals(data):
        """POST /api/v2/settings/signals: which AgentHC signals (scope "main"
        or "all": the classic /api/source/scope, two steps) and the practice
        preview on/off (the key the chat's "preview on|off" writes; straight
        away when sent alone)."""
        p = _v2_payload(data, ("scope", "dry_run_preview"))
        if not p:
            raise _Invalid("Send scope and/or dry_run_preview. Nothing was "
                           "changed.")
        if "scope" in p and p["scope"] not in ("main", "all"):
            raise _Invalid('scope must be "main" or "all". Nothing was changed.')
        if "dry_run_preview" in p and not isinstance(p["dry_run_preview"], bool):
            raise _Invalid("dry_run_preview must be true or false. Nothing was "
                           "changed.")
        with A.CONFIG_LOCK:
            _sync()
            changes = []
            if "scope" in p:
                if cfg.get("source", "agenthc") != "agenthc":
                    raise _Invalid("Which signals is a choice of the AgentHC "
                                   "source only. Nothing was changed.")
                if p["scope"] != _scope_now():
                    changes.append(_change(
                        "scope", "Which signals", _scope_now(), p["scope"],
                        "raises_risk" if p["scope"] == "all" else "lowers_risk"))
            if changes:
                wait = _two_step("signals", p, data, changes, [
                    "It does not switch between Practice and LIVE.",
                    does_not_trade,
                    "It does not change your trade sizes: other trades are "
                    "sized as Rules & limits says."])
                if wait:
                    return wait
            elif "dry_run_preview" not in p:
                return {"ok": True, "saved": False,
                        "note": "Nothing to change: that is your setting already."}
            if "dry_run_preview" in p:
                cfg["dry_run_preview"] = p["dry_run_preview"]
            if changes:
                res = source_scope(None, {"scope": p["scope"]})  # saves
                if not res.get("ok"):
                    return res
            else:
                save()
            return {"ok": True, "saved": True, "settings": _settings_view()}

    def _wallet_now():
        return ({"site": _host_only(cfg["lnbits_url"])}
                if cfg.get("lnbits_url") and cfg.get("lnbits_admin_key") else None)

    def _wallet_notes(had):
        return ([("Your current wallet is not emptied or deleted: sats left in "
                  "it stay there. Its details are saved to a file in the "
                  "agent's folder first, so you can still open it.")]
                if had else []) + [
            "It does not switch between Practice and LIVE.", does_not_trade]

    @_v2
    def v2_wallet_create(data):
        """POST /api/v2/wallet/create: make the agent's own wallet (the
        classic /api/wallet/create), or replace it ({replace: true}), by the
        second (confirmed) POST only. The new wallet's page link is never
        sent back (it opens the wallet: POST /api/v2/wallet/page)."""
        import lightning_wallet
        p = _v2_payload(data, ("replace",))
        if not isinstance(p.get("replace", False), bool):
            raise _Invalid("replace must be true or false. Nothing was changed.")
        if not A.consent_ok():  # as the classic /api/wallet/create
            return {"ok": False, "error": "accept the agreement first"}
        with A.CONFIG_LOCK:
            _sync()
            had = _wallet_now()
            if had and p != {"replace": True}:
                raise _Invalid("Your agent already has a wallet: send replace: "
                               "true to replace it. Nothing was changed.")
            if not had and p not in ({}, {"replace": False}):
                raise _Invalid("There is no wallet to replace. Nothing was "
                               "changed.")
            new = str(lightning_wallet.DEFAULT_LNBITS or "")
            changes = [_change("wallet", "Payment wallet", had,
                               {"site": _host_only(new), "new": True}, "neutral")]
            if cfg.get("source") != "agenthc":
                changes.append(_change("source", "Signal source", _source_now(),
                                       {"source": "agenthc", "site": None}, "neutral"))
            wait = _two_step("wallet_create", p, data, changes, [
                "It does not put any sats in the new wallet: it starts empty."]
                + _wallet_notes(bool(had)),
                extra={"warning": lightning_wallet.demo_warning(new) or None})
            fp = _fingerprint()  # what the token was just redeemed against
        if wait:
            return wait
        # LNbits is asked outside the lock (it can take seconds); the switch
        # to the new wallet happens only if nothing was saved meanwhile (a
        # wallet or source another tab confirmed is never overwritten)
        try:
            res = _wallet_create(cfg, save, {"replace": True} if had else {},
                                 switching=_unchanged_since(fp))
        except _Changed:
            return {"ok": False, "error": _REVIEW_AGAIN}
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")
                    or "The wallet was not made. Nothing was changed."}
        note = "Wallet made. It starts empty."
        if res.get("old_wallet_file"):
            note += (" Your old wallet was not deleted: if it still holds sats, "
                     "move them to the new one. Its details are saved in "
                     f"{res['old_wallet_file']}.")
        return {"ok": True, "saved": True, "note": note,
                "warning": res.get("warning") or None,
                "old_wallet_file": res.get("old_wallet_file") or None,
                "settings": _settings_view()}

    @_v2
    def v2_wallet_fund(data):
        """POST /api/v2/wallet/fund {sats}: an invoice to add sats to the
        agent's wallet (the classic /api/wallet/fund's), made by the second
        (confirmed) POST only. The classic form raises a small amount to
        1,000 sats and cuts a big one to the wallet's invoice cap; /v2
        refuses both and says the range."""
        p = _v2_payload(data, ("sats",))
        with A.CONFIG_LOCK:
            _sync()
            w = _wallet()
            if w is None:
                raise _Invalid("Your agent has no wallet yet: make one first. "
                               "Nothing was changed.")
            cap = int(cfg.get("lnbits_max_invoice_sats") or 0)
            sats = _v2_number(p.get("sats"), "sats", 1000, cap or 100_000_000,
                              whole=True)
            p = {"sats": sats}
            auto = cfg.get("max_autopay_sats")
            wait = _two_step("wallet_fund", p, data, [_change(
                "wallet_fund", "Add to the agent's wallet", None, sats,
                "neutral")], [
                "Nothing is paid yet: the next step shows a Lightning invoice, "
                "and nothing is paid until you pay it from your own wallet app.",
                ("The agent spends from this wallet only on day-passes, each "
                 f"capped at {int(auto):,} sats." if isinstance(auto, (int, float))
                 and not isinstance(auto, bool) else
                 "The agent spends from this wallet only on day-passes."),
                "It does not switch between Practice and LIVE.", does_not_trade])
        if wait:
            return wait
        return _wallet_fund_invoice(w, sats, save)

    @_v2
    def v2_wallet_connect(data):
        """POST /api/v2/wallet/connect {url, admin_key}: use your own LNbits
        wallet instead of the agent's (the classic /api/source's wallet
        part: it checks the wallet answers, and saves the old one's details
        to a file first), by the second (confirmed) POST only. The key is
        never sent back."""
        p = _v2_payload(data, ("url", "admin_key"))
        url = _v2_text(p.get("url"), "url", longest=500)
        key = _v2_text(p.get("admin_key"), "admin_key", longest=500)
        if not _feed_url_ok(url):
            raise _Invalid("url must start with https:// (or be on this "
                           "computer). Nothing was changed.")
        p = {"url": url, "admin_key": key}
        with A.CONFIG_LOCK:
            _sync()
            had = _wallet_now()
            if had and cfg.get("lnbits_admin_key") == key and cfg.get("lnbits_url") == url:
                return {"ok": True, "saved": False,
                        "note": "Nothing to change: that is your wallet already."}
            changes = [_change("wallet", "Payment wallet", had,
                               {"site": _host_only(url), "new": False}, "neutral")]
            if cfg.get("source") != "agenthc":
                changes.append(_change("source", "Signal source", _source_now(),
                                       {"source": "agenthc", "site": None}, "neutral"))
            same_wallet = bool(had) and cfg.get("lnbits_admin_key") == key
            wait = _two_step("wallet_connect", p, data, changes,
                             ["The agent checks the wallet answers before it "
                              "saves anything."] + _wallet_notes(bool(had) and not same_wallet))
            fp = _fingerprint()  # what the token was just redeemed against
            scope = _scope_now()
        if wait:
            return wait
        try:
            res = _source_save({"source": "agenthc", "scope": scope,
                                "lnbits_url": url, "lnbits_key": key},
                               _save_if_unchanged(fp))
        except _Changed:
            return {"ok": False, "error": _REVIEW_AGAIN}
        if res.get("ok"):
            res = {**res, "saved": True, "settings": _settings_view()}
        return res

    @_v2
    def v2_wallet_page(data):
        """POST /api/v2/wallet/page: the agent's wallet page link, for the
        button that opens it (a bearer link: whoever has it can spend the
        wallet, so it is never in a GET, a page or a log)."""
        _v2_payload(data, ())
        with A.CONFIG_LOCK:
            _sync()
            page = str(cfg.get("lnbits_wallet_page") or "")
        if not (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")) or not page:
            return {"ok": False, "error": "No wallet page is on file for your "
                    "agent's wallet (a wallet you connected yourself opens "
                    "from your own LNbits)."}
        if not page.startswith("https://") and not _feed_url_ok(page):
            return {"ok": False, "error": "The wallet page on file is not a web "
                    "address this page can open."}
        return {"ok": True, "url": page}

    @_v2
    def v2_ask(data):
        """POST /api/v2/ask {question}: "Ask your agent" on Today. Answer
        only: the answer function the classic chat falls back to (_ask_llm,
        with the same settings, status fields and recent trades d_command
        hands it), and never the chat's commands: no apply_command, no
        "code:", nothing saved. What reads like a command is a question."""
        import llm_client
        if not isinstance(data, dict) or set(data) != {"question"}:
            raise _Invalid("Send {question}: the question only.")
        q = data["question"]
        if not isinstance(q, str) or not q.strip():
            raise _Invalid("Type a question first.")
        if len(q) > 2000 or any(ord(ch) < 32 and ch not in "\n\t" for ch in q):
            raise _Invalid("The question must be plain text of at most 2,000 "
                           "characters.")
        if apply_command is None:
            # as d_command: with no agent behind this server there is no
            # status or trade log to answer from
            return {"ok": False, "error": "The agent isn't running, so it can't "
                    "answer yet. Start it in Settings, under The agent on this "
                    "computer."}
        now = cfg_getter() if cfg_getter else cfg
        if not llm_client.configured(now):
            return {"ok": False, "error": "No AI helper is connected, so there is "
                    "nobody to answer. Connect one in Settings, under "
                    "Connections."}
        answer = _ask_llm(now, q.strip(), d_status(None).get("fields", {}),
                          get_trades() if get_trades else [],
                          system=_V2_ASK_SYSTEM)
        return {"ok": True, "answer": str(answer)}

    W.routes_get = {"/api/state": state, "/api/rh/start": rh_start,
                    "/api/wallet/balance": wallet_balance,
                    "/api/deploy/status": deploy_status,
                    "/api/brokers": brokers_list,
                    "/api/broker/cash": broker_cash,
                    "/api/update/check": update_check,
                    "/api/help/netcheck": help_netcheck,
                    "/api/help/report": help_report,
                    "/api/wallet/quote": wallet_quote,
                    "/api/llm/providers": llm_providers,
                    "/api/pass/status": pass_status_route,
                    "/api/pass/options": pass_options_route,
                    "/api/tip/options": _tip_options_route,
                    "/api/status": d_status, "/api/trades": d_trades,
                    "/api/pnl": d_pnl,
                    "/api/v2/status": d_status_v2,
                    "/api/v2/activity": d_activity_v2,
                    "/api/v2/settings": v2_settings,
                    "/api/v2/agreement": v2_agreement,
                    "/api/proposal": proposal_get,
                    "/api/autostart": autostart_get,
                    "/api/move/download": move_download}
    def ask(_h, data):
        q = str(data.get("question", "")).strip()
        wiz_state = _llm_safe_state(state(None))
        reply = _ask_llm(cfg, q, wiz_state, [], system=(
            "You are the setup assistant embedded in the user's own "
            "agentic-trader install wizard. Help them complete setup: explain "
            "LNbits wallets and admin keys, the sats day-pass, Robinhood "
            "Agentic accounts and options approval, dry-run mode, sizing "
            "choices, and the policy file. Never advise position sizing or "
            "whether to trade; never tell them to skip the agreement. Point "
            "at GETTING_STARTED.md / SECURITY.md for detail. Be concise."))
        return {"reply": reply}

    W.routes_post = {"/api/consent": consent, "/api/source": source,
                     "/api/source/scope": source_scope,
                     "/api/sizing": sizing, "/api/safety": safety,
                     "/api/finish": finish, "/api/ask": ask,
                     "/api/policy/disable": policy_disable,
                     "/api/start-local": start_local,
                     "/api/policy": policy_save,
                     "/api/move/export": move_export,
                     "/api/move/finish": move_finish,
                     "/api/move/import": move_import,
                     "/api/autostart": autostart_set,
                     "/api/wallet/create": wallet_create,
                     "/api/pass/buy": pass_buy,
                     "/api/tip/create": _tip_create,
                     "/api/tip/status": _tip_status_route,
                     "/api/wallet/fund": wallet_fund,
                     "/api/llm": llm, "/api/llm/models": llm_models,
                     "/api/deploy": deploy,
                     "/api/update/pull": update_pull,
                     "/api/wallet/recurring": wallet_recurring,
                     "/api/broker/connect": broker_connect,
                     "/api/proposal/apply": proposal_apply,
                     "/api/proposal/reject": proposal_reject,
                     "/api/command": d_command,
                     "/api/v2/settings/limits": v2_limits,
                     "/api/v2/settings/rules": v2_rules,
                     "/api/v2/settings/alerts": v2_alerts,
                     "/api/v2/settings/computer": v2_computer,
                     "/api/v2/settings/ai": v2_ai,
                     "/api/v2/broker/connect": v2_broker_connect,
                     "/api/v2/broker/robinhood": v2_robinhood,
                     "/api/v2/pass/buy": v2_pass_buy,
                     "/api/v2/wallet/recurring": v2_recurring,
                     "/api/v2/command": v2_command,
                     "/api/v2/agent/start": v2_agent_start,
                     "/api/v2/move/finish": v2_move_finish,
                     "/api/v2/move/deploy": v2_move_deploy,
                     "/api/v2/settings/code": v2_code,
                     "/api/v2/proposal/request": v2_proposal_request,
                     "/api/v2/proposal/apply": v2_proposal_apply,
                     "/api/v2/settings/source": v2_source,
                     "/api/v2/settings/signals": v2_signals,
                     "/api/v2/wallet/create": v2_wallet_create,
                     "/api/v2/wallet/fund": v2_wallet_fund,
                     "/api/v2/wallet/connect": v2_wallet_connect,
                     "/api/v2/wallet/page": v2_wallet_page,
                     "/api/v2/ask": v2_ask}

    W.routes_get = {p: _fresh(fn) for p, fn in W.routes_get.items()}
    W.routes_post = {p: _fresh(fn) for p, fn in W.routes_post.items()}

    class _Redirect(Exception):
        pass

    # patch GET to turn /callback success into a browser redirect to /
    orig_get = W.do_GET

    def do_GET(self):
        if self.path.startswith("/callback"):
            if not self._guard(check_origin=False):
                return
            try:
                _fresh(rh_callback, plain_errors=False)(self)
            except _Redirect:
                pass
            except Exception as exc:
                if pending["oauth"]:
                    _rh_signin_done(False, error=str(exc)[:200]
                                    or type(exc).__name__)
                return self._send(None, html=f"<h3>OAuth failed: {exc}</h3>"
                                             f"<p><a href='/'>back to setup</a></p>")
            self.send_response(302)
            # "/" as always; "/v2" only for a sign-in /v2 started (a fixed
            # value set by the server, never taken from the request)
            back = pending.get("oauth_return")
            pending["oauth_return"] = None
            self.send_header("Location", back if back in _RETURN_TO.values() else "/")
            self.end_headers()
            return
        return orig_get(self)

    W.do_GET = do_GET

    server = _AppServer(("127.0.0.1", APP_PORT), W)
    url = f"http://127.0.0.1:{APP_PORT}/"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    if not wait_finish:
        start_remote(W)
    if wait_finish:
        login = f"{url}setup?k={ui_token()}"
        print(f"Setup running at {login}  (Ctrl-C to abort)")
        print("On a remote server, tunnel first:  "
              f"ssh -L {APP_PORT}:127.0.0.1:{APP_PORT} user@host")
        try:
            import os as _os
            # After an update-button re-exec the user's tab reloads itself —
            # opening a second tab would just duplicate the wizard.
            if not _os.environ.pop("AGENT_UPDATE_RESTARTED", None):
                webbrowser.open(login)
        except Exception:
            pass
        try:
            pending["finished"].wait()
            print("Setup complete. If you clicked \"Start my agent now\", it "
                  "is now running in the background and the browser page "
                  "becomes its dashboard — you can close this window, the "
                  "agent keeps going. Stop it from the dashboard or with: "
                  "agent.py stop. Not started yet? Run: agent.py start")
        except KeyboardInterrupt:
            print("\nWizard aborted.")
        server.shutdown()
        return None
    return f"{url}dash?k={ui_token()}"


def run_wizard():
    start_app(wait_finish=True)


def _repo_dir():
    import os
    return os.path.dirname(os.path.abspath(__file__))


# ── code updates (wizard "check for updates" button) ────────────────────────

def _git(args, timeout=60):
    import os
    import subprocess
    # Never let git wait for a password/credential prompt nobody can see
    # (a stuck prompt made the Update button look like it did nothing).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    return subprocess.run(["git", "-C", _repo_dir(), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


_FETCH_TIMEOUT_S = 45
_NET_HINTS = ("could not resolve", "unable to access", "ssl", "tls", "timed out",
              "failed to connect", "connection", "proxy", "certificate",
              "network", "unreachable", "schannel", "gnutls")


def _snapshot_mode():
    """No git checkout (zip install) or no git on PATH → update from
    GitHub's zip of the reviewed commit instead (snapshot_update)."""
    import os
    import shutil
    return (not os.path.isdir(os.path.join(_repo_dir(), ".git"))
            or shutil.which("git") is None)


def _install_write_problem():
    """None if the updater can change files in the install folder, else why
    not, in the owner's words. Asked BEFORE anything is downloaded: under the
    systemd unit shipped before 2026-11 (ProtectSystem=strict with only the
    state folder writable) git and pip hit a read-only filesystem, so every
    server stayed on its deploy-day code with only a raw git error to show."""
    import errno
    import os
    import sys
    import tempfile
    repo = _repo_dir()
    try:
        fd, probe = tempfile.mkstemp(prefix=".update-write-test-", dir=repo)
        os.close(fd)
        os.unlink(probe)
        return None
    except OSError as exc:
        why = exc
    if os.getenv("INVOCATION_ID") and why.errno == errno.EROFS:
        paths = [os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader")),
                 repo]
        venv = os.path.abspath(sys.prefix)
        if (sys.prefix != sys.base_prefix
                and not venv.startswith(os.path.join(repo, ""))):
            paths.append(venv)  # pip installs there
        return (
            "Can't update: the background service on this server is not "
            "allowed to change the agent's own files (its service file is "
            "from an older version). Nothing was changed. To fix it once, "
            "log in to the server, run this one line, then press Update "
            "again:\n"
            f"  sudo sed -i 's|^ReadWritePaths=.*|ReadWritePaths="
            f"{' '.join(paths)}|' /etc/systemd/system/agentic-trader.service"
            " && sudo systemctl daemon-reload"
            " && sudo systemctl restart agentic-trader\n"
            "It restarts the agent; your settings, positions and trade "
            "history are kept.")
    return (f"Can't update: the agent can't write to its own folder ({repo}): "
            f"{why.strerror or why}. Nothing was changed. Run the agent as "
            "the user who owns that folder (in Docker, rebuild the image "
            "instead of using this button).")


def _github_unreachable(raw):
    """Error text for a failed update download: the diagnosed cause + fix
    when it looks like a network problem, else the raw error."""
    raw = str(raw or "").strip()
    if any(h in raw.lower() for h in _NET_HINTS):
        try:
            import net_check
            diag = net_check.diagnose("github.com")
            if not diag["ok"]:
                return net_check.summary(diag, name="GitHub (where updates come from)")
        except Exception:
            pass
    return raw[:300] or "the update download failed"


def _fetch_origin():
    """git fetch with a short timeout. Returns an error string or None."""
    import subprocess
    try:
        f = _git(["fetch", "--quiet", "origin"], timeout=_FETCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _github_unreachable(
            f"fetch timed out after {_FETCH_TIMEOUT_S}s (network timed out)")
    if f.returncode != 0:
        return "fetch failed: " + _github_unreachable((f.stderr or f.stdout).strip())
    return None


def _update_upstream():
    """The remote ref updates come from: the current branch's origin twin if
    it exists, else origin/main (detached HEAD, local-only branches)."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    candidate = f"origin/{branch}" if branch and branch != "HEAD" else "origin/main"
    if _git(["rev-parse", "--verify", "--quiet", candidate]).returncode != 0:
        candidate = "origin/main"
    return candidate


# Updates are only ever pulled from the official repository. A tampered
# `origin` (another local program editing .git/config, a typo'd fork) would
# otherwise let the Update button install and run arbitrary code. Forks set
# AGENTIC_TRADER_UPDATE_REMOTE to their own URL explicitly.
OFFICIAL_REMOTES = (
    "https://github.com/traderhc123/agentic-trader",
    "https://github.com/traderhc123/agentic-trader.git",
    "git@github.com:traderhc123/agentic-trader.git",
    "ssh://git@github.com/traderhc123/agentic-trader.git",
)


def _remote_problem():
    """None if origin is the trusted update source, else an error string."""
    import os
    url = _git(["remote", "get-url", "origin"]).stdout.strip()
    allowed = set(OFFICIAL_REMOTES)
    if os.getenv("AGENTIC_TRADER_UPDATE_REMOTE"):
        allowed.add(os.environ["AGENTIC_TRADER_UPDATE_REMOTE"].strip())
    if url.rstrip("/") in allowed:
        return None
    return (f"refusing to update from an unrecognised remote ({url or 'none'}) — "
            "updates only come from github.com/traderhc123/agentic-trader "
            "(forks: set AGENTIC_TRADER_UPDATE_REMOTE to your repo URL)")


# The update check's last answer, given again for UPDATE_CHECK_MEMO_S (an
# error for UPDATE_ERROR_MEMO_S): every page load asks, and each check is a
# `git fetch` or a GitHub API call (60 an hour per address without a login).
# Keyed by where updates come from and what is installed, so an update, a
# changed remote or a new snapshot asks again. "Check now" passes force=True.
UPDATE_CHECK_MEMO_S = 900
UPDATE_ERROR_MEMO_S = 60
_UPDATE_MEMO = {}


def _update_memo_key():
    """What a remembered answer depends on: the mode, the remote and the
    installed commit (all local reads). None when it can't be told."""
    try:
        if _snapshot_mode():
            import snapshot_update
            return ("snapshot", snapshot_update.installed_commit(_repo_dir()))
        return ("git", _git(["remote", "get-url", "origin"]).stdout.strip(),
                _git(["rev-parse", "HEAD"]).stdout.strip())
    except Exception:
        return None


def check_code_updates(force=False):
    """Fetch origin and report how far behind HEAD is. Never raises.
    Returns the exact commit (`target`) the user is shown, so the pull can
    install THAT commit and nothing pushed after they looked.

    The answer is remembered for UPDATE_CHECK_MEMO_S (errors for
    UPDATE_ERROR_MEMO_S) and carries `checked_at` (ISO, UTC) — when it was
    really checked; force=True checks again now."""
    import time as _t
    from datetime import datetime as _dt, timezone as _tz
    key = _update_memo_key()
    memo = _UPDATE_MEMO
    if (not force and key is not None and memo.get("key") == key
            and _t.monotonic() - memo["at"] < (
                UPDATE_CHECK_MEMO_S if memo["result"].get("ok")
                else UPDATE_ERROR_MEMO_S)):
        return dict(memo["result"])
    result = _check_code_updates()
    result = dict(result) if isinstance(result, dict) else {
        "ok": False, "error": "update check failed"}
    result["checked_at"] = _dt.now(_tz.utc).isoformat(timespec="seconds")
    memo.clear()
    if key is not None:
        memo.update(key=key, at=_t.monotonic(), result=dict(result))
    return result


def _forget_update_check():
    """An update was installed (or tried): the next check asks again."""
    _UPDATE_MEMO.clear()


def _check_code_updates():
    problem = _install_write_problem()
    if problem:
        return {"ok": False, "error": problem}
    if _snapshot_mode():
        import snapshot_update
        try:
            return snapshot_update.check(_repo_dir())
        except Exception as exc:
            return {"ok": False, "error": _github_unreachable(exc)}
    try:
        problem = _remote_problem()
        if problem:
            return {"ok": False, "error": problem}
        err = _fetch_origin()
        if err:
            return {"ok": False, "error": err}
        upstream = _update_upstream()
        behind = _git(["rev-list", "--count", f"HEAD..{upstream}"])
        if behind.returncode != 0:
            return {"ok": False,
                    "error": f"could not compare against {upstream}"}
        n = int(behind.stdout.strip() or 0)
        summary = ""
        if n:
            log = _git(["log", "--oneline", "--no-decorate", "-20",
                        f"HEAD..{upstream}"])
            summary = log.stdout.strip()
        target = _git(["rev-parse", upstream]).stdout.strip()
        return {"ok": True, "behind": n, "upstream": upstream,
                "summary": summary, "target": target}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _restart_or_tell(argv, done, now_it, to_finish, after="", port=None,
                     quiet=True, urgent=False):
    """Restart the agent (update, move-file import, self-edit).
    restart_self waits for an order that is still being placed; if that
    order is stuck and it gives up, the owner is told what did and did not
    happen — the page said "restarting", and nothing may be left waiting or
    paused with nobody knowing. `done`: what already happened; `now_it`:
    what the agent is doing instead; `to_finish`: how the instruction to
    restart it by hand begins; `after`: what to do after that. `quiet`: the
    restarted copy does not open a browser window."""
    import platform_compat
    if quiet:
        os.environ["AGENT_UPDATE_RESTARTED"] = "1"
    if platform_compat.restart_self(
            argv, APP_PORT if port is None else port) is not False:
        return True
    if quiet:
        os.environ.pop("AGENT_UPDATE_RESTARTED", None)
    msg = (f"agentic-trader: {done}, but the agent did NOT "
           "restart, because an order was still being placed and restarting "
           f"in the middle of an order could lose track of it. {now_it} "
           f"{to_finish} once that order shows in the "
           "dashboard: python agent.py stop, then python agent.py start"
           + (". " + after if after else ""))
    try:  # restart_self already printed why to the log
        import agent as A
        A.notify(A._load(A.CONFIG_PATH, {}) or {}, msg,
                 **({"priority": "high"} if urgent else {}))
    except Exception:
        pass
    return False


def _as_before_text():
    """How the agent goes on when a restart could not happen: as it was."""
    try:
        import agent as A
        if A._full_hold_note():
            return " and stays on hold (no orders at all), as it was before."
        if A._paused(A._load(A.CONFIG_PATH, {}) or {}):
            return (" and stays paused (no new entries; exits still run), "
                    "as it was before.")
    except Exception:
        pass
    return " and keeps trading as before."


def _restart_after_update(argv):
    """Restart onto the freshly downloaded code. If the restart can't happen
    (see _restart_or_tell) the agent carries on as before — the update put
    it on hold for the restart (hold_orders), which is released here; the
    owner's own Pause, in config.json, was never touched — and the owner is
    told."""
    ok = _restart_or_tell(
        argv, "the update was downloaded",
        "It keeps running the previous version" + _as_before_text(),
        "To finish the update, restart it")
    if not ok:
        release_orders()
    return ok


def _restart_after_import(argv):
    """Restart onto the setup that was just imported from a move file.

    If an order is still being placed and the restart gives up, the agent
    stays on hold — the import's hold has no time limit, and what this
    process has in memory is the setup from before: it must never trade on
    it again, and it is never saved over the imported files
    (setup_replaced) — and the owner is told that nothing is acted on until
    they restart it. If the restart itself fails, the agent leaves
    (_leave_after_import): the supervisor, or the owner, starts it again on
    the imported setup."""
    try:
        return _restart_or_tell(
            argv, "the setup from your other computer was saved on this computer",
            "It is ON HOLD now: it reads no signals and places no orders at "
            "all — it does NOT act on exit signals and the expiry guard does "
            "not sell — so watch your open positions in your broker app. "
            "What it still has in memory is the setup from before the import; "
            "that is not saved over the imported one, so the order that was "
            "still being placed is not in the imported setup's memory: check "
            "your broker account for it.",
            "Restart it",
            after="After the restart, check that the imported setup is there; "
                  "if it is not, import the move file again.",
            urgent=True)
    except Exception as exc:
        _leave_after_import(exc)


def restart_after_self_edit(argv, port=None):
    """Restart onto code the owner just approved (self_edit). If the restart
    can't happen the agent keeps running the code it had, and the owner is
    told the change is not loaded yet."""
    return _restart_or_tell(
        argv, "the code change you approved was written",
        "It keeps running the code it had before the change"
        + _as_before_text(),
        "To load the change, restart it", port=port, quiet=False)


def pull_code_updates(expect=None):
    """Fetch origin and fast-forward to the upstream. Returns (ok, message).
    Fetches itself — never assume check_code_updates ran first (a stale
    origin/<branch> ref would merge cleanly and report success without
    actually updating). --ff-only so local commits/self-edits are never
    merged over silently — a diverged tree is reported, not resolved.
    `expect` (the `target` from check_code_updates) pins the exact commit the
    user reviewed: if the remote moved since, nothing is installed."""
    _forget_update_check()  # whatever happens, the next check asks again
    problem = _install_write_problem()
    if problem:
        return False, problem
    if _snapshot_mode():
        import snapshot_update
        try:
            return snapshot_update.pull(_repo_dir(), expect)
        except Exception as exc:
            return False, _github_unreachable(exc)
    try:
        problem = _remote_problem()
        if problem:
            return False, problem
        err = _fetch_origin()
        if err:
            return False, err
        upstream = _update_upstream()
        head = _git(["rev-parse", upstream]).stdout.strip()
        if expect and head != expect:
            return False, ("the remote changed since you checked — press "
                           "'Check for code updates' again and review the "
                           "new commit list")
        r = _git(["merge", "--ff-only", head or upstream], timeout=120)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()[:200]
            return False, ("cannot fast-forward (local changes or diverged "
                           f"history): {err}")
        return True, f"updated to {upstream}"
    except Exception as exc:
        return False, str(exc)[:200]


def _leave_after_import(exc):
    """The moved setup is on disk but the restart onto it failed. This
    process still has the OLD setup in memory and is on hold (no signals,
    no orders, for good): it must neither carry on like that nor save its
    memory over the imported files. Leaving is the only safe way out — the
    background supervisor starts the agent again on the imported setup;
    without one the owner starts it."""
    import os
    import platform_compat
    try:
        print("The move file was imported, but the agent could not restart "
              f"by itself ({str(exc)[:120]}). It is stopping now so it can't "
              "keep running on the old setup. If it does not come back by "
              "itself within a minute, start it again: python agent.py start",
              flush=True)
    except Exception:
        pass
    os._exit(platform_compat.RESTART_EXIT)


# ── the dashboard (served BY the running agent) ──────────────────────────────

def _host_only(url):
    """A link cut down to its host name ("demo.lnbits.com"), or None: no
    scheme, login, path or query."""
    from urllib.parse import urlparse
    try:
        return (urlparse(str(url or "")).hostname or None)
    except ValueError:
        return None


def _site_only(url):
    """A link cut down to its site: no login, path or query (where feed
    tokens and wallet logins live)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        return f"{u.scheme}://{u.hostname or ''}/…"
    except ValueError:
        return "[link]"


def _llm_safe_state(st):
    """The wizard's state as an AI provider may see it. The page itself
    needs the wallet link (its usr= part IS the wallet's login) and the
    feed address; the AI gets "connected: yes/no" and the site name."""
    import re
    link = re.compile(r"https?://[^\s\"'<>]+")
    out = {k: v for k, v in st.items() if k != "disclaimer"}
    out["wallet"] = {"connected": bool((st.get("wallet") or {}).get("connected"))}
    out["notes"] = {k: link.sub(lambda m: _site_only(m.group(0)), str(v))
                    for k, v in (st.get("notes") or {}).items()}
    return out


# "Ask your agent" in /v2 (POST /api/v2/ask): _ask_llm's own instructions,
# except that /v2 takes no typed commands, so for control the answer points
# at the page that has the control instead of at a chat command.
_V2_ASK_SYSTEM = (
    "You are the status assistant embedded in the user's own agentic-trader "
    "instance. Answer questions using ONLY the status and trade-log data "
    "provided. You cannot place, modify, or size trades, and you cannot "
    "change any setting: this box only answers. For control, tell the user "
    "where it is on their dashboard: Pause / Resume and Practice / LIVE in "
    "the bar at the top; trade size, daily limits, the expiry-day sell time "
    "and which signals to take in Rules & limits; the signal source, signal "
    "days and the payment wallet in Signals & pass; the broker, AI helper, "
    "alerts, start at login, stopping or starting the agent and code changes "
    "in Settings. Never give investment advice; you may explain what the "
    "agent did and why (per its logs). Be concise and plain-spoken.")


def _ask_llm(cfg, question, status_fields, trades, system=None):
    """Free-text Q&A about the agent's own state. Answer-only, never trades.
    Everything the agent adds to the question (status, trade log) is
    scrubbed of keys, tokens, invoices and login links first."""
    import llm_client
    import support
    if not llm_client.configured(cfg):
        return ("Connect an AI model (Setup → Connect your AI) to enable Q&A — "
                "Claude, OpenAI, Grok, Gemini, OpenRouter or a local model.")
    try:
        return llm_client.complete(
            cfg,
            system or (
                "You are the status assistant embedded in the user's own "
                "agentic-trader instance. Answer questions using ONLY the "
                "status and trade-log data provided. You cannot place, "
                "modify, or size trades — for control, tell the user the "
                "exact command (pause, resume, dry on/off, set budget N, "
                "set contracts N, set percent N, set other budget|contracts|"
                "percent N, other sizing same, other trades on|off, set positions N, "
                "set cap N, stop, autostart on|off, open at login on|off, keep awake on|off, preview on|off, rules, tickers SPY QQQ, calls only|puts only|calls and puts, min strike N, no 0dte on|off, loss stop N). Never give investment advice; you may "
                "explain what the agent did and why (per its logs). Be "
                "concise and plain-spoken."),
            support.scrub(
                f"STATUS:\n{json.dumps(status_fields, indent=1)}\n\n"
                f"RECENT TRADE LOG:\n{json.dumps(trades, indent=1)}\n\n")
            + f"QUESTION: {question}",
            max_tokens=1024, effort="low")
    except llm_client.LLMError as exc:
        if exc.reason == "refused":
            return "I can't help with that one."
        return f"Q&A failed: {str(exc)[:150]}"
    except Exception as exc:
        return f"Q&A failed: {str(exc)[:150]}"


def start_dashboard(get_status, get_trades, apply_command, cfg_getter,
                    get_view=None, get_activity=None):
    """Serve the combined app (dashboard + settings) from the running agent."""
    return start_app(get_status, get_trades, apply_command, cfg_getter,
                     get_view=get_view, get_activity=get_activity)
