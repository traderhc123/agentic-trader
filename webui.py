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

Stdlib only (http.server + threading) — no new dependencies.
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
_LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>Locked</title>
<body style="font-family:system-ui;background:#0b0e14;color:#e8ebf2;padding:2rem">
<h2>This dashboard is locked</h2>
<p>Open the login link printed in the terminal where the agent is running,
or print it again with:</p><pre>python agent.py link</pre></body>"""


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

# Run-loop controls the dashboard can flip; agent.py polls these.
CONTROLS = {"paused": False, "stop": False}

_STYLE = """
<style>
 :root{
   --bg:#0b0e14; --panel:#141925; --panel-2:#161b28; --sink:#0a0c11;
   --fg:#e8ebf2; --muted:#9aa4b6; --line:#232a38; --line-2:#2c3446;
   --accent:#4c8dff; --accent-2:#82b0ff; --ok:#4ade80; --err:#f87171;
   --shadow:0 1px 2px rgba(0,0,0,.45),0 10px 34px rgba(0,0,0,.30);
 }
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,system-ui,sans-serif;
      max-width:760px;margin:2rem auto;padding:0 1.15rem;background:var(--bg);color:var(--fg);
      line-height:1.55;-webkit-font-smoothing:antialiased;letter-spacing:.1px}
 h1{font-size:1.5rem;font-weight:650;letter-spacing:-.02em}
 h2{font-size:1.06rem;font-weight:600;margin:1.6rem 0 .5rem;letter-spacing:-.01em}
 a{color:var(--accent-2);text-decoration:none} a:hover{text-decoration:underline}
 section{border:1px solid var(--line);border-radius:12px;padding:1.15rem 1.2rem;margin:1rem 0;
         background:linear-gradient(180deg,var(--panel),var(--panel-2));box-shadow:var(--shadow)}
 section.done{border-color:#2f6f43} section.locked{opacity:.45;pointer-events:none}
 label{display:block;font-size:.9rem;margin-top:.15rem}
 input[type=text],input[type=password],input[type=number],textarea,select{
   width:100%;padding:.6rem .7rem;margin:.3rem 0 .7rem;background:var(--sink);color:var(--fg);
   border:1px solid var(--line-2);border-radius:8px;font-size:.95rem;
   transition:border-color .15s,box-shadow .15s}
 input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);
   box-shadow:0 0 0 3px rgba(76,141,255,.18)}
 button{padding:.55rem 1.1rem;border:0;border-radius:8px;color:#fff;cursor:pointer;
        font-size:.95rem;font-weight:550;background:linear-gradient(180deg,var(--accent),#3a7bef);
        box-shadow:0 1px 2px rgba(0,0,0,.35);transition:transform .08s,filter .15s}
 button:hover{filter:brightness(1.08)} button:active{transform:translateY(1px)}
 button:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(76,141,255,.38)}
 button:disabled{background:#2a303c;color:#6b7280;cursor:not-allowed;box-shadow:none;filter:none}
 .muted{color:var(--muted);font-size:.86rem} .ok{color:var(--ok)} .err{color:var(--err);white-space:pre-line}
 pre{background:var(--sink);padding:.75rem;border-radius:8px;overflow-x:auto;border:1px solid var(--line);
     white-space:pre-wrap;word-break:break-word;font-size:.8rem}
 .disc h3,.disc h4,.disc h5,.disc h6{margin:.9rem 0 .35rem;font-size:.92rem;color:var(--fg)}
  .disc h3{font-size:1rem} .disc p{margin:.45rem 0} .disc ul{margin:.3rem 0 .5rem 1.1rem;padding:0}
  .disc blockquote{margin:.5rem 0;padding:.4rem .7rem;border-left:3px solid var(--accent);color:var(--muted)}
  .disc code{font-size:.78rem;background:var(--line);padding:.05rem .3rem;border-radius:4px}
 .disc{max-height:280px;overflow-y:auto;border:1px solid var(--line-2);padding:.85rem;
       border-radius:8px;font-size:.82rem;background:var(--sink)}
 .badge{font-size:.72rem;padding:.12rem .55rem;border-radius:99px;
        background:var(--line-2);margin-left:.5rem}
 .badge.on{background:#14532d;color:var(--ok)}
 #chatlog{height:300px;overflow-y:auto;background:var(--sink);border-radius:8px;
          padding:.75rem;font-size:.85rem;border:1px solid var(--line)}
 .me{color:var(--accent-2)} .agent{color:var(--fg);margin-bottom:.6rem;white-space:pre-wrap}
 table{width:100%;border-collapse:collapse;font-size:.82rem}
 td,th{padding:.35rem .55rem;border-bottom:1px solid var(--line);text-align:left}
 @media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style>"""

_WIZARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentic-trader setup</title>""" + _STYLE + """
<style>
 html{background:
   radial-gradient(1100px 720px at 82% -10%,rgba(76,141,255,.12),transparent 58%),
   radial-gradient(900px 620px at 4% 2%,rgba(76,141,255,.07),transparent 55%),
   var(--bg);background-repeat:no-repeat;background-attachment:fixed;min-height:100%}
 body{max-width:880px;background:transparent}
 #bg{position:fixed;inset:0;width:100%;height:100%;z-index:-1;display:block}
 .hero{display:flex;align-items:center;gap:.85rem;margin:.5rem 0 .1rem}
 .hero>div.txt{min-width:0}
 .hero .tag{font-size:.7rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
 .hero h1{margin:.05rem 0 0}
 .logo{width:42px;height:42px;flex:none;border-radius:12px;position:relative;overflow:hidden;
   background:radial-gradient(125% 125% at 30% 18%,#6ea0ff,#2f6bff 52%,#1c3d97);
   box-shadow:0 6px 20px rgba(58,110,255,.45),inset 0 1px 0 rgba(255,255,255,.4)}
 .logo::after{content:"";position:absolute;inset:0;
   background:conic-gradient(from 200deg,transparent,rgba(255,255,255,.4),transparent 42%)}
 .grow{flex:1}
 .lead{color:var(--muted);font-size:.9rem;margin:.55rem 0 .1rem;max-width:64ch}
 .stepbar{display:flex;gap:.4rem;flex-wrap:wrap;margin:1.25rem 0 1.35rem}
 .chip{font-size:.75rem;padding:.32rem .78rem;border-radius:99px;border:1px solid var(--line-2);
   color:var(--muted);cursor:default;user-select:none;background:rgba(20,25,37,.55);transition:.15s}
 .chip.done{border-color:#2f7d46;color:var(--ok);cursor:pointer}
 .chip.done:hover{background:rgba(47,125,70,.14)}
 .chip.cur{border-color:var(--accent);color:#bcd4ff;background:rgba(37,60,120,.4);
   box-shadow:0 0 0 3px rgba(76,141,255,.12)}
 section{display:none;background:linear-gradient(180deg,rgba(20,25,37,.82),rgba(22,27,40,.88));
   backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px)}
 section.active{display:block;animation:rise .28s ease both}
 @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
 .navrow{display:flex;gap:.6rem;margin-top:.9rem;align-items:center;flex-wrap:wrap}
 .navrow .back{background:linear-gradient(180deg,#28303f,#222a38);border:1px solid var(--line-2)}
 #help{margin-top:1.5rem;border-top:1px solid var(--line);padding-top:.9rem}
 #chatlog{height:170px}
 .chatrow{display:flex;gap:.4rem}.chatrow input{margin:0;flex:1}
</style></head><body>
<canvas id="bg" aria-hidden="true"></canvas>
<div class="hero">
 <div class="logo" aria-hidden="true"></div>
 <div class="txt"><div class="tag">Agentic Trader</div>
  <h1>Set up your trading agent</h1></div>
 <span class="grow"></span>
 <a class="chip done" href="/dash" style="align-self:center">Dashboard →</a>
</div>
<p class="lead">Everything runs on your own machine — this wizard is served from
127.0.0.1 by your agent itself. It moves real money; read each step carefully.</p>
<div class="navrow" style="margin:0 0 .9rem">
 <button onclick="checkUpdates()" id="upd-btn" style="background:var(--sink);border:1px solid var(--line-2);color:var(--muted);font-size:.82rem;padding:.4rem .8rem">⟳ Check for updates</button>
 <button onclick="pullUpdates()" id="upd-pull" style="display:none">Pull latest code</button>
 <span id="upd-msg" class="muted"></span>
</div>
<div class="stepbar" id="stepbar"></div>

<section id="s-consent"><h2>Agreement</h2>
 <div class="disc" id="disclaimer">loading…</div>
 <p><label><input type="checkbox" id="c-read"> I have read the entire agreement
 above and I accept all of its terms, including that <b>all liability is mine</b>
 and that no party here is a registered investment adviser.</label></p>
 <p>Type exactly <b>I AGREE AND ACCEPT ALL LIABILITY</b> to accept:</p>
 <input type="text" id="c-phrase" autocomplete="off">
 <div class="navrow"><button onclick="doConsent()">Accept & continue</button>
 <span id="c-msg"></span></div>
</section>

<section id="s-llm"><h2>Connect your AI (recommended)</h2>
 <p class="muted">Any model works — Claude, ChatGPT/OpenAI, Grok, Gemini,
 OpenRouter (hundreds of models with one key), DeepSeek, Mistral, Groq, or a
 model running on this computer. It powers the help chat, your plain-English
 trading policy and the dashboard Q&amp;A. It never places trades. Skippable.</p>
 <label>1. Paste an API key</label>
 <input type="password" id="llm-key" placeholder="sk-ant-…, sk-…, xai-…, AIza…, sk-or-…"
  autocomplete="off" oninput="llmKeyTyped()" onkeydown="if(event.key==='Enter')llmFind()">
 <p class="muted" style="margin:.3rem 0">The provider is detected from the key.
 Need one? <a id="llm-keys-link" href="https://console.anthropic.com/settings/keys"
 target="_blank" rel="noopener">Get a key ↗</a> · <b>Claude Max or Team plan?</b>
 It includes monthly API credits — link them to a Claude Console organization
 (claude.ai → your plan's API credits), then create a key there. Signing in
 with a chat account (claude.ai, chatgpt.com) doesn't connect here; the API
 key does. Keys stay on this computer.</p>
 <details id="llm-adv"><summary class="muted">Provider / local model / custom endpoint</summary>
  <select id="llm-provider" onchange="llmProviderChanged()"></select>
  <input type="text" id="llm-base" placeholder="Endpoint URL (custom / local only)">
  <p class="muted">Local models (Ollama, LM Studio) need no key — pick
  "Local model", check the URL, then press Find models.</p>
 </details>
 <div class="navrow"><button onclick="llmFind()">Find models</button>
 <span id="llm-find-msg"></span></div>
 <div id="llm-pick" style="display:none">
  <label>2. Pick a model</label>
  <input type="text" id="llm-model" list="llm-models" autocomplete="off"
   placeholder="start typing to filter">
  <datalist id="llm-models"></datalist>
  <p class="muted">Bigger models follow your trading policy more reliably;
  any error or unclear answer from the model means the trade is skipped.</p>
 </div>
 <div class="navrow"><button id="llm-connect" onclick="doLLM()" style="display:none">Connect &amp; continue</button>
 <button class="back" onclick="skipLLM()">Skip for now</button>
 <span id="llm-msg"></span></div>
</section>

<section id="s-source"><h2>Signal source</h2>
 <select id="src" onchange="srcChanged()">
  <option value="agenthc">AgentHC Agentic Day Trade Ideas (journal feed, sats-priced)</option>
  <option value="manual">My own commands (commands.jsonl)</option>
  <option value="url">A JSON feed URL I provide</option>
 </select>
 <div id="src-agenthc">
  <label>Which trade signals?</label>
  <select id="hc-scope">
   <option value="main">Main pick only — one high-conviction trade a day (default)</option>
   <option value="all">Main pick + "other trades" — wider journal, several entries a day</option>
  </select>
  <p class="muted">"Other trades" follows AgentHC's broader shadow journal; your
  daily entry cap and per-trade budget still apply to every signal.</p>
  <label>Access — prepay signal days</label>
  <p class="muted">One Lightning payment from the wallet you already use (Cash
  App, Strike, Phoenix, Alby…) — ~$10 per signal day, priced in sats. A day
  is only used when your agent actually receives a trade signal (an entry or
  an exit) that day; quiet days are free. "Main pick only" uses a day only
  for main-pick signals; adding "other trades" (above) uses days on those
  too. Unused days carry over when you buy more. The agent never holds
  money.</p>
  <div class="navrow">
   <button onclick="buyPass(1)">1 signal day (try it)</button>
   <button onclick="buyPass(5)">5 signal days</button>
   <button onclick="buyPass(20)">20 signal days</button>
   <button onclick="buyPass(60)">60 signal days</button>
  </div>
  <p id="pass-status" class="muted"></p>
  <div id="pass-box" style="display:none">
   <p id="pass-quote"></p>
   <div id="pass-qr" style="display:none;margin:.6rem 0"></div>
   <pre id="pass-bolt11"></pre>
   <p><button onclick="copyText('pass-bolt11','pass-copy-msg')">Copy invoice</button>
   <span id="pass-copy-msg"></span>
   <a id="pass-link" href="#">Open in my Lightning wallet app</a></p>
   <p class="muted">Scan the QR with your phone's wallet, or copy the invoice
   into any Lightning app. This page updates by itself once you pay (the
   invoice is good for 1 hour).</p>
   <div id="pass-help" style="display:none" class="disc">
    <b>Payment not going through?</b> Nothing has arrived yet. Most failures
    happen inside the paying wallet, before the payment leaves it:
    <ul>
     <li>Your wallet needs <b id="pass-need"></b> available (the invoice plus
     a little for network fees).</li>
     <li><b>Cash App</b> and some other apps limit Lightning payments or fail on
     larger ones — try the <b>1 signal day</b> pass first, or pay from
     <b>Strike</b> or <b>Phoenix</b>.</li>
     <li>Still stuck? Use <b>Advanced → auto-pay from a wallet the agent
     controls</b> below, or open the dashboard's <b>Help</b> panel and send
     the support report.</li>
    </ul>
   </div>
  </div>
  <details id="adv-wallet"><summary class="muted">Advanced: auto-pay each day
  from a Lightning wallet the agent controls, or use an API key</summary>
  <p class="muted">The agent pays ~$10 each market day from a wallet it holds
  the keys to. Only for people who want hands-off daily payments.</p>
  <button onclick="makeWallet()">Create my agent's wallet for me</button>
  <span id="mw-msg"></span>
  <div id="fund-box" style="display:none">
   <p>Wallet ready ✓ — now give your agent some sats. Pay this invoice from any
   Lightning app (Strike, Cash App, Phoenix, Alby…):</p>
   <label>Amount (sats) — a day pass is ~$10 of bitcoin</label>
   <input type="number" id="fund-sats" value="16000" min="1000" oninput="satsChanged()">
   <button onclick="fundInvoice()">Show invoice</button>
   <p id="fund-quote" class="muted" style="display:none"></p>
   <p id="fund-note" class="ok" style="display:none"></p>
   <div class="navrow">
    <button id="recur-btn" onclick="toggleRecurring()">Recurring day-pass: OFF</button>
    <span class="muted">ON = the agent buys each day's ~$10 pass from its
    wallet automatically (never above your auto-pay cap). OFF = it never
    spends without you.</span>
   </div>
   <div id="fund-qr" style="display:none;margin:.6rem 0"></div>
   <pre id="bolt11" style="display:none"></pre>
   <p id="fund-open" style="display:none">
   <button onclick="copyInvoice()">Copy invoice</button> <span id="copy-msg"></span>
   <a id="fund-link" href="#">Open in my Lightning wallet app</a>
   · balance: <span id="fund-bal">0</span> sats
   <span class="muted">(scan the QR with your phone's wallet, or copy the
   invoice and paste it in any Lightning app — updates automatically after
   you pay)</span></p>
   <p class="muted">Hosted wallet on demo.lnbits.com — a public TEST server
   (custodial; it can be reset). Keep only a few days of passes there, or use
   your own instance below.</p>
  </div>
   <p class="muted">Your own LNbits wallet:</p>
   <input type="text" id="ln-url" placeholder="LNbits instance URL (https://…)">
   <input type="password" id="ln-key" placeholder="LNbits wallet ADMIN key">
   <p class="muted">— or —</p>
   <input type="password" id="hc-key" placeholder="AgentHC Premium API key">
  </details>
 </div>
 <div id="src-url" style="display:none">
  <input type="text" id="src-url-input" placeholder="https://my-feed.example.com/events">
 </div>
 <div id="src-manual" style="display:none"><p class="muted">You'll append JSON
  lines to <code>~/.agentic-trader/commands.jsonl</code> — see the README.</p></div>
 <div class="navrow"><button onclick="doSource()">Save & continue</button>
 <span id="src-msg"></span></div>
</section>

<section id="s-broker"><h2>Broker — where orders execute</h2>
 <p class="muted">Pick your platform. Robinhood is one-click OAuth; key-based
 platforms take API keys (many offer a paper/simulated mode — the safest trial).</p>
 <div class="navrow"><button onclick="rhStart()">Connect Robinhood</button>
 <span id="rh-msg"></span></div>
 <div id="rh-accounts"></div>
 <p class="muted" style="margin-top:1rem">— or a key-based platform —</p>
 <select id="brk-sel" onchange="brkFields()"></select>
 <div id="brk-fields"></div>
 <div class="navrow" id="brk-connect" style="display:none">
  <button onclick="brkConnect()">Connect</button> <span id="brk-msg"></span></div>
 <details style="margin-top:1rem"><summary class="muted">Don't see your
 platform? Your agent can build the connector</summary>
  <p class="muted">Every platform needs a small adapter (different APIs). Your
  agent can WRITE one for any platform with a trading API — after setup, in the
  dashboard: <code>self-edit on</code> then
  <code>code: add a broker adapter for &lt;platform&gt; using their API</code>.
  You review the diff before it applies; it then appears here automatically.
  moomoo, Interactive Brokers, Tastytrade, Tradier, etc. all work this way.</p>
 </details>
</section>

<section id="s-sizing"><h2>Position sizing</h2>
 <p class="muted">Nobody here is an investment advisor and none of this
 software can advise position sizing — this number is YOUR decision, made
 with money you can afford to lose entirely.</p>
 <label><input type="radio" name="szmode" value="budget" checked> Dollar budget
 per trade (buys what fits; skips trades that exceed it)</label><br>
 <label><input type="radio" name="szmode" value="contracts"> Fixed contracts
 per trade</label>
 <input type="number" id="sz-val" placeholder="e.g. 500" min="1" step="any">
 <label class="muted">"Other trades" size — only used if your source includes
 the wider other-trades journal; sized separately from the main pick
 (blank = same as main)</label>
 <input type="number" id="sz-other-val" placeholder="blank = same as main"
 min="1" step="any">
 <div class="navrow"><button onclick="doSizing()">Save & continue</button>
 <span id="sz-msg"></span></div>
</section>

<section id="s-safety"><h2>Safety rails & extras</h2>
 <p><label><input type="checkbox" id="sf-dry" checked> Start in
 <b>dry-run</b> mode (log actions, place no orders — recommended)</label></p>
 <label>Max new entries per day</label>
 <input type="number" id="sf-cap" value="5" min="1">
 <label>Max daily loss in $ (optional, blank/0 = off) — once today's estimated
  realized loss reaches this, no new entries until tomorrow; exits still run</label>
 <input type="number" id="sf-maxloss" placeholder="e.g. 300" min="0">
 <label>Notifications (any/all, optional)</label>
 <input type="text" id="nt-discord" placeholder="Discord webhook URL">
 <input type="text" id="nt-ntfy" placeholder="ntfy.sh topic">
 <input type="text" id="nt-tg-token" placeholder="Telegram bot token">
 <input type="text" id="nt-tg-chat" placeholder="Telegram chat id">
 <label>My trading policy (optional — plain-English rules the agent's LLM
 checks before every entry; veto-only; uses YOUR Anthropic API key)</label>
 <textarea id="pol-text" rows="5"
  placeholder="- Never trade more than 2 new entries per day.&#10;- Skip puts.&#10;- Only tickers: SPY, QQQ, NVDA."></textarea>
 <input type="password" id="pol-key" placeholder="Claude API key (if you skipped Connect your AI — any other provider: use that step)">
 <div class="navrow"><button onclick="doSafety()">Finish setup</button>
 <span id="sf-msg"></span></div>
</section>

<section id="s-done"><h2>Done — where should your agent live?</h2>
 <div id="done-body"></div>
 <div id="deploy-box">
  <p><b>Option A — this computer:</b>
   <button onclick="startLocal()">Start my agent now</button>
   <span id="start-msg"></span></p>
  <p class="muted">One click — the agent starts here and this page becomes its
  dashboard. Keep this computer awake during market hours.</p>
  <p><b>Option B — let your agent create its own cloud server</b> (~$6/mo on
  YOUR DigitalOcean account): it provisions the server, moves its completed
  setup there, and starts itself. Your token is used once and not stored.</p>
  <input type="password" id="do-token" placeholder="DigitalOcean API token (write scope)">
  <select id="do-region"><option>nyc3</option><option>sfo3</option><option>tor1</option>
   <option>lon1</option><option>fra1</option><option>sgp1</option></select>
  <button onclick="deploy()">Create my agent's server</button>
  <div id="deploy-msg"></div>
 </div>
</section>

<div id="help">
 <p class="muted">Need help with this step? Ask your agent (works once your
 AI is connected in step 2 — any provider).</p>
 <div id="chatlog"></div>
 <div class="chatrow">
  <input type="text" id="cmd" placeholder="e.g. what is dry-run? · is the hosted wallet safe?"
   onkeydown="if(event.key==='Enter')ask()">
  <button onclick="ask()">Ask</button>
 </div>
</div>

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
function render(){
  const bar=document.getElementById('stepbar');bar.innerHTML='';
  STEPS.forEach((s,i)=>{
    const c=document.createElement('span');
    c.className='chip'+(doneSet.has(s[0])?' done':'')+(i===cur?' cur':'');
    c.textContent=(doneSet.has(s[0])?'✓ ':'')+(i+1)+' · '+s[1];
    if(i<=maxUnlocked)c.onclick=()=>{cur=i;render();};
    bar.appendChild(c);
  });
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
  p.textContent='✓ '+txt;
}
function done(id){
  // auto-advance only the FIRST time a step completes — re-saving an
  // already-done step must not yank the user off the tab they chose
  const first=!doneSet.has(id);
  doneSet.add(id);
  maxUnlocked=Math.max(maxUnlocked,Math.min(idx(id)+1,STEPS.length-1));
  if(first)cur=Math.min(idx(id)+1,STEPS.length-1);
  render();}
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
  const st=document.getElementById('pass-status');st.textContent='creating invoice…';
  const r=await api('/api/pass/buy',{days:days});
  if(!r.ok){st.innerHTML='<span class="err">'+esc(r.error)+'</span>';
    if(r.unavailable)document.getElementById('adv-wallet').open=true;return;}
  document.getElementById('pass-box').style.display='';
  document.getElementById('pass-quote').textContent=days+' signal day'+(days>1?'s':'')+' — '+
    r.amount_sats.toLocaleString()+' sats'+(r.amount_usd?' (~$'+r.amount_usd+')':'')+
    (r.carried?' · plus '+r.carried+' unused day(s) carried over':'');
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
    st.innerHTML='<span class="ok">Access active ✓ — '+(r.days_left??'?')+
      ' signal day(s) left</span>';
  }else if(r.status==='pending'){st.textContent='waiting for payment…';}
  else if(r.status==='unpaid'){
    if(passTimer){clearInterval(passTimer);passTimer=null;}
    document.getElementById('pass-box').style.display='none';
    st.innerHTML='<span class="err">That invoice expired unpaid — pick a pass again.</span>';
  }else if(r.status==='exits_only'){st.innerHTML='<span class="err">Your signal days are used up — exits still come through for a few days. Buy more above.</span>';}
  else if(r.status==='expired'||r.status==='used_up'){st.innerHTML='<span class="err">Your pass has run out — buy more days above.</span>';}
}
function srcChanged(){
  const v=document.getElementById('src').value;
  for(const k of ['agenthc','url','manual'])
    document.getElementById('src-'+k).style.display = (k===v)?'':'none';
}
async function boot(){
  const st=await api('/api/state');
  document.getElementById('disclaimer').innerHTML=mdLite(st.disclaimer);
  const notes=st.notes||{};
  if(st.consent){done('s-consent');restoredNote('s-consent',notes.consent||'Agreement signed — on file');}
  if(st.llm){done('s-llm');restoredNote('s-llm',notes.llm||'AI connected');}
  if(st.source){done('s-source');restoredNote('s-source',notes.source||('Source saved: '+st.source));
    const sel=document.getElementById('src');sel.value=st.source;srcChanged();}
  if(st.wallet&&st.wallet.connected){showWallet(st.wallet.page,'wallet connected ✓');
    document.getElementById('adv-wallet').open=true;}
  if(st.pass)checkPass();
  setRecurBtn(!!st.recurring);
  if(st.broker){done('s-broker');
    // numeric = Robinhood account last4; otherwise a key-based broker id
    const t=/^\\d+$/.test(st.broker)?'account ····'+st.broker:st.broker;
    document.getElementById('rh-msg').innerHTML='<span class="ok">connected ✓ '+esc(t)+'</span>';
    restoredNote('s-broker','Broker connected: '+t);}
  if(st.sizing){done('s-sizing');restoredNote('s-sizing',notes.sizing||'Sizing saved');}
  if(st.safety){done('s-safety');restoredNote('s-safety',notes.safety||'Safety saved');}
  cur=Math.min(maxUnlocked,STEPS.length-1);render();
  loadBrokers();
}
async function doConsent(){
  if(!document.getElementById('c-read').checked){
    document.getElementById('c-msg').innerHTML='<span class="err">check the box after reading</span>';return;}
  const r=await api('/api/consent',{phrase:document.getElementById('c-phrase').value});
  document.getElementById('c-msg').innerHTML=r.ok?'<span class="ok">accepted ✓</span>'
    :'<span class="err">'+r.error+'</span>';
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
    if(f.type==='checkbox')return '<label><input type="checkbox" id="bf-'+f.id+'"'+
      (f.default?' checked':'')+'> '+esc(f.label)+'</label><br>';
    return '<input type="'+(f.type==='password'?'password':'text')+
      '" id="bf-'+f.id+'" placeholder="'+esc(f.label)+'">';
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
  if(r.ok)done('s-broker');
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
    document.getElementById('rh-msg').innerHTML='<span class="ok">connected ✓ account ····'+st.broker+'</span>';
    if(st.rh_warning)document.getElementById('rh-accounts').innerHTML=
      '<p class="err">'+st.rh_warning+'</p>';
    done('s-broker');}
  else setTimeout(poll,1500);
}
async function doSizing(){
  const mode=document.querySelector('input[name=szmode]:checked').value;
  const r=await api('/api/sizing',{mode:mode,value:document.getElementById('sz-val').value,
    other:document.getElementById('sz-other-val').value});
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
  try{r=await api('/api/start-local',{});}
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
  log.innerHTML+='<div class="me">you: '+esc(q)+'</div>';log.scrollTop=log.scrollHeight;
  const r=await api('/api/ask',{question:q});
  log.innerHTML+='<div class="agent">'+esc(r.reply)+'</div>';log.scrollTop=log.scrollHeight;
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

_DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentic-trader</title>""" + _STYLE + """
<style>
 html{background:
   radial-gradient(1000px 640px at 85% -12%,rgba(76,141,255,.10),transparent 60%),
   var(--bg);background-repeat:no-repeat;background-attachment:fixed;min-height:100%}
 body{max-width:1180px;background:transparent}
 header{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin:.4rem 0 1rem}
 header .sp{flex:1}
 .hero-mini{display:flex;align-items:center;gap:.7rem;min-width:0}
 .hero-mini .tag{font-size:.66rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
 .hero-mini h1{margin:0;font-size:1.25rem;letter-spacing:-.02em}
 .logo{width:38px;height:38px;flex:none;border-radius:11px;position:relative;overflow:hidden;
   background:radial-gradient(125% 125% at 30% 18%,#6ea0ff,#2f6bff 52%,#1c3d97);
   box-shadow:0 6px 20px rgba(58,110,255,.45),inset 0 1px 0 rgba(255,255,255,.4)}
 .logo::after{content:"";position:absolute;inset:0;
   background:conic-gradient(from 200deg,transparent,rgba(255,255,255,.4),transparent 42%)}
 .qbtn{background:var(--sink);color:var(--fg);border:1px solid var(--line-2);font-size:.85rem;
   padding:.45rem .85rem;text-decoration:none;border-radius:8px;display:inline-block}
 .qbtn:hover{background:#1a2233;border-color:var(--accent);filter:none;text-decoration:none}
 .qbtn.warn{border-color:#78350f;color:#fbbf24}
 .pill{display:inline-block;font-size:.72rem;padding:.12rem .55rem;
   border-radius:99px;border:1px solid var(--line-2);color:var(--muted)}
 .pill.live{border-color:#14532d;color:var(--ok);background:rgba(74,222,128,.08)}
 .pill.dry{border-color:#1e3a8a;color:#93c5fd;background:rgba(76,141,255,.08)}
 .pill.paused{border-color:#78350f;color:#fbbf24;background:rgba(251,191,36,.07)}
 .pill.bad{border-color:#7f1d1d;color:var(--err);background:rgba(248,113,113,.08)}
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.75rem;margin-bottom:1rem}
 .tile{border:1px solid var(--line);border-radius:12px;padding:.75rem .9rem;
   background:linear-gradient(180deg,var(--panel),var(--panel-2));box-shadow:var(--shadow)}
 .tile .k{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
 .tile .v{font-size:1.3rem;font-weight:650;margin-top:.15rem;color:var(--fg);line-height:1.2}
 .tile .v.sm{font-size:1rem}
 .tile .s{font-size:.75rem;color:var(--muted);margin-top:.15rem}
 .meter{height:5px;border-radius:99px;background:var(--line);margin-top:.45rem;overflow:hidden}
 .meter i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent-2));width:0}
 .meter.hot i{background:linear-gradient(90deg,#f59e0b,#f87171)}
 .grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:1rem;align-items:start}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
 .col{display:flex;flex-direction:column;gap:1rem;min-width:0}
 .panel{border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);
   background:linear-gradient(180deg,var(--panel),var(--panel-2))}
 .panel h2{margin:0;padding:.65rem .9rem;border-bottom:1px solid var(--line);font-size:.78rem;
   text-transform:uppercase;letter-spacing:.07em;color:var(--muted);display:flex;gap:.5rem;align-items:center}
 .panel h2 .sp{flex:1}
 .panel .body{padding:.6rem .9rem .8rem}
 .empty{color:var(--muted);font-size:.85rem;padding:.4rem 0}
 .pos{display:flex;flex-wrap:wrap;gap:.5rem}
 .pcard{border:1px solid var(--line-2);border-radius:10px;padding:.45rem .7rem;background:var(--sink);
   display:flex;gap:.55rem;align-items:baseline}
 .pcard b{font-size:.95rem} .pcard .muted{font-size:.78rem}
 .tag{font-size:.68rem;font-weight:650;padding:.05rem .4rem;border-radius:5px}
 .tag.call{background:rgba(74,222,128,.12);color:var(--ok)} .tag.put{background:rgba(248,113,113,.12);color:var(--err)}
 .tag.dry{background:rgba(76,141,255,.12);color:#93c5fd}
 .feed{list-style:none;margin:0;padding:0;max-height:380px;overflow-y:auto}
 .feed li{display:grid;grid-template-columns:1.6rem minmax(0,1fr) auto;gap:.55rem;align-items:start;
   padding:.5rem 0;border-bottom:1px solid var(--line)}
 .feed li:last-child{border-bottom:0}
 .ic{width:1.6rem;height:1.6rem;border-radius:7px;display:grid;place-items:center;font-size:.8rem;font-weight:700}
 .ic.in{background:rgba(74,222,128,.13);color:var(--ok)} .ic.out{background:rgba(251,191,36,.13);color:#fbbf24}
 .ic.skip{background:var(--line);color:var(--muted)}
 .feed .t{font-size:.88rem} .feed .t b{font-weight:600} .feed .n{font-size:.78rem;color:var(--muted)}
 .feed .when{font-size:.75rem;color:var(--muted);white-space:nowrap}
 .pnl-pos{color:var(--ok)} .pnl-neg{color:var(--err)}
 #spark{width:100%;height:70px;display:block}
 .chatbox{display:flex;flex-direction:column;gap:.5rem}
 #chatlog{height:220px}
 .chatrow{display:flex;gap:.4rem} .chatrow input{margin:0;flex:1}
 .chips{display:flex;gap:.35rem;flex-wrap:wrap}
 .chips button{font-size:.75rem;padding:.25rem .6rem;background:var(--sink);border:1px solid var(--line-2);color:var(--muted)}
 .chips button:hover{color:var(--fg);border-color:var(--accent);filter:none}
 .hint{font-size:.76rem;color:var(--muted)}
 .navrow{display:flex;gap:.6rem;margin-top:.6rem;flex-wrap:wrap;align-items:center}
 #help{margin:0 0 1rem}
 @media(max-width:560px){body{margin-top:1rem}.tiles{grid-template-columns:1fr 1fr}
   .tile .v{font-size:1.1rem}.hero-mini h1{font-size:1.1rem}}
</style></head><body>
<header>
 <div class="hero-mini"><div class="logo" aria-hidden="true"></div>
  <div><div class="tag">Agentic Trader</div><h1>Dashboard</h1></div></div>
 <span class="pill" id="b-mode">…</span>
 <span class="pill" id="b-market"></span>
 <span class="pill paused" id="b-paused" style="display:none">⏸ paused</span>
 <span class="sp"></span>
 <a class="qbtn" id="upd-badge" href="/setup" style="display:none" title="Install it from settings (top of the page)">⬆ update available</a>
 <button class="qbtn" onclick="toggleHelp()">? help</button>
 <a class="qbtn" href="/setup">⚙ settings</a>
 <button class="qbtn" id="q-pause" onclick="quick(this.dataset.cmd)" data-cmd="pause">Pause</button>
 <button class="qbtn" id="q-dry" onclick="quick(this.dataset.cmd)" data-cmd="dry on">Dry-run</button>
</header>

<section id="help" style="display:none">
 <h2>Help</h2>
 <p class="muted">Something not working? These two buttons usually find the
 cause without any typing.</p>
 <p><button onclick="helpNet()">Check my connection</button>
 <button onclick="helpReport()">Copy support report</button>
 <span id="help-msg" class="muted"></span></p>
 <pre id="help-out" style="display:none;white-space:pre-wrap;max-height:22rem;overflow:auto"></pre>
 <p class="muted" style="font-size:.8rem">The report contains no keys, tokens,
 invoices or wallet IDs — they are removed before it's shown. Paste it to
 whoever helps you with the agent.</p>
</section>

<div class="tiles">
 <div class="tile"><div class="k">Entries today</div>
  <div class="v" id="t-entries">–</div><div class="meter" id="m-entries"><i></i></div>
  <div class="s" id="t-cap"></div></div>
 <div class="tile"><div class="k">Open positions</div>
  <div class="v" id="t-pos">–</div><div class="s">opened by this agent</div></div>
 <div class="tile" id="tile-feed"><div class="k">Signal feed</div>
  <div class="v sm" id="t-feed">–</div><div class="s" id="t-feed-s"></div></div>
 <div class="tile"><div class="k">Size per trade</div>
  <div class="v sm" id="t-sizing">–</div><div class="s">your choice — not advice</div></div>
 <div class="tile"><div class="k">Your rules</div>
  <div class="v sm" id="t-policy">–</div><div class="s" id="t-policy-hint"></div></div>
</div>

<div class="grid">
 <div class="col">
  <div class="panel" id="p-proposal" style="display:none;border-color:#78350f">
   <h2>Proposed code change — review before it applies</h2>
   <div class="body">
    <p id="prop-expl"></p><p id="prop-warn" class="err"></p>
    <pre id="prop-diff" style="max-height:260px;overflow-y:auto"></pre>
    <div class="navrow">
     <button onclick="propApply()">Apply &amp; restart</button>
     <button class="back" onclick="propReject()">Reject</button>
     <span id="prop-msg"></span>
    </div>
   </div></div>
  <div class="panel" id="p-positions"><h2>Open positions</h2>
   <div class="body"><div class="pos" id="positions"><span class="empty">none</span></div></div></div>
  <div class="panel" id="p-activity"><h2>Activity<span class="sp"></span>
   <span id="pnl-total" class="hint"></span></h2>
   <div class="body"><svg id="spark" viewBox="0 0 300 70" preserveAspectRatio="none" style="display:none"></svg>
   <ul class="feed" id="trades"><li class="empty">nothing yet</li></ul></div></div>
 </div>
 <div class="col">
  <div class="panel" id="p-chat"><h2>Talk to your agent</h2>
   <div class="body chatbox">
    <div class="chips">
     <button onclick="ask('status')">status</button>
     <button onclick="ask('set cap 3')">set cap 3</button>
     <button onclick="ask('set positions 5')">set positions 5</button>
     <button onclick="ask('why did you skip the last signal?')">why the last skip?</button>
    </div>
    <div id="chatlog"><span class="hint">Ask anything, or give a command —
    <code>pause</code>, <code>set budget 500</code>, <code>set cap 3</code>,
    <code>dry off</code>, <code>stop</code>.</span></div>
    <div class="chatrow">
     <input type="text" id="cmd" placeholder="Ask or command…"
      onkeydown="if(event.key==='Enter')send()">
     <button onclick="send()">Send</button>
    </div>
    <div class="hint">Code changes: run Claude Code in the agent's folder on
    this computer, then restart the agent.</div>
   </div></div>
 </div>
</div>
<script>
async function api(p, body){
  const r=await fetch(p, body?{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}:{});
  return await r.json();
}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function $(id){return document.getElementById(id);}
function toggleHelp(){const h=$('help');h.style.display=h.style.display==='none'?'':'none';}
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
function when(ts){
  const d=new Date(ts);if(isNaN(d))return esc(ts||'');
  const today=new Date().toDateString()===d.toDateString();
  return (today?'':d.toLocaleDateString([], {month:'short',day:'numeric'})+' ')+
    d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
}
function money(v){const n=Number(v);return (n>=0?'+$':'−$')+Math.abs(n).toFixed(2);}
function contractHtml(c){
  // "NVDA 2026-10-16 $215 PUT" -> NVDA · Oct 16 · $215 PUT
  const m=String(c||'').match(/^(\\S+)\\s+(\\d{4})-(\\d{2})-(\\d{2})\\s+\\$?([\\d.]+)\\s+(CALL|PUT)$/i);
  if(!m)return '<b>'+esc(c||'')+'</b>';
  const d=new Date(+m[2],+m[3]-1,+m[4]);
  return '<b>'+esc(m[1])+'</b> '+esc(d.toLocaleDateString([], {month:'short',day:'numeric'}))+
    ' $'+esc(m[5])+' <span class="tag '+(m[6].toUpperCase()==='CALL'?'call':'put')+'">'+
    esc(m[6].toUpperCase())+'</span>';
}
function feedTile(text){
  // "prepaid pass · 4 signal days left" / "wallet 41,000 sats" / "Premium API key"
  const t=String(text||'');
  const tile=$('tile-feed');
  const m=t.match(/(\\d+)\\s+signal day/);
  if(m){$('t-feed').textContent=m[1]+' signal day'+(m[1]==='1'?'':'s')+' left';
    $('t-feed-s').textContent='prepaid pass';}
  else{$('t-feed').textContent=t||'–';$('t-feed-s').textContent='';}
  const bad=/expired|used up|not paid|none|no access|exits only/i.test(t);
  tile.style.borderColor=bad?'#7f1d1d':'';
}
function spark(points){
  const svg=$('spark');
  if(points.length<2){svg.style.display='none';return;}
  const min=Math.min(0,...points),max=Math.max(0,...points),span=(max-min)||1;
  const x=i=>i*300/(points.length-1),y=v=>64-(v-min)*58/span;
  const d=points.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)).join(' ');
  const up=points[points.length-1]>=0;
  svg.innerHTML='<line x1="0" x2="300" y1="'+y(0).toFixed(1)+'" y2="'+y(0).toFixed(1)+
    '" stroke="#2c3446" stroke-dasharray="3 3"/><path d="'+d+'" fill="none" stroke="'+
    (up?'#4ade80':'#f87171')+'" stroke-width="2" vector-effect="non-scaling-stroke"/>';
  svg.style.display='';
}
async function refresh(){
  try{
    const s=await api('/api/status');
    if(s.mode==='OFFLINE'){
      $('b-mode').textContent='agent not running';$('b-mode').className='pill bad';
      $('t-feed').textContent='start it: agent.py run';return;}
    const f=s.fields||{};const live=s.mode==='LIVE';
    const bm=$('b-mode');
    bm.textContent=live?'● LIVE — real orders':'◌ DRY-RUN — no orders';
    bm.className='pill '+(live?'live':'dry');
    $('b-market').textContent=(f['market']||'').startsWith('open')?'market open':'market closed';
    $('b-paused').style.display=s.paused?'':'none';
    const qp=$('q-pause');qp.textContent=s.paused?'Resume':'Pause';qp.dataset.cmd=s.paused?'resume':'pause';
    const qd=$('q-dry');qd.textContent=live?'Switch to dry-run':'Go live';
    qd.dataset.cmd=live?'dry on':'dry off';qd.className='qbtn'+(live?'':' warn');
    const ent=(f['entries today']||'').split('/');
    const n=parseInt(ent[0],10)||0,cap=parseInt((ent[1]||'').replace(/\\D+/g,''),10)||0;
    $('t-entries').textContent=isNaN(parseInt(ent[0],10))?'–':n;
    $('t-cap').textContent=cap?('of '+cap+' allowed today'):'';
    const mt=$('m-entries');mt.firstChild.style.width=(cap?Math.min(100,n*100/cap):0)+'%';
    mt.className='meter'+(cap&&n>=cap?' hot':'');
    const pos=(f['open positions']||'none');
    const list=pos==='none'?[]:pos.split(', ');
    $('t-pos').textContent=list.length;
    $('positions').innerHTML=list.length?list.map(p=>{
      const a=p.split('|');
      if(a.length<4)return '<div class="pcard"><b>'+esc(p)+'</b></div>';
      return '<div class="pcard">'+contractHtml(a[0]+' '+a[1]+' $'+a[2]+' '+(a[3]==='C'?'CALL':'PUT'))+'</div>';
    }).join(''):'<span class="empty">none</span>';
    $('t-sizing').textContent=(f['sizing']||'–').replace('contract(s)/trade','contract(s)');
    const polOn=f['policy brain']==='on';
    $('t-policy').textContent=polOn?'On':'Off';
    $('t-policy-hint').textContent=polOn?'checked before every entry':'add rules in settings → Safety';
    if(f['source']==='agenthc')feedTile(f['feed access']);
    else{$('t-feed').textContent=SOURCES[f['source']]||f['source']||'–';$('t-feed-s').textContent='signal source';}
    try{
      const p=await api('/api/proposal');const panel=$('p-proposal');
      if(p.pending){panel.style.display='';
        $('prop-expl').textContent=p.explanation;
        $('prop-warn').textContent=(p.warnings||[]).join('  ');
        $('prop-diff').textContent=p.diff;}
      else panel.style.display='none';
    }catch(e){}
    const t=await api('/api/trades');
    const rows=t.trades||[];
    let cum=0;const pts=[0];
    rows.slice().reverse().forEach(x=>{if(x.action==='exit'&&x.est_pnl_usd!=null){cum+=Number(x.est_pnl_usd);pts.push(cum);}});
    spark(pts);
    $('pnl-total').innerHTML=pts.length>1?'est. P&amp;L of exits shown: <span class="'+
      (cum>=0?'pnl-pos':'pnl-neg')+'">'+money(cum)+'</span>':'';
    $('trades').innerHTML=rows.length?rows.map(x=>{
      const a=x.action||'';let ic='skip',icon='–',title;
      if(a==='entry'){ic='in';icon='▲';title='Entered '+contractHtml(x.contract);}
      else if(a==='exit'){ic='out';icon='▼';title='Exited '+contractHtml(x.contract)+
        (x.est_pnl_usd!=null?' <span class="'+(x.est_pnl_usd>=0?'pnl-pos':'pnl-neg')+'">'+money(x.est_pnl_usd)+'</span>':'');}
      else{title='Skipped '+contractHtml(x.contract)+' — '+esc(SKIPS[a]||a.replace(/_/g,' '));}
      const note=(a==='policy_veto'||a==='entry'||a==='exit')&&x.reason?esc(String(x.reason).slice(0,120)):'';
      return '<li><span class="ic '+ic+'">'+icon+'</span><div><div class="t">'+title+
        (x.dry?' <span class="tag dry">dry-run</span>':'')+'</div>'+
        (note?'<div class="n">'+note+'</div>':'')+'</div><span class="when">'+when(x.ts)+'</span></li>';
    }).join(''):'<li class="empty">nothing yet — signals and the agent\\'s decisions show up here</li>';
  }catch(e){$('b-mode').textContent='agent unreachable';$('b-mode').className='pill bad';}
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
  log.insertAdjacentHTML('beforeend','<div class="'+cls+'">'+(cls==='me'?'you: ':'')+esc(text)+'</div>');
  log.scrollTop=log.scrollHeight;
}
async function quick(cmd){
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
            return self._send(None, html=self.pages[path])
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


def _net_error_text(exc):
    """Error text for the wizard; connection failures get the diagnosed
    cause + fix (antivirus, VPN, DNS filter…) instead of a raw SSL error."""
    import net_check
    if net_check.is_connection_error(exc):
        try:
            from agent import _agenthc_host
            diag = net_check.diagnose(_agenthc_host())
        except Exception:
            diag = None
        if diag and not diag["ok"]:
            return net_check.summary(diag)
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
    if cfg.get("sizing_mode") == "budget":
        notes["sizing"] = (f"Sizing saved: up to "
                           f"${cfg.get('budget_per_trade_usd', 0):g} per trade")
    elif cfg.get("sizing_mode"):
        n = cfg.get("contracts_per_trade", 1)
        notes["sizing"] = f"Sizing saved: {n} contract{'s' if n != 1 else ''} per trade"
    if notes.get("sizing") and cfg.get("other_sizing_mode"):
        if cfg["other_sizing_mode"] == "budget":
            notes["sizing"] += (" · other trades "
                                f"${cfg.get('other_budget_per_trade_usd', 0):g}/trade")
        else:
            notes["sizing"] += (" · other trades "
                                f"{cfg.get('other_contracts_per_trade', 1)} contract(s)")
    if cfg.get("max_entries_per_day") is not None:
        notes["safety"] = (
            f"Safety saved: {'DRY-RUN' if cfg.get('dry_run', True) else 'LIVE'}"
            f" · max {cfg.get('max_entries_per_day')} entries/day")
    return notes


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
            "sizing": bool(cfg.get("sizing_mode")),
            "safety": cfg.get("max_entries_per_day") is not None,
            "notes": _restore_notes(cfg, A._load(A.ACCEPTANCE_PATH, {})),
            "wallet": {
                "connected": bool(cfg.get("lnbits_url")
                                  and cfg.get("lnbits_admin_key")),
                "page": cfg.get("lnbits_wallet_page", ""),
            },
            "recurring": bool(cfg.get("day_pass_recurring")),
            "pass": bool(cfg.get("feed_pass") or cfg.get("feed_pass_pending")),
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
        pid = str(data.get("provider", "")).strip() or llm_client.detect_provider(key)
        base = str(data.get("base_url", "")).strip()
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
        return {"ok": True, "provider": pid, "models": models,
                "suggested": llm_client.suggest_model(pid, models)}

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
        except hc.PassSizeUnavailable as exc:
            return {"ok": False, "error": str(exc)}
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
            return {"ok": False, "status": "error", "error": str(exc)[:160]}

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
        mode = data.get("mode")
        try:
            val = float(data.get("value") or 0)
        except (TypeError, ValueError):
            val = 0
        if val <= 0:
            return {"ok": False, "error": "enter a positive number"}
        if mode == "budget":
            cfg["sizing_mode"] = "budget"
            cfg["budget_per_trade_usd"] = val
            cfg.setdefault("contracts_per_trade", 1)
        else:
            cfg["sizing_mode"] = "contracts"
            cfg["contracts_per_trade"] = max(1, int(val))
        cfg.setdefault("max_contracts_per_trade", 100)
        # Optional per-track override: "other trades" journal events sized
        # separately from the main pick (same mode); blank clears it.
        other_raw = str(data.get("other") or "").strip()
        if other_raw:
            try:
                oval = float(other_raw)
            except (TypeError, ValueError):
                oval = 0
            if oval <= 0:
                return {"ok": False,
                        "error": "other-trades size must be a positive number"}
            if mode == "budget":
                cfg["other_sizing_mode"] = "budget"
                cfg["other_budget_per_trade_usd"] = oval
                cfg.pop("other_contracts_per_trade", None)
            else:
                cfg["other_sizing_mode"] = "contracts"
                cfg["other_contracts_per_trade"] = max(1, int(oval))
                cfg.pop("other_budget_per_trade_usd", None)
        else:
            for k in ("other_sizing_mode", "other_budget_per_trade_usd",
                      "other_contracts_per_trade"):
                cfg.pop(k, None)
        save()
        return {"ok": True}

    def safety(_h, data):
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
               + platform_compat.venv_python() + " agent.py run\n\n"
               "(or enable the systemd service / Docker container — see "
               "GETTING_STARTED.md)")
        return {"ok": True, "next": nxt}

    def finish(_h, _data):
        pending["finished"].set()
        return {"ok": True}

    def start_local(_h, _data):
        """Option A as one click: spawn the agent detached, then hand it this
        port. The child waits until the wizard's socket is actually free
        before exec'ing `agent.py run`, so the dashboard (same port) can't
        lose the bind race and fall back to headless."""
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from brokers import broker_ready
        if not (cfg.get("source") and broker_ready(cfg)):
            return {"ok": False,
                    "error": "finish the source and broker steps first"}
        save()
        import os
        import platform_compat
        script = os.path.join(_repo_dir(), "agent.py")
        log_path = os.path.join(os.path.dirname(A.CONFIG_PATH), "agent-run.log")
        try:
            platform_compat.spawn_after_port_free(
                script, ["run"], APP_PORT, log_path=log_path, cwd=_repo_dir())
        except Exception as exc:
            return {"ok": False, "error": f"could not start the agent: {exc}"}
        # release the port AFTER this response has flushed
        threading.Timer(0.5, pending["finished"].set).start()
        return {"ok": True}

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
                    "/api/status": d_status, "/api/trades": d_trades,
                    "/api/proposal": proposal_get}
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
                     "/api/start-local": start_local,
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
                  "is taking over this address — the browser page becomes the "
                  "dashboard. Otherwise start it any time with: agent.py run")
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
                "set positions N, "
                "set cap N, stop). Never give investment advice; you may "
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
