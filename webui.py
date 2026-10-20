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
        candidates = [
            ["open", "-na", "Google Chrome", "--args", f"--app={url}"],
            ["open", "-na", "Microsoft Edge", "--args", f"--app={url}"],
            ["open", "-na", "Brave Browser", "--args", f"--app={url}"],
        ]
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
CONTROLS = {"paused": False, "stop": False}

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
      · balance: <span id="fund-bal" class="num">0</span> sats
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
    automatically when I log in to this computer</label></p>
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

<script>
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
  const r=await updCall('/api/update/check',null,150000);
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
let passTimer=null,passHelpTimer=null;
async function buyPass(days){
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
  if(passTimer)clearInterval(passTimer);
  passTimer=setInterval(checkPass,3000);
}
async function checkPass(){
  const r=await api('/api/pass/status');
  const st=document.getElementById('pass-status');
  if(r.status==='active'){
    if(passTimer){clearInterval(passTimer);passTimer=null;}
    document.getElementById('pass-box').style.display='none';
    {const n=Number(r.days_left);
    st.innerHTML='<span class="ok">✓ Access active — '+(Number.isFinite(n)&&r.days_left!=null?n+' signal day'+(n===1?'':'s'):'? signal days')+
      ' left</span>';}
  }else if(r.status==='pending'){st.textContent='waiting for payment…';}
  else if(r.status==='unpaid'){
    if(passTimer){clearInterval(passTimer);passTimer=null;}
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
// means every size is paused. Re-checked every few minutes so a size comes
// back on its own once the node has refilled.
async function passSizes(){
  try{const r=await api('/api/pass/options');
    if(!Array.isArray(r.options))return;
    document.querySelectorAll('.pass[data-days]').forEach(b=>{
      b.style.display=r.options.indexOf(+b.dataset.days)>=0?'':'none';});
    const n=document.getElementById('pass-sizes-note');
    if(n)n.textContent=r.options.length?'':'Passes are paused for a little while — '+
      'our node is refilling so it can receive the payment. This page checks again every few minutes.';
  }catch(e){}
}
async function boot(){
  passSizes();setInterval(passSizes,180000);
  const st=await api('/api/state');
  document.getElementById('disclaimer').innerHTML=mdLite(st.disclaimer);
  const notes=st.notes||{};
  if(st.consent){done('s-consent');restoredNote('s-consent',notes.consent||'Agreement signed — on file');}
  if(st.llm){done('s-llm');restoredNote('s-llm',notes.llm||'AI connected');
    // Key on file: leave the box blank and press Find models to switch model.
    document.getElementById('llm-key').placeholder='saved key on file — leave blank and press Find models to change model';}
  if(st.source){done('s-source');restoredNote('s-source',notes.source||('Source saved: '+st.source));
    const sel=document.getElementById('src');sel.value=st.source;srcChanged();}
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
    :'<span class="err">'+r.error+'</span>';
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
async function makeWallet(){
  document.getElementById('mw-msg').textContent=' creating…';
  const r=await api('/api/wallet/create',{});
  if(r.ok){showWallet(r.page,'created ✓');
    if(r.warning)document.getElementById('mw-msg').innerHTML+=
      '<div class="err" style="margin-top:.5rem">⚠️ '+esc(r.warning)+'</div>';}
  else document.getElementById('mw-msg').innerHTML=' <span class="err">'+r.error+'</span>';
}
async function fundInvoice(){
  const sats=document.getElementById('fund-sats').value;
  const r=await api('/api/wallet/fund',{sats:sats});
  if(!r.ok){document.getElementById('mw-msg').innerHTML=' <span class="err">'+r.error+'</span>';return;}
  const pre=document.getElementById('bolt11');pre.style.display='';pre.textContent=r.bolt11;
  renderQr(r.qr);
  const fn=document.getElementById('fund-note');
  if(r.note){fn.textContent=r.note;fn.style.display='';}
  else fn.style.display='none';
  document.getElementById('copy-msg').textContent='';
  document.getElementById('fund-open').style.display='';
  const fl=document.getElementById('fund-link');
  fl.href='lightning:'+r.bolt11;fl.style.display='';
  if(balTimer)clearInterval(balTimer);
  balTimer=setInterval(async()=>{const b=await api('/api/wallet/balance');
    if(b.ok)document.getElementById('fund-bal').textContent=b.sats.toLocaleString();},4000);
}
async function doSource(){
  const v=document.getElementById('src').value;
  const r=await api('/api/source',{source:v,
    scope:document.getElementById('hc-scope').value,
    lnbits_url:document.getElementById('ln-url').value,
    lnbits_key:document.getElementById('ln-key').value,
    agenthc_key:document.getElementById('hc-key').value,
    source_url:document.getElementById('src-url-input').value});
  document.getElementById('src-msg').innerHTML=r.ok?'<span class="ok">'+r.note+'</span>'
    :'<span class="err">'+r.error+'</span>';
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
  document.getElementById('brk-msg').innerHTML=r.ok?' <span class="ok">'+r.note+'</span>'
    :' <span class="err">'+r.error+'</span>';
  if(r.ok){document.getElementById('brk-status').innerHTML='<span class="ok">Connected: '+
    esc(b.name||id)+' ✓</span>';done('s-broker');}
}
async function rhStart(){
  const r=await api('/api/rh/start');
  if(r.url){document.getElementById('rh-msg').textContent='waiting for approval…';
    window.open(r.url,'_blank');poll();}
  else document.getElementById('rh-msg').innerHTML='<span class="err">'+r.error+'</span>';
}
async function poll(){
  const st=await api('/api/state');
  if(st.broker){
    document.getElementById('rh-msg').textContent='';
    document.getElementById('brk-status').innerHTML='<span class="ok">Connected: Robinhood account ····'+
      esc(st.broker)+' ✓</span>';
    if(st.rh_warning)document.getElementById('rh-accounts').innerHTML=
      '<p class="err">'+st.rh_warning+'</p>';
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
    :'<span class="err">'+r.error+'</span>';
  if(r.ok)done('s-sizing');
}
async function doSafety(){
  const r=await api('/api/safety',{dry:document.getElementById('sf-dry').checked,
    cap:document.getElementById('sf-cap').value,
    max_loss:document.getElementById('sf-maxloss').value,
    discord:document.getElementById('nt-discord').value,
    ntfy:document.getElementById('nt-ntfy').value,
    tg_token:document.getElementById('nt-tg-token').value,
    tg_chat:document.getElementById('nt-tg-chat').value,
    policy:document.getElementById('pol-text').value,
    anthropic_key:document.getElementById('pol-key').value});
  if(r.ok){done('s-safety');
    document.getElementById('done-body').innerHTML=
     '<p class="ok">Setup complete — pick where your agent should live.</p>'+
     '<details><summary class="muted">…or start it from a terminal instead</summary>'+
     '<pre>'+esc(r.next)+'</pre></details>';}
  else document.getElementById('sf-msg').innerHTML='<span class="err">'+r.error+'</span>';
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
  if(!r.ok){el.innerHTML='<span class="err">'+r.error+'</span>';return;}
  const poll2=async()=>{
    const s=await api('/api/deploy/status');
    if(s.ip){el.innerHTML='<span class="ok">Your agent is live on its own server at '
      +s.ip+' ✓</span><br><span class="muted">It started automatically and will send '
      +'your startup notification shortly. This computer no longer needs to run '
      +'anything — you can close this window. Dashboard from here: '
      +'ssh -L 8721:127.0.0.1:8721 trader@'+s.ip
      +' then open http://127.0.0.1:8721/dash</span>';
      api('/api/finish',{});}
    else{el.textContent='server status: '+s.status+' — waiting…';setTimeout(poll2,5000);}
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
 .feed li{display:grid;grid-template-columns:62px 22px minmax(0,1fr) auto;gap:var(--s3);align-items:start;
   padding:10px var(--s4);border-bottom:1px solid var(--line)}
 .feed li:last-child{border-bottom:0}
 .feed li:hover{background:var(--surface-2)}
 .feed li.empty{display:block;color:var(--muted);text-align:center;padding:24px var(--s4)}
 .feed li.empty:hover{background:none}
 .feed .when{font:12px/20px var(--mono);color:var(--muted);white-space:nowrap}
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
<script>
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
  policy_veto:'your rules said no'};
const ICON=n=>'<svg class="i"><use href="#i-'+n+'"/></svg>';
function cap1(s){s=String(s||'');return s.charAt(0).toUpperCase()+s.slice(1);}
function shortDate(d){return d.toLocaleDateString([], {month:'short',day:'numeric'});}
function hhmm(d){return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false});}
function when(ts){
  // 24h HH:MM for today, "Oct 14" for older rows (full time in the title)
  const d=new Date(ts);if(isNaN(d))return esc(ts||'');
  const today=new Date().toDateString()===d.toDateString();
  return '<span class="when" title="'+esc(d.toLocaleString())+'">'+esc(today?hhmm(d):shortDate(d))+'</span>';
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
function setMode(kind,live,paused){
  // kind: 'ok' | 'off' | 'unreachable'
  // #modebar is a live region polled every 5s: only touch the DOM when the
  // text actually changes, or screen readers re-announce it on every poll.
  const mb=$('modebar'),main=$('mb-main'),sub=mb.querySelector('.mb-txt>.muted');
  const put=(cls,h)=>{if(mb.className!==cls)mb.className=cls;if(main.innerHTML!==h)main.innerHTML=h;};
  if(kind==='off'){put('modebar off','<b>Agent not running.</b> Start it: <code>python agent.py run</code>');return;}
  if(kind==='unreachable'){put('modebar off','<b>Can\\'t reach the agent on this computer</b> — is it still running?');return;}
  put('modebar '+(live?'live':'dry')+(paused?' paused':''),
    (live?'<b>LIVE.</b> Real orders with real money are sent to your broker.':
    '<b>Dry-run.</b> The agent logs every decision but places no orders.')+
    (paused?' <b class="pz">Paused</b> — no new entries until you press Resume.':''));
  const st=live?'Switch to dry-run any time to stop sending orders — your decision, not advice.':
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
    bm.innerHTML=live?'<span class="dot"></span>LIVE · real orders':'<span class="dot"></span>Dry-run · no orders';
    bm.className='pill '+(live?'live':'dry');
    const mk=$('b-market'),open=(f['market']||'').startsWith('open');
    mk.innerHTML=open?'<span class="dot"></span>Market open':'Market closed';
    mk.className='pill'+(open?' open':'');mk.style.display='';
    $('b-paused').style.display=s.paused?'':'none';
    setMode('ok',live,!!s.paused);
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
      return '<li>'+when(x.ts)+'<span class="ic '+ic+'">'+ICON(icon)+'</span><div><div class="t">'+title+'</div>'+
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
async function quick(cmd){
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
refresh();setInterval(refresh,5000);
</script></body></html>"""


# ── shared handler plumbing ──────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    routes_get = {}
    routes_post = {}
    pages = {}
    landing = "/setup"

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
        self.send_response(302)
        self.send_header("Set-Cookie", f"{_SESSION_COOKIE}={k}; Path=/; HttpOnly; "
                         "SameSite=Lax; Max-Age=31536000")
        self.send_header("Location", u.path or "/")
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
            self.send_header("Location", self.landing)
            self.end_headers()
            return
        if path in self.pages:
            app = None if "ui=classic" in self.path else _app_index()
            if app is None:
                return self._send(None, html=self.pages[path])
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
            return self._send(fn(self))
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
                "step 2 (Connect your AI), then save this step again.")
    return ("A trading policy needs a connected AI to check it. Without one, "
            "every entry would be skipped and the agent would never trade. "
            "Connect your AI in step 2, paste a Claude key below, or clear "
            "the policy box.")


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
              cfg_getter=None, wait_finish=False):
    """One server, one port: /setup (wizard) + /dash (dashboard) + all APIs."""
    import agent as A
    from brokers.robinhood import RobinhoodMCP, _token_path, content_json
    from lightning_wallet import LNbitsWallet, WalletError

    cfg = A._load(A.CONFIG_PATH, {}) or {}
    pending = {"oauth": None, "broker_last4": "", "rh_warning": "",
               "finished": threading.Event()}

    def save():
        A._save(A.CONFIG_PATH, cfg, private=True)

    class W(_Handler):
        pages = {"/setup": _WIZARD_HTML, "/dash": _DASH_HTML}
        landing = "/dash" if get_status else "/setup"
        routes_get, routes_post = {}, {}

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
                cfg["lnbits_url"] = url
                cfg["lnbits_admin_key"] = str(data["lnbits_key"]).strip()
                cfg.setdefault("max_autopay_sats", 30_000)
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
        save()
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

    def deploy(_h, data):
        import provision
        import llm_policy
        if llm_policy.entries_blocked(cfg):
            return {"ok": False, "error": llm_policy.BLOCKED_MSG}
        token = str(data.get("token", "")).strip()
        if not token:
            return {"ok": False, "error": "paste a DigitalOcean API token"}
        try:
            droplet_id, msg = provision.create_droplet(
                token, str(data.get("region", "nyc3")))
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)[:250]}
        pending["deploy"] = (token, droplet_id)
        return {"ok": True, "note": msg}

    def deploy_status(_h):
        import provision
        if not pending.get("deploy"):
            return {"status": "no deploy in progress", "ip": ""}
        token, droplet_id = pending["deploy"]
        return provision.droplet_status(token, droplet_id)

    def brokers_list(_h):
        from brokers import key_brokers
        return {"brokers": key_brokers()}

    def broker_connect(_h, data):
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
        save()
        return {"ok": True, "note": msg}

    def update_check(_h):
        return check_code_updates()

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
        # seen-events, trade log, daily cap) already lives on disk. CONTROLS
        # resets on restart, so polling resumes automatically.
        CONTROLS["paused"] = True

        def _restart():
            _time.sleep(3.0)  # response flush + in-flight event grace
            os.environ["AGENT_UPDATE_RESTARTED"] = "1"  # skip browser popup
            import platform_compat
            platform_compat.restart_self(list(sys.argv), APP_PORT)
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
                "diff": "\n".join(c["diff"] for c in p.get("changes", []))[:200000]}

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
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from sources import agenthc_day_trade_ideas as hc
        try:
            days = int(data.get("days") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "pick 1, 5, 20 or 60 days"}
        current = (cfg.get("feed_pass") or {}).get("token")
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
        import time as _t
        cfg["source"] = "agenthc"
        cfg["feed_pass_pending"] = {
            "token": body["pass_token"], "days": days, "bought_at": _t.time(),
            "access_until": body.get("access_until")}
        save()
        bolt11 = body["payment_request"]
        return {"ok": True, "bolt11": bolt11, "qr": _qr_matrix(bolt11.upper()),
                "amount_sats": body.get("amount_sats"),
                "amount_usd": body.get("amount_usd_approx"),
                "extends": bool(body.get("extends_current_pass")),
                "carried": body.get("carried_over_days") or 0,
                "access_until": body.get("access_until")}

    def pass_options_route(_h):
        from sources import agenthc_day_trade_ideas as hc
        return {"options": hc.pass_options()}

    def pass_status_route(_h):
        """Pending purchase first (promote it once paid), else the current pass."""
        from sources import agenthc_day_trade_ideas as hc
        import time as _t
        pend = cfg.get("feed_pass_pending")
        try:
            if pend:
                st = hc.pass_status(pend["token"])
                if st.get("status") in ("active", "exits_only"):
                    cfg["feed_pass"] = {**pend, "access_until": st.get("access_until")}
                    cfg.pop("feed_pass_pending", None)
                    save()
                    return {"ok": True, "status": "active",
                            "days_left": st.get("days_left", st.get("trading_days_left")),
                            "access_until": st.get("access_until")}
                if st.get("status") == "pending":
                    if _t.time() - float(pend.get("bought_at") or 0) < 3600 + 300:
                        return {"ok": True, "status": "pending"}
                cfg.pop("feed_pass_pending", None)  # expired unpaid / invalid
                save()
                if not cfg.get("feed_pass"):
                    return {"ok": True, "status": "unpaid"}
            cur = cfg.get("feed_pass")
            if not cur:
                return {"ok": True, "status": "none"}
            st = hc.pass_status(cur["token"])
            return {"ok": True, "status": st.get("status"),
                    "days_left": st.get("days_left", st.get("trading_days_left")),
                    "access_until": st.get("access_until")}
        except Exception as exc:
            return {"ok": False, "status": "error", "error": _net_error_text(exc, cache_s=60)}

    def wallet_create(_h, _data):
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from lightning_wallet import create_wallet
        try:
            url, key, page = create_wallet()
        except WalletError as exc:
            return {"ok": False, "error": str(exc)[:200]}
        cfg["source"] = "agenthc"
        cfg["lnbits_url"] = url
        cfg["lnbits_admin_key"] = key
        if page:
            cfg["lnbits_wallet_page"] = page
        cfg.setdefault("max_autopay_sats", 30_000)
        save()
        from lightning_wallet import demo_warning
        return {"ok": True, "page": page, "warning": demo_warning(url)}

    def wallet_fund(_h, data):
        w = _wallet()
        if w is None:
            return {"ok": False, "error": "no wallet yet"}
        try:
            sats = max(1000, int(data.get("sats") or 50000))
        except (TypeError, ValueError):
            return {"ok": False, "error": "enter a sats amount"}
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
            save()
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
        cfg["day_pass_recurring"] = bool(data.get("on"))
        save()
        return {"ok": True, "on": cfg["day_pass_recurring"]}

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
        for acct in ((payload.get("data") or {}).get("accounts") or []):
            if acct.get("agentic_allowed") and acct.get("state") == "active":
                cfg["robinhood_account"] = str(acct["account_number"])
                cfg["broker"] = "robinhood"
                pending["broker_last4"] = cfg["robinhood_account"][-4:]
                if not acct.get("option_level") or acct.get("option_level") == "option_level_0":
                    warning = ("Options are NOT enabled on your Agentic account — "
                               "orders will be rejected until you apply: "
                               "https://applink.robinhood.com/upgrade_options"
                               f"?account_number={cfg['robinhood_account']}")
        pending["rh_warning"] = warning
        save()
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
        cfg.clear()
        cfg.update(new)
        save()
        return {"ok": True}

    def safety(_h, data):
        perr = _policy_save_error(cfg, data.get("policy", ""),
                                  data.get("anthropic_key", ""))
        if perr:
            return {"ok": False, "error": perr}
        cfg["dry_run"] = bool(data.get("dry", True))
        try:
            cfg["max_entries_per_day"] = max(1, int(data.get("cap") or 5))
        except (TypeError, ValueError):
            cfg["max_entries_per_day"] = 5
        raw_loss = str(data.get("max_loss") or "").strip()
        if raw_loss:  # blank keeps any existing limit (re-running the wizard)
            try:
                cfg["max_daily_loss_usd"] = max(
                    0.0, float(raw_loss.lstrip("$").replace(",", "")))
            except ValueError:
                pass
        cfg.setdefault("max_event_age_s", 300)
        for src_key, cfg_key in (("discord", "discord_webhook_url"),
                                 ("ntfy", "ntfy_topic"),
                                 ("tg_token", "telegram_bot_token"),
                                 ("tg_chat", "telegram_chat_id")):
            v = str(data.get(src_key, "")).strip()
            if v:
                cfg[cfg_key] = v
        policy = str(data.get("policy", "")).strip()
        if policy:
            import llm_policy
            path = llm_policy.policy_path()
            import os as _os
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(policy + "\n")
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

    def policy_disable(_h, _data):
        import llm_policy
        moved = llm_policy.disable_policy()
        return {"ok": True, "note": ("policy turned off (saved as policy.md.disabled)"
                                     if moved else "no policy was set")}

    def finish(_h, _data):
        pending["finished"].set()
        return {"ok": True}

    def start_local(_h, data):
        """Option A as one click: start the agent in the background (under
        keep_running's supervisor, so closing the terminal doesn't stop it
        and a crash restarts it), optionally at every login too, then hand
        it this port. The supervisor waits until the wizard's socket is
        free before running `agent.py run`, so the dashboard (same port)
        can't lose the bind race and fall back to headless."""
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from brokers import broker_ready
        if not (cfg.get("source") and broker_ready(cfg)):
            return {"ok": False,
                    "error": "finish the source and broker steps first"}
        import llm_policy
        if llm_policy.entries_blocked(cfg):
            return {"ok": False, "error": llm_policy.BLOCKED_MSG}
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

    def d_trades(_h):
        return {"trades": get_trades() if get_trades else []}

    def d_command(_h, data):
        text = str(data.get("text", "")).strip()
        if apply_command is None:
            return {"reply": "The agent isn't running yet — finish setup, then "
                             "start it with: agent.py run"}
        handled, reply = apply_command(text)
        if handled:
            return {"reply": reply}
        s = d_status(None)
        return {"reply": _ask_llm((cfg_getter() if cfg_getter else cfg), text,
                                  s.get("fields", {}),
                                  get_trades() if get_trades else [])}

    W.routes_get = {"/api/state": state, "/api/rh/start": rh_start,
                    "/api/wallet/balance": wallet_balance,
                    "/api/deploy/status": deploy_status,
                    "/api/brokers": brokers_list,
                    "/api/update/check": update_check,
                    "/api/help/netcheck": help_netcheck,
                    "/api/help/report": help_report,
                    "/api/wallet/quote": wallet_quote,
                    "/api/llm/providers": llm_providers,
                    "/api/pass/status": pass_status_route,
                    "/api/pass/options": pass_options_route,
                    "/api/status": d_status, "/api/trades": d_trades,
                    "/api/proposal": proposal_get,
                    "/api/autostart": autostart_get}
    def ask(_h, data):
        q = str(data.get("question", "")).strip()
        wiz_state = {k: v for k, v in state(None).items() if k != "disclaimer"}
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
                     "/api/sizing": sizing, "/api/safety": safety,
                     "/api/finish": finish, "/api/ask": ask,
                     "/api/policy/disable": policy_disable,
                     "/api/start-local": start_local,
                     "/api/autostart": autostart_set,
                     "/api/wallet/create": wallet_create,
                     "/api/pass/buy": pass_buy,
                     "/api/wallet/fund": wallet_fund,
                     "/api/llm": llm, "/api/llm/models": llm_models,
                     "/api/deploy": deploy,
                     "/api/update/pull": update_pull,
                     "/api/wallet/recurring": wallet_recurring,
                     "/api/broker/connect": broker_connect,
                     "/api/proposal/apply": proposal_apply,
                     "/api/proposal/reject": proposal_reject,
                     "/api/command": d_command}

    class _Redirect(Exception):
        pass

    # patch GET to turn /callback success into a browser redirect to /
    orig_get = W.do_GET

    def do_GET(self):
        if self.path.startswith("/callback"):
            if not self._guard(check_origin=False):
                return
            try:
                rh_callback(self)
            except _Redirect:
                pass
            except Exception as exc:
                return self._send(None, html=f"<h3>OAuth failed: {exc}</h3>"
                                             f"<p><a href='/'>back to setup</a></p>")
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        return orig_get(self)

    W.do_GET = do_GET

    server = _AppServer(("127.0.0.1", APP_PORT), W)
    url = f"http://127.0.0.1:{APP_PORT}/"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
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


def check_code_updates():
    """Fetch origin and report how far behind HEAD is. Never raises.
    Returns the exact commit (`target`) the user is shown, so the pull can
    install THAT commit and nothing pushed after they looked."""
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


def pull_code_updates(expect=None):
    """Fetch origin and fast-forward to the upstream. Returns (ok, message).
    Fetches itself — never assume check_code_updates ran first (a stale
    origin/<branch> ref would merge cleanly and report success without
    actually updating). --ff-only so local commits/self-edits are never
    merged over silently — a diverged tree is reported, not resolved.
    `expect` (the `target` from check_code_updates) pins the exact commit the
    user reviewed: if the remote moved since, nothing is installed."""
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


# ── the dashboard (served BY the running agent) ──────────────────────────────

def _ask_llm(cfg, question, status_fields, trades, system=None):
    """Free-text Q&A about the agent's own state. Answer-only, never trades."""
    import llm_client
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
                "percent N, other sizing same, set positions N, "
                "set cap N, stop, autostart on|off). Never give investment advice; you may "
                "explain what the agent did and why (per its logs). Be "
                "concise and plain-spoken."),
            f"STATUS:\n{json.dumps(status_fields, indent=1)}\n\n"
            f"RECENT TRADE LOG:\n{json.dumps(trades, indent=1)}\n\n"
            f"QUESTION: {question}",
            max_tokens=1024, effort="low")
    except llm_client.LLMError as exc:
        if exc.reason == "refused":
            return "I can't help with that one."
        return f"Q&A failed: {str(exc)[:150]}"
    except Exception as exc:
        return f"Q&A failed: {str(exc)[:150]}"


def start_dashboard(get_status, get_trades, apply_command, cfg_getter):
    """Serve the combined app (dashboard + settings) from the running agent."""
    return start_app(get_status, get_trades, apply_command, cfg_getter)
