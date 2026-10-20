# Getting started (for non-technical users)

You don't need to be a programmer to run this agent. This guide assumes
nothing. Read [DISCLAIMER.md](DISCLAIMER.md) first — real money, your
responsibility, not investment advice.

## The easiest path: let an AI set you up

If you use **Claude** (or ChatGPT or any AI assistant that can run commands
on your computer — e.g. the Claude desktop app with Claude Code):

> Paste this to your AI assistant (Claude Cowork, the Claude desktop app,
> Claude Code, …):
> **"Set me up with https://github.com/traderhc123/agentic-trader — follow
> its BOOT.md"**
>
> It installs the agent on your computer, fills in everything you tell it,
> and checks the result. You do four things yourself: accept the agreement,
> log in to your broker, pay for the feed from your own wallet, and choose
> your trade size.

The assistant will install everything and walk you through setup step by
step. Two things it will (correctly) make YOU do yourself: read and accept
the agreement, and log into Robinhood in your own browser. That's by design.

> **Does it run "inside" my Claude subscription?** No — Claude helps you set
> it up and can start it for you, but the agent itself is a small program
> that runs on a computer you choose (below). It keeps running after you
> close Claude, and it doesn't consume your Claude subscription. **No AI key
> is needed to trade** — limits and rules are plain settings. (The optional
> AI step — help chat, dashboard Q&A and plain-English rules — uses an **AI
> API key** from any provider —
> Claude, OpenAI, Grok, Gemini, OpenRouter, or a local model. Claude Max and
> Team plans include monthly API credits you can link to a Claude Console
> organization; otherwise keys are billed pay-as-you-go by the provider.
> Signing in with a chat account doesn't connect it — create an API key.)

## Where should it run?

The agent must be **awake during US market hours** (9:30 AM–4:00 PM Eastern,
Mon–Fri). It sleeps itself outside those hours. Pick one:

| Option | Good for | Cost |
|---|---|---|
| **Your everyday computer** | Trying it out (start here, in dry-run mode) | Free |
| **An old computer / Mac mini / Raspberry Pi at home** | Set-and-forget, no monthly fee | Free |
| **A tiny cloud server (VPS)** | Most reliable — always on, survives power cuts | ~$5/month |

**Windows PCs and Macs:** fully supported. While the agent runs it keeps the
computer from going to sleep by itself (the screen can still turn off) —
"Keep this computer awake" in the Safety step, on by default; say
`keep awake off` in the dashboard to turn it off. Nothing in your power
settings is changed, and it stops when the agent stops.

**Laptop caveat:** closing the lid (or choosing Sleep) still sleeps the
computer, and the agent pauses. It
catches up when it wakes (and refuses to act on entries older than 5
minutes — a safety feature), but exits can be delayed. Fine for dry-run
trials; not ideal for live money.

**Keeping it (and its dashboard) up 24/7** — works the same on Windows
and Mac:

| What happens | What the agent does |
|---|---|
| Wi-Fi / internet drops | The dashboard keeps working (it runs on your computer, at `http://127.0.0.1:8721/dash`). Trading pauses and picks up again by itself when the connection is back. |
| The agent crashes | Restarts by itself (15 s, backing off to 10 min if it keeps crashing). |
| The agent freezes (e.g. a network call that never returns after Wi-Fi drops or the computer wakes up) | Detected after 20 min of silence, then restarted. |
| The dashboard stops answering but the agent keeps going | The agent restarts itself to bring the dashboard back. |
| You restart / shut down the computer | With **start at login** on (`autostart on`, or the checkbox on the last setup step), it starts when you log in and **opens the dashboard by itself, already signed in**. Turn just the opening off with `open at login off`. |
| You bookmark the dashboard | `http://127.0.0.1:8721/dash` stays signed in for a year in that browser. Signed out? Run `python agent.py link`. |

What no setting can fix on a personal computer: nothing runs until someone
logs in after a restart (a disk-encrypted Mac or PC waits at the password
screen), a dead battery or closed lid stops it, and the dashboard is only
reachable from that computer. For truly always-on, use **Option B** below.

**How much terminal is involved, really?** ONE pasted line, once. The
installer handles everything (it even installs Python itself if your
computer doesn't have it — no git needed either), opens the setup wizard in
your browser, and the wizard's last step has a **"Start my agent now"**
button. Every decision and all day-to-day use happens in your browser.

### Option A — your computer (10 minutes, start here)

1. Open Terminal (Mac: Cmd-Space, type "Terminal") and paste:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
   ```
   **Windows:** press the Windows key, type "PowerShell", open it, and paste:
   ```powershell
   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.ps1 | iex"
   ```
   That is the only command. The installer sets everything up (including a
   private Python if needed) and **the setup wizard opens in your browser
   automatically**.
2. In the wizard: read and accept the agreement, pick your signal source,
   connect your broker (Robinhood bounces you there and straight back;
   Alpaca and moomoo both have zero-dollar paper modes), choose a dollar
   budget per trade, and set the safety rails. It defaults to **dry-run
   mode** — no real orders — so you can watch it for a few days risk-free.
3. On the final step click **"Start my agent now"** — the same page becomes
   your agent's live dashboard: status, every action it's taken, and a chat
   box where you can say `pause`, `resume`, `set budget 500`, `dry off`, or
   just ask what it's been doing. Bookmark **http://127.0.0.1:8721** — it's
   your agent's home from now on.

   **Closing the window is fine.** The agent runs in the background, so you
   can close PowerShell / Terminal and it keeps going (it even restarts
   itself if it crashes). Leave **"Also start it automatically when I log in"**
   ticked and it comes back by itself after a restart or reboot. To stop it:
   type `stop` in the dashboard, or run `python agent.py stop`. Only one copy
   ever runs — opening it again just takes you to the dashboard.

   **New computer?** Press **Move computer** on the dashboard: it saves one
   passphrase-locked file with your whole setup and stops the agent on the
   old computer. On the new one, run the same installer and pick **"Moving
   from another computer?"** on the first setup page. No re-setup needed.

   The dashboard is login-protected: the
   browser that opened it stays logged in; anywhere else (another browser, a
   remote server), run `python agent.py link` and open the link it prints.
   On a remote server, tunnel first: `ssh -L 8721:127.0.0.1:8721 user@yourserver`.

### Option B — a $5 VPS (always-on, ~30 minutes)

1. Create the smallest server at DigitalOcean, Hetzner, or Vultr (choose
   "Ubuntu"). They all have point-and-click guides.
2. Connect to it (each provider has a "Console" button in the browser — no
   extra software needed), then run the same three commands from Option A.
   **Tip:** you can also just give your AI assistant the server's address
   and let it do this part over SSH.
3. Make it survive reboots — either:
   - **tmux (simplest):** `sudo apt install tmux`, run `tmux`, start the
     agent inside it, press `Ctrl-b` then `d` to detach. It keeps running.
     `tmux attach` brings it back.
   - **systemd (proper):** copy `deploy/agentic-trader.service` as described
     in the comments at the top of that file. Then it auto-starts on boot
     and restarts if it crashes.

### Option C — Docker (if you already use Docker)

```bash
docker build -t agentic-trader .
docker run -it -v agentic-trader-data:/data agentic-trader python agent.py setup
docker run -d --restart unless-stopped -v agentic-trader-data:/data agentic-trader
```

## The safety rails are on by default

- **Dry-run mode** is the setup default: the agent logs and notifies what it
  *would* do, places nothing. Watch it for a few days, then set
  `"dry_run": false` in `~/.agentic-trader/config.json` to go live.
- **Daily entry cap** (default 5) and **1-contract default sizing** bound the
  worst day; no trade is ever larger than `max_contracts_per_trade` (100).
- **Max daily loss** (optional): set it in the wizard's Safety step or say
  `set max loss 300` in the dashboard — after an estimated $300 realized loss
  in a day, no new entries until tomorrow (exits still run).
- **Prepaid feed access**: for the AgentHC feed, buy 1 (try it), 5, 20 or 60
  signal days in the wizard with one Lightning payment from the wallet you
  already use —
  the agent holds no money. A day is only used when your agent receives a
  trade signal (entry or exit) that day; quiet days are free. You're notified
  when 2 days are left; buying more carries unused days over. A size can be
  briefly off sale while the server's Lightning node refills capacity after a
  large purchase, and big passes (≈$200/$600) may exceed some wallets'
  Lightning send limits.
- **Day-pass spend cap** (only if you chose the Advanced wallet auto-pay): at
  most one day-pass's worth of sats per day (`max_autopay_sats_per_day`) — a
  misbehaving paywall can't drain the wallet.
- **Main pick only by default**: the AgentHC feed follows the single daily
  high-conviction pick unless you opt into "other trades" (the wider journal,
  several entries a day) during setup — or later via
  `"include_other_trades": true` in `~/.agentic-trader/config.json`. The daily
  cap and per-trade budget apply either way.
- **Three ways to size, per book**: a fixed dollar amount, a fixed number
  of contracts, or a percent of your account value per trade. The main book
  (the daily pick) and the shadow book ("other trades") each pick their own —
  set it in the wizard's Sizing step, with chat commands (`set budget 500`,
  `set contracts 2`, `set percent 2`, `set other percent 1`,
  `other sizing same`), or in config.json (`"sizing_mode"`:
  `"budget"`/`"contracts"`/`"percent"` with `"budget_per_trade_usd"` /
  `"contracts_per_trade"` / `"percent_per_trade"`; the same keys prefixed
  `other_` for the shadow book — unset = same as the main book).
  Percent mode re-reads your account value from the broker at each entry and
  turns it into a dollar budget for that trade; if the value can't be read,
  the entry is skipped rather than guessed. `max_contracts_per_trade` caps
  every mode.
- **No weekend spend**: the recurring day-pass never auto-buys on Saturday or
  Sunday — the feed only publishes on market days, so a weekend pass would be
  wasted sats.
- **Notifications**: set up Discord/ntfy/Telegram during setup — you get a
  message on every action, veto, and error, plus a daily digest after the
  close. Never run it silently.
- **Your rules, enforced — no AI needed**: in the Safety step pick which
  tickers to trade (or never trade), calls only / puts only, a minimum
  strike, no same-day (0DTE) contracts, and "stop for the day after N losses
  in a row". Change them any time in the dashboard chat (say `rules`).
- **Plain-English rules (optional, needs an AI)**: connect an AI in the
  optional AI step and write rules in your own words; the agent checks every
  entry against them before acting.

Full details: [SECURITY.md](SECURITY.md).

## Something not working?

```bash
./.venv/bin/python agent.py doctor
```

(Windows: `.\.venv\Scripts\python.exe agent.py doctor` — in every command in
this guide, `./.venv/bin/python` becomes `.\.venv\Scripts\python.exe`.)

checks everything (consent, source, wallet + balance, feed reachability,
broker connection, notifications) and prints the exact command that fixes
each ✗. Run it any time the agent isn't doing what you expect.

**Easiest: the dashboard's `? help` button.** "Check my connection" names
whatever is blocking the agent; "Copy support report" copies one block of
text (no keys or tokens in it) to paste to whoever helps you.

**"SSL handshake failed" / can't connect to AgentHC?** Run
`./.venv/bin/python agent.py netcheck`. It works out what on your computer or
network is blocking the connection (antivirus HTTPS scanning, a VPN, a
router or DNS filter) and tells you how to fix it. The running agent does
the same automatically and sends you the answer as a notification.

**moomoo users**: the adapter talks to moomoo's local **OpenD** gateway —
download it from moomoo.com → API, log in once, keep it running, and
`.venv/bin/pip install moomoo-api` (Windows: `.venv\Scripts\pip.exe install moomoo-api`). `doctor` will tell you if either piece
is missing. Paper (SIMULATE) is the default; live orders additionally need
your trade password and options approval.

## See what it would have done (dry-run, free)

In dry-run with the AgentHC source, the dashboard shows **Would have taken ·
past 7 days**: AgentHC's closed paper trades from the past week, each checked
against your rules (tickers, calls/puts, min strike, no 0DTE, loss stop).
You'll see "would take" or "skip: why", plus AgentHC's paper P&L. No pass is
needed and nothing is ordered. Trades show up after they close (not live),
and your AI policy, daily caps and budget aren't applied. Turn it off with
`preview off`.

## Checklist before going live (turning dry-run off)

- [ ] You read DISCLAIMER.md and accepted it yourself
- [ ] Robinhood **Agentic account** exists, has **options approval**, and is
      funded with only what you can afford to lose
- [ ] If using the AgentHC feed: the agent's wallet holds sats (~50k ≈ a month) — the wizard creates the wallet FOR the agent; you just pay one invoice from any Lightning app
- [ ] Notifications tested (you got the test message)
- [ ] You watched at least a few days of dry-run activity and it did what
      you expected
- [ ] You know how to stop it: `stop` in the dashboard or `python agent.py
      stop` (Ctrl-C if you ran it in the foreground; `docker stop` /
      `systemctl stop agentic-trader` on a server) — open positions stay in
      your account; close them in the Robinhood app if needed
- [ ] You decided whether it should start at login (`python agent.py
      autostart on|off`)
