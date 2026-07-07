"""Local web UI — a setup wizard and a live dashboard, no terminal needed.

Both servers bind 127.0.0.1 ONLY (never exposed to the network). On a remote
VPS, reach them through an SSH tunnel:  ssh -L 8722:127.0.0.1:8722 user@host

Wizard (``python agent.py setup --web``, port 8721):
    Full setup in the browser — the consent gate (checkbox + typed phrase,
    still the human's own affirmative act), signal source + Lightning wallet,
    Robinhood OAuth (seamless: the wizard listens on the same 127.0.0.1:8721
    the OAuth redirect targets, so approval lands back here automatically),
    dollar-budget or contract sizing, and the safety rails.

Dashboard (started automatically by ``python agent.py run``, port 8722):
    The user's OWN agent serves its own UI: live status, trade log, and a
    command box — pause / resume / dry on|off / set budget N / set cap N /
    stop, plus free-text questions answered by the LLM (user's API key)
    against the live status and trade log. Command handling is deliberately
    a fixed allowlist; the LLM can only ANSWER, never trade.

Stdlib only (http.server + threading) — no new dependencies.
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_PORT = 8721      # one app, one port (also the Robinhood OAuth redirect port)
WIZARD_PORT = APP_PORT
DASH_PORT = APP_PORT

# Run-loop controls the dashboard can flip; agent.py polls these.
CONTROLS = {"paused": False, "stop": False}

_STYLE = """
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;
      padding:0 1rem;background:#0f1115;color:#e6e6e6;line-height:1.5}
 h1{font-size:1.4rem} h2{font-size:1.05rem;margin:1.6rem 0 .4rem}
 section{border:1px solid #2a2f3a;border-radius:10px;padding:1rem;margin:1rem 0;
         background:#161a22}
 section.done{border-color:#2e7d32} section.locked{opacity:.45;pointer-events:none}
 input[type=text],input[type=password],input[type=number],textarea,select{
   width:100%;box-sizing:border-box;padding:.5rem;margin:.25rem 0 .6rem;
   background:#0f1115;color:#e6e6e6;border:1px solid #2a2f3a;border-radius:6px}
 button{padding:.5rem 1rem;border:0;border-radius:6px;background:#3b82f6;
        color:#fff;cursor:pointer;font-size:.95rem} button:disabled{background:#333}
 .muted{color:#9aa3b2;font-size:.85rem} .ok{color:#4ade80} .err{color:#f87171}
 pre{background:#0b0d11;padding:.7rem;border-radius:6px;overflow-x:auto;
     white-space:pre-wrap;word-break:break-word;font-size:.8rem}
 .disc{max-height:280px;overflow-y:auto;border:1px solid #2a2f3a;padding:.8rem;
       border-radius:6px;font-size:.82rem;background:#0b0d11}
 .badge{font-size:.75rem;padding:.1rem .5rem;border-radius:99px;
        background:#2a2f3a;margin-left:.5rem}
 .badge.on{background:#14532d;color:#4ade80}
 #chatlog{height:300px;overflow-y:auto;background:#0b0d11;border-radius:6px;
          padding:.7rem;font-size:.85rem}
 .me{color:#93c5fd} .agent{color:#e6e6e6;margin-bottom:.6rem;white-space:pre-wrap}
 table{width:100%;border-collapse:collapse;font-size:.82rem}
 td,th{padding:.3rem .5rem;border-bottom:1px solid #232936;text-align:left}
</style>"""

_WIZARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentic-trader setup</title>""" + _STYLE + """
<style>
 body{max-width:820px}
 .stepbar{display:flex;gap:.35rem;flex-wrap:wrap;margin:1rem 0 1.2rem}
 .chip{font-size:.75rem;padding:.25rem .7rem;border-radius:99px;
       border:1px solid #2a2f3a;color:#9aa3b2;cursor:default;user-select:none}
 .chip.done{border-color:#2e7d32;color:#4ade80;cursor:pointer}
 .chip.cur{border-color:#3b82f6;color:#93c5fd;background:#101827}
 section{display:none}
 section.active{display:block}
 .navrow{display:flex;gap:.6rem;margin-top:.8rem}
 .navrow .back{background:#232936;border:1px solid #2a2f3a}
 #help{margin-top:1.4rem;border-top:1px solid #232936;padding-top:.8rem}
 #chatlog{height:170px}
 .chatrow{display:flex;gap:.4rem}.chatrow input{margin:0;flex:1}
</style></head><body>
<h1>agentic-trader — setup <a class="chip done" href="/dash" style="text-decoration:none">→ dashboard</a></h1>
<p class="muted">Everything happens on YOUR machine (served from 127.0.0.1 by
your agent's own wizard). Real money; read carefully.</p>
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
 <p class="muted">Your agent becomes a real AI agent with an Anthropic API key
 (pay-per-use, separate from a Claude subscription — console.anthropic.com).
 It powers the help chat, your plain-English trading policy, and the dashboard
 Q&A. Skippable; add it later any time.</p>
 <input type="password" id="llm-key" placeholder="sk-ant-…">
 <div class="navrow"><button onclick="doLLM()">Connect & continue</button>
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
  <p class="muted">Pay-as-you-go: ~$10/day in sats, paid automatically from the
  agent's own Lightning wallet.</p>
  <button onclick="makeWallet()">Create my agent's wallet for me</button>
  <span id="mw-msg"></span>
  <div id="fund-box" style="display:none">
   <p>Wallet ready ✓ — now give your agent some sats. Pay this invoice from any
   Lightning app (Strike, Cash App, Phoenix, Alby…):</p>
   <label>Amount (sats — ~50,000 ≈ a month of market days)</label>
   <input type="number" id="fund-sats" value="50000" min="1000">
   <button onclick="fundInvoice()">Show invoice</button>
   <pre id="bolt11" style="display:none"></pre>
   <p id="fund-open" style="display:none"><a id="fund-link" href="#">Open in my
   Lightning wallet app</a> · balance: <span id="fund-bal">0</span> sats
   <span class="muted">(updates automatically after you pay)</span></p>
   <p class="muted">Hosted wallet on demo.lnbits.com (custodial) — the agent
   keeps only spending money here. Advanced: use your own instance below.</p>
  </div>
  <details><summary class="muted">Advanced: my own wallet or API key</summary>
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

<section id="s-broker"><h2>Broker</h2>
 <p class="muted">Where should orders execute? Robinhood is one click; Alpaca
 takes API keys and offers a <b>paper mode</b> (simulated fills, zero real
 dollars — the safest full trial).</p>
 <div class="navrow"><button onclick="rhStart()">Connect Robinhood</button>
 <span id="rh-msg"></span></div>
 <div id="rh-accounts"></div>
 <details><summary class="muted">Use Alpaca instead (paper or live)</summary>
  <input type="text" id="ap-key" placeholder="Alpaca API Key ID">
  <input type="password" id="ap-secret" placeholder="Alpaca API Secret">
  <p><label><input type="checkbox" id="ap-paper" checked> Paper trading
  (recommended)</label></p>
  <button onclick="doAlpaca()">Connect Alpaca</button> <span id="ap-msg"></span>
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
 <div class="navrow"><button onclick="doSizing()">Save & continue</button>
 <span id="sz-msg"></span></div>
</section>

<section id="s-safety"><h2>Safety rails & extras</h2>
 <p><label><input type="checkbox" id="sf-dry" checked> Start in
 <b>dry-run</b> mode (log actions, place no orders — recommended)</label></p>
 <label>Max new entries per day</label>
 <input type="number" id="sf-cap" value="5" min="1">
 <label>Notifications (any/all, optional)</label>
 <input type="text" id="nt-discord" placeholder="Discord webhook URL">
 <input type="text" id="nt-ntfy" placeholder="ntfy.sh topic">
 <input type="text" id="nt-tg-token" placeholder="Telegram bot token">
 <input type="text" id="nt-tg-chat" placeholder="Telegram chat id">
 <label>My trading policy (optional — plain-English rules the agent's LLM
 checks before every entry; veto-only; uses YOUR Anthropic API key)</label>
 <textarea id="pol-text" rows="5"
  placeholder="- Never trade more than 2 new entries per day.&#10;- Skip puts.&#10;- Only tickers: SPY, QQQ, NVDA."></textarea>
 <input type="password" id="pol-key" placeholder="Anthropic API key (if not connected earlier)">
 <div class="navrow"><button onclick="doSafety()">Finish setup</button>
 <span id="sf-msg"></span></div>
</section>

<section id="s-done"><h2>Done — where should your agent live?</h2>
 <div id="done-body"></div>
 <div id="deploy-box">
  <p><b>Option A — this computer:</b> run the command above; keep it awake
  during market hours.</p>
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
 Anthropic key is connected).</p>
 <div id="chatlog"></div>
 <div class="chatrow">
  <input type="text" id="cmd" placeholder="e.g. what is dry-run? · is the hosted wallet safe?"
   onkeydown="if(event.key==='Enter')ask()">
  <button onclick="ask()">Ask</button>
 </div>
</div>

<script>
const STEPS=[["s-consent","Agreement"],["s-llm","Your AI"],["s-source","Source"],
 ["s-broker","Robinhood"],["s-sizing","Sizing"],["s-safety","Safety"],["s-done","Launch"]];
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
function done(id){doneSet.add(id);
  maxUnlocked=Math.max(maxUnlocked,Math.min(idx(id)+1,STEPS.length-1));
  cur=Math.min(idx(id)+1,STEPS.length-1);render();}
function unlock(id){maxUnlocked=Math.max(maxUnlocked,idx(id));render();}
async function api(p, body){
  const r = await fetch(p, body?{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}:{});
  return await r.json();
}
function srcChanged(){
  const v=document.getElementById('src').value;
  for(const k of ['agenthc','url','manual'])
    document.getElementById('src-'+k).style.display = (k===v)?'':'none';
}
async function boot(){
  const st=await api('/api/state');
  document.getElementById('disclaimer').textContent=st.disclaimer;
  if(st.consent)done('s-consent');
  if(st.llm)done('s-llm');
  if(st.source)done('s-source');
  if(st.broker){done('s-broker');
    document.getElementById('rh-msg').innerHTML='<span class="ok">connected ✓ account ····'+st.broker+'</span>';}
  cur=Math.min(maxUnlocked,STEPS.length-1);render();
}
async function doConsent(){
  if(!document.getElementById('c-read').checked){
    document.getElementById('c-msg').innerHTML='<span class="err">check the box after reading</span>';return;}
  const r=await api('/api/consent',{phrase:document.getElementById('c-phrase').value});
  document.getElementById('c-msg').innerHTML=r.ok?'<span class="ok">accepted ✓</span>'
    :'<span class="err">'+r.error+'</span>';
  if(r.ok)done('s-consent');
}
async function doLLM(){
  const r=await api('/api/llm',{key:document.getElementById('llm-key').value});
  document.getElementById('llm-msg').innerHTML=r.ok?' <span class="ok">connected ✓</span>'
    :' <span class="err">'+r.error+'</span>';
  if(r.ok)done('s-llm');
}
async function skipLLM(){done('s-llm');}
let walletMade=false, balTimer=null;
async function makeWallet(){
  document.getElementById('mw-msg').textContent=' creating…';
  const r=await api('/api/wallet/create',{});
  if(r.ok){walletMade=true;
    document.getElementById('mw-msg').innerHTML=' <span class="ok">created ✓</span>';
    document.getElementById('fund-box').style.display='';}
  else document.getElementById('mw-msg').innerHTML=' <span class="err">'+r.error+'</span>';
}
async function fundInvoice(){
  const sats=document.getElementById('fund-sats').value;
  const r=await api('/api/wallet/fund',{sats:sats});
  if(!r.ok){document.getElementById('mw-msg').innerHTML=' <span class="err">'+r.error+'</span>';return;}
  const pre=document.getElementById('bolt11');pre.style.display='';pre.textContent=r.bolt11;
  document.getElementById('fund-open').style.display='';
  document.getElementById('fund-link').href='lightning:'+r.bolt11;
  if(balTimer)clearInterval(balTimer);
  balTimer=setInterval(async()=>{const b=await api('/api/wallet/balance');
    if(b.ok)document.getElementById('fund-bal').textContent=b.sats.toLocaleString();},4000);
}
async function doSource(){
  const v=document.getElementById('src').value;
  const r=await api('/api/source',{source:v,
    lnbits_url:document.getElementById('ln-url').value,
    lnbits_key:document.getElementById('ln-key').value,
    agenthc_key:document.getElementById('hc-key').value,
    source_url:document.getElementById('src-url-input').value});
  document.getElementById('src-msg').innerHTML=r.ok?'<span class="ok">'+r.note+'</span>'
    :'<span class="err">'+r.error+'</span>';
  if(r.ok)done('s-source');
}
async function doAlpaca(){
  const r=await api('/api/alpaca',{key:document.getElementById('ap-key').value,
    secret:document.getElementById('ap-secret').value,
    paper:document.getElementById('ap-paper').checked});
  document.getElementById('ap-msg').innerHTML=r.ok?' <span class="ok">'+r.note+'</span>'
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
  const r=await api('/api/sizing',{mode:mode,value:document.getElementById('sz-val').value});
  document.getElementById('sz-msg').innerHTML=r.ok?'<span class="ok">saved ✓</span>'
    :'<span class="err">'+r.error+'</span>';
  if(r.ok)done('s-sizing');
}
async function doSafety(){
  const r=await api('/api/safety',{dry:document.getElementById('sf-dry').checked,
    cap:document.getElementById('sf-cap').value,
    discord:document.getElementById('nt-discord').value,
    ntfy:document.getElementById('nt-ntfy').value,
    tg_token:document.getElementById('nt-tg-token').value,
    tg_chat:document.getElementById('nt-tg-chat').value,
    policy:document.getElementById('pol-text').value,
    anthropic_key:document.getElementById('pol-key').value});
  if(r.ok){done('s-safety');
    document.getElementById('done-body').innerHTML=
     '<p class="ok">Setup complete.</p><pre>'+r.next+'</pre>'+
     '<p class="muted">The dashboard lives at http://127.0.0.1:8722 once the '+
     'agent is running.</p>';
    api('/api/finish',{});}
  else document.getElementById('sf-msg').innerHTML='<span class="err">'+r.error+'</span>';
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
      +'anything. Dashboard from here: ssh -L 8721:127.0.0.1:8721 trader@'+s.ip
      +' then open http://127.0.0.1:8721/dash</span>';}
    else{el.textContent='server status: '+s.status+' — waiting…';setTimeout(poll2,5000);}
  };
  poll2();
}
async function ask(){
  const box=document.getElementById('cmd');const q=box.value.trim();if(!q)return;
  box.value='';const log=document.getElementById('chatlog');
  const esc=s=>{const d=document.createElement('div');d.textContent=s;return d.innerHTML;};
  log.innerHTML+='<div class="me">you: '+esc(q)+'</div>';log.scrollTop=log.scrollHeight;
  const r=await api('/api/ask',{question:q});
  log.innerHTML+='<div class="agent">'+esc(r.reply)+'</div>';log.scrollTop=log.scrollHeight;
}
boot();
</script></body></html>"""

_DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentic-trader</title>""" + _STYLE + """
<style>
 body{max-width:1180px}
 header{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin:.4rem 0 1rem}
 header h1{margin:0;font-size:1.25rem}
 header .sp{flex:1}
 .qbtn{background:#232936;color:#e6e6e6;border:1px solid #2a2f3a;font-size:.85rem}
 .qbtn:hover{background:#2a3242}
 .grid{display:grid;grid-template-columns:2fr 1fr;gap:1rem;align-items:start}
 @media(max-width:900px){.grid{grid-template-columns:1fr}}
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
        gap:.8rem;margin-bottom:1rem}
 .tile{border:1px solid #2a2f3a;border-radius:10px;background:#161a22;
       padding:.7rem .9rem}
 .tile .k{font-size:.72rem;color:#9aa3b2;text-transform:uppercase;
          letter-spacing:.04em}
 .tile .v{font-size:1.35rem;font-weight:600;margin-top:.15rem}
 .tile .s{font-size:.75rem;color:#9aa3b2}
 .panel{border:1px solid #2a2f3a;border-radius:10px;background:#161a22;
        display:flex;flex-direction:column;min-height:0}
 .panel h2{margin:0;padding:.6rem .9rem;border-bottom:1px solid #232936;
           font-size:.85rem;text-transform:uppercase;letter-spacing:.04em;
           color:#9aa3b2}
 .panel .body{padding:.4rem .9rem .7rem;overflow-y:auto}
 #p-activity .body{max-height:320px}
 #p-positions .body{max-height:160px}
 .col-left{display:flex;flex-direction:column;gap:1rem;min-width:0}
 #p-chat{position:sticky;top:1rem;height:calc(100vh - 2rem);max-height:640px}
 #p-chat .body{flex:1;display:flex;flex-direction:column;gap:.5rem}
 #chatlog{flex:1;height:auto}
 .chatrow{display:flex;gap:.4rem}
 .chatrow input{margin:0;flex:1}
 .pill{display:inline-block;font-size:.72rem;padding:.05rem .5rem;
       border-radius:99px;border:1px solid #2a2f3a;color:#9aa3b2}
 .pill.live{border-color:#7f1d1d;color:#fca5a5}
 .pill.dry{border-color:#1e3a8a;color:#93c5fd}
 .pill.paused{border-color:#78350f;color:#fbbf24}
</style></head><body>
<header>
 <h1>agentic-trader</h1>
 <span class="pill" id="b-mode">…</span>
 <span class="pill" id="b-market"></span>
 <span class="pill paused" id="b-paused" style="display:none">⏸ PAUSED</span>
 <span class="sp"></span>
 <a class="qbtn" href="/setup" style="text-decoration:none;padding:.5rem 1rem;border-radius:6px">⚙ settings</a>
 <button class="qbtn" id="q-pause" onclick="quick(this.dataset.cmd)" data-cmd="pause">Pause</button>
 <button class="qbtn" id="q-dry" onclick="quick(this.dataset.cmd)" data-cmd="dry on">Dry-run</button>
</header>

<div class="tiles">
 <div class="tile"><div class="k">Entries today</div>
  <div class="v" id="t-entries">–</div><div class="s" id="t-cap"></div></div>
 <div class="tile"><div class="k">Open positions</div>
  <div class="v" id="t-pos">–</div><div class="s">only ones this agent opened</div></div>
 <div class="tile"><div class="k">Sizing</div>
  <div class="v" id="t-sizing" style="font-size:1.05rem">–</div>
  <div class="s">your decision — not advice</div></div>
 <div class="tile"><div class="k">Policy brain</div>
  <div class="v" id="t-policy" style="font-size:1.05rem">–</div>
  <div class="s" id="t-source"></div></div>
</div>

<div class="grid">
 <div class="col-left">
  <div class="panel" id="p-positions"><h2>Open positions</h2>
   <div class="body" id="positions"><span class="muted">none</span></div></div>
  <div class="panel" id="p-activity"><h2>Activity</h2>
   <div class="body" id="trades"><span class="muted">nothing yet</span></div></div>
  <p class="muted" style="font-size:.78rem">Settings change in chat
  (<code>set budget 500</code>, <code>set cap 3</code>, <code>dry off</code>,
  <code>stop</code>). Code changes: run Claude Code in the repo folder on this
  machine, then restart the agent.</p>
 </div>
 <div class="panel" id="p-chat"><h2>Talk to your agent</h2>
  <div class="body">
   <div id="chatlog"></div>
   <div class="chatrow">
    <input type="text" id="cmd" placeholder="pause · set budget 500 · or ask anything…"
     onkeydown="if(event.key==='Enter')send()">
    <button onclick="send()">Send</button>
   </div>
  </div>
 </div>
</div>
<script>
async function api(p, body){
  const r=await fetch(p, body?{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}:{});
  return await r.json();
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
async function refresh(){
  try{
    const s=await api('/api/status');
    if(s.mode==='OFFLINE'){
      document.getElementById('b-mode').textContent='agent not running';
      document.getElementById('b-mode').className='pill paused';
      document.getElementById('t-source').textContent='start it: agent.py run';
      return;}
    const live=s.mode==='LIVE';
    const bm=document.getElementById('b-mode');
    bm.textContent=live?'● LIVE — real orders':'◌ DRY-RUN — no orders';
    bm.className='pill '+(live?'live':'dry');
    document.getElementById('b-market').textContent=
      (s.fields['market']||'').startsWith('open')?'market open':'market closed';
    document.getElementById('b-paused').style.display=s.paused?'':'none';
    const qp=document.getElementById('q-pause');
    qp.textContent=s.paused?'Resume':'Pause';qp.dataset.cmd=s.paused?'resume':'pause';
    const qd=document.getElementById('q-dry');
    qd.textContent=live?'Switch to dry-run':'Go live';
    qd.dataset.cmd=live?'dry on':'dry off';
    const ent=(s.fields['entries today']||'').split('/');
    document.getElementById('t-entries').textContent=(ent[0]||'–').trim();
    document.getElementById('t-cap').textContent='of '+((ent[1]||'').replace('cap','').trim()||'?')+' daily cap';
    const pos=(s.fields['open positions']||'none');
    const list=pos==='none'?[]:pos.split(', ');
    document.getElementById('t-pos').textContent=list.length;
    document.getElementById('positions').innerHTML = list.length ?
      '<table>'+list.map(p=>{const a=p.split('|');
        return '<tr><td>'+esc(a[0]||p)+'</td><td>'+esc(a[1]||'')+'</td><td>$'+
        esc(a[2]||'')+'</td><td>'+esc(a[3]==='C'?'CALL':a[3]==='P'?'PUT':'')+'</td></tr>';
      }).join('')+'</table>' : '<span class="muted">none</span>';
    document.getElementById('t-sizing').textContent=s.fields['sizing']||'–';
    document.getElementById('t-policy').textContent=
      (s.fields['policy brain']==='on')?'ON (your rules)':'off';
    document.getElementById('t-source').textContent='source: '+(s.fields['source']||'–');
    const t=await api('/api/trades');
    document.getElementById('trades').innerHTML = t.trades.length ?
      '<table><tr><th>time (UTC)</th><th>action</th><th>contract</th><th>note</th></tr>'+
      t.trades.map(x=>'<tr><td>'+esc((x.ts||'').replace('T',' ').slice(5,16))+
        '</td><td>'+esc(x.action||'')+'</td><td>'+esc(x.contract||'')+
        '</td><td>'+esc((x.reason||'').slice(0,80))+'</td></tr>').join('')+'</table>'
      : '<span class="muted">nothing yet</span>';
  }catch(e){document.getElementById('status')&&(document.getElementById('status').textContent='agent unreachable');}
}
async function quick(cmd){
  const r=await api('/api/command',{text:cmd});
  const log=document.getElementById('chatlog');
  log.innerHTML+='<div class="me">you: '+esc(cmd)+'</div><div class="agent">'+esc(r.reply)+'</div>';
  log.scrollTop=log.scrollHeight;refresh();
}
async function send(){
  const box=document.getElementById('cmd');const text=box.value.trim();if(!text)return;
  box.value='';const log=document.getElementById('chatlog');
  log.innerHTML+='<div class="me">you: '+esc(text)+'</div>';
  log.scrollTop=log.scrollHeight;
  const r=await api('/api/command',{text:text});
  log.innerHTML+='<div class="agent">'+esc(r.reply)+'</div>';
  log.scrollTop=log.scrollHeight;refresh();
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
        path = self.path.split("?")[0]
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
            "broker": pending["broker_last4"] or
                      (cfg.get("robinhood_account", "")[-4:]
                       if cfg.get("robinhood_account") else ""),
            "rh_warning": pending["rh_warning"],
            "llm": bool(cfg.get("anthropic_api_key")),
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
            elif cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
                note = "using the wallet created above ✓"
            else:
                return {"ok": False,
                        "error": "create a wallet above, or provide your own / an API key"}
        save()
        return {"ok": True, "note": note}

    def llm(_h, data):
        key = str(data.get("key", "")).strip()
        if not key.startswith("sk-ant-"):
            return {"ok": False, "error": "that doesn't look like an Anthropic key"}
        try:
            import requests as _rq
            r = _rq.get("https://api.anthropic.com/v1/models",
                        headers={"x-api-key": key,
                                 "anthropic-version": "2023-06-01"}, timeout=10)
            if r.status_code == 401:
                return {"ok": False, "error": "key rejected (401)"}
            if r.status_code != 200:
                return {"ok": False, "error": f"validation failed HTTP {r.status_code}"}
        except Exception as exc:
            return {"ok": False, "error": f"could not validate: {str(exc)[:100]}"}
        cfg["anthropic_api_key"] = key
        cfg.setdefault("llm_model", "claude-opus-4-8")
        cfg.setdefault("llm_fallback", "skip")
        save()
        return {"ok": True}

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

    def alpaca_connect(_h, data):
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from brokers import alpaca as _ap
        cfg["alpaca_key_id"] = str(data.get("key", "")).strip()
        cfg["alpaca_secret"] = str(data.get("secret", "")).strip()
        cfg["alpaca_paper"] = bool(data.get("paper", True))
        cfg["broker"] = "alpaca"
        ok, msg = _ap.verify(cfg)
        if not ok:
            return {"ok": False, "error": msg}
        pending["broker_last4"] = "alp·" + ("PPR" if cfg["alpaca_paper"] else "LIV")
        save()
        return {"ok": True, "note": msg}

    def wallet_create(_h, _data):
        if not A.consent_ok():
            return {"ok": False, "error": "accept the agreement first"}
        from lightning_wallet import create_wallet
        try:
            url, key = create_wallet()
        except WalletError as exc:
            return {"ok": False, "error": str(exc)[:200]}
        cfg["source"] = "agenthc"
        cfg["lnbits_url"] = url
        cfg["lnbits_admin_key"] = key
        cfg.setdefault("max_autopay_sats", 30_000)
        save()
        return {"ok": True}

    def wallet_fund(_h, data):
        w = _wallet()
        if w is None:
            return {"ok": False, "error": "no wallet yet"}
        try:
            sats = max(1000, int(data.get("sats") or 50000))
            return {"ok": True, "bolt11": w.create_invoice(
                sats, memo="fund agentic-trader")}
        except (WalletError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:200]}

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
        cfg.setdefault("max_contracts_per_trade", 25)
        save()
        return {"ok": True}

    def safety(_h, data):
        cfg["dry_run"] = bool(data.get("dry", True))
        try:
            cfg["max_entries_per_day"] = max(1, int(data.get("cap") or 5))
        except (TypeError, ValueError):
            cfg["max_entries_per_day"] = 5
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
            with open(path, "w") as f:
                f.write(policy + "\n")
            key = str(data.get("anthropic_key", "")).strip()
            if key:
                cfg["anthropic_api_key"] = key
            cfg.setdefault("llm_model", "claude-opus-4-8")
            cfg.setdefault("llm_fallback", "skip")
        cfg["poll_seconds"] = int(cfg.get("poll_seconds", 30))
        save()
        nxt = ("cd " + _repo_dir() + "\n"
               "./.venv/bin/python agent.py run\n\n"
               "(or enable the systemd service / Docker container — see "
               "GETTING_STARTED.md)")
        return {"ok": True, "next": nxt}

    def finish(_h, _data):
        pending["finished"].set()
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
                    "/api/status": d_status, "/api/trades": d_trades}
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
                     "/api/wallet/create": wallet_create,
                     "/api/wallet/fund": wallet_fund,
                     "/api/llm": llm, "/api/deploy": deploy,
                     "/api/alpaca": alpaca_connect,
                     "/api/command": d_command}

    class _Redirect(Exception):
        pass

    # patch GET to turn /callback success into a browser redirect to /
    orig_get = W.do_GET

    def do_GET(self):
        if self.path.startswith("/callback"):
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

    server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), W)
    url = f"http://127.0.0.1:{APP_PORT}/"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    if wait_finish:
        print(f"Setup running at {url}setup  (Ctrl-C to abort)")
        print("On a remote server, tunnel first:  "
              f"ssh -L {APP_PORT}:127.0.0.1:{APP_PORT} user@host")
        try:
            webbrowser.open(url + "setup")
        except Exception:
            pass
        try:
            pending["finished"].wait()
            print("Setup complete — start the agent with: agent.py run "
                  "(the same URL then serves the dashboard).")
        except KeyboardInterrupt:
            print("\nWizard aborted.")
        server.shutdown()
        return None
    return url + "dash"


def run_wizard():
    start_app(wait_finish=True)


def _repo_dir():
    import os
    return os.path.dirname(os.path.abspath(__file__))


# ── the dashboard (served BY the running agent) ──────────────────────────────

def _ask_llm(cfg, question, status_fields, trades, system=None):
    """Free-text Q&A about the agent's own state. Answer-only, never trades."""
    try:
        import anthropic
    except ImportError:
        return ("I can answer questions if you add your Anthropic API key "
                "(anthropic_api_key in config.json) and `pip install anthropic`.")
    if not (cfg.get("anthropic_api_key") or __import__("os").getenv("ANTHROPIC_API_KEY")):
        return ("Add your Anthropic API key (anthropic_api_key in config.json "
                "or ANTHROPIC_API_KEY) to enable Q&A.")
    try:
        client = anthropic.Anthropic(api_key=cfg.get("anthropic_api_key") or None)
        response = client.messages.create(
            model=cfg.get("llm_model", "claude-opus-4-8"),
            max_tokens=1024,
            system=system or (
                "You are the status assistant embedded in the user's own "
                "agentic-trader instance. Answer questions using ONLY the "
                "status and trade-log data provided. You cannot place, "
                "modify, or size trades — for control, tell the user the "
                "exact command (pause, resume, dry on/off, set budget N, "
                "set cap N, stop). Never give investment advice; you may "
                "explain what the agent did and why (per its logs). Be "
                "concise and plain-spoken."),
            output_config={"effort": "low"},
            messages=[{"role": "user", "content":
                       f"STATUS:\n{json.dumps(status_fields, indent=1)}\n\n"
                       f"RECENT TRADE LOG:\n{json.dumps(trades, indent=1)}\n\n"
                       f"QUESTION: {question}"}],
        )
        if response.stop_reason == "refusal":
            return "I can't help with that one."
        return next((b.text for b in response.content if b.type == "text"),
                    "(no answer)")
    except Exception as exc:
        return f"Q&A failed: {str(exc)[:150]}"


def start_dashboard(get_status, get_trades, apply_command, cfg_getter):
    """Serve the combined app (dashboard + settings) from the running agent."""
    return start_app(get_status, get_trades, apply_command, cfg_getter)
