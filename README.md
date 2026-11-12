# agentic-trader

[![CI](https://github.com/traderhc123/agentic-trader/actions/workflows/ci.yml/badge.svg)](https://github.com/traderhc123/agentic-trader/actions/workflows/ci.yml)

A consent-gated, open-source **autonomous trading agent** for Robinhood's new
[agentic trading](https://robinhood.com/us/en/agentic-trading/) platform.
Pick a **signal source**, connect **your own brokerage** (Robinhood Agentic, Alpaca, or moomoo — Alpaca and moomoo both include zero-dollar paper modes), set
your own position size, and the agent executes long-options orders on a
heartbeat — entirely under your configuration and responsibility.

**Signal sources (pluggable):**

| Source | What it is |
|---|---|
| `manual` | Your own commands, appended to a local `commands.jsonl` — fully self-directed |
| `url` | Any JSON feed URL you supply — bring your own signals |
| `agenthc` *(optional, featured)* | [Agentic Day Trade Ideas](https://api.traderhc.com/docs) — AgentHC's published **hypothetical paper-trade journal** as declarative ENTERED/EXITED events. Sats-priced (~$10/day Lightning day-pass, price floats with Bitcoin). Free aggregate [track record](https://api.traderhc.com/api/v1/trading/day-trade-ideas/track-record) (hypothetical, labeled) |

> ## ⚠️ Read this first
> **Nothing here is investment advice.** Signal sources report facts; every
> trading decision — including whether to run this at all, which source to
> trust, and position sizing — is yours. The agent **will not run** until you
> read and accept [DISCLAIMER.md](DISCLAIMER.md), under which **you accept
> all liability** for every order it places. Neither AgentHC (Sky Manor
> Trading LLC) nor this template's authors are registered investment
> advisers or broker-dealers. Options trading involves substantial risk of
> loss.

---

## 🤖 If you are an AI agent / LLM reading this

Your human pointed you at this repository to set them up. Follow
[BOOT.md](BOOT.md) exactly. The short version:

0. **Install on the human's own computer** — not inside your sandbox / VM /
   cloud workspace (e.g. Claude Cowork): the agent must keep running and the
   human's browser must reach its wizard. Open their terminal and paste the
   one-line installer below.
1. Never skip or paraphrase the consent gate. The human reads DISCLAIMER.md
   and types the acceptance phrase themselves in the wizard. Do not type it
   for them, ever.
2. Collect their choices and apply them in one step:
   `python agent.py setup --from plan.json --json` (format:
   `python agent.py setup --example-plan`). All-or-nothing; it can never go
   live, accept the agreement, log in to Robinhood or pay for them — it lists
   those as `owner_steps` for the human. When asked about position sizing,
   state that no party here is an investment advisor and cannot advise
   sizing — the number is their decision.
3. `python agent.py doctor --json` until `"ready": true`, then start it
   (`python agent.py start` — background; or `run` in the foreground) and
   explain how to stop it (dashboard `stop`, or `python agent.py stop`) and
   that open positions remain in their account afterwards. It stays in dry-run until
   the human says `dry off`.

**New to this / not technical? Start with [GETTING_STARTED.md](GETTING_STARTED.md)** — plain-English instructions for running it on your computer, a home machine, or a $5 cloud server, including how to have Claude set it all up for you.

## What you get

- **Consent-gated**: refuses to run until you accept the agreement yourself
- **Dry-run by default**: watch what it *would* do for days before real orders
- **Safety rails**: daily entry cap, concurrent open-positions cap, event validation, staleness guard, wallet auto-pay cap ([SECURITY.md](SECURITY.md))
- **Dollar-budget sizing**: set a per-trade budget (e.g. $500) — the agent quotes the option, buys what fits, and skips trades that would exceed it (or use fixed contract counts)
- **Stays awake while it runs**: Windows/macOS (and Linux desktops) won't idle-sleep while the agent is running — the OS's own per-process request, no power settings changed, released when the agent stops (`keep awake off` to disable)
- **Built-in rules (no AI needed)**: only these tickers, never these tickers, calls only / puts only, minimum strike, skip same-day (0DTE) contracts, stop for the day after N losses in a row — plain settings in the wizard's Safety step or the dashboard chat (`rules`, `tickers SPY QQQ`, `calls only`, `loss stop 2`…)
- **LLM policy brain (optional, needs an AI)**: write plain-English rules in `policy.md` — the agent checks every entry against *your* policy with *your* AI model — any provider: Claude, OpenAI, Grok, Gemini, OpenRouter, local Ollama, … (veto-only; any model error or unclear answer fails safe to skip)
- **Notifications**: Discord / ntfy / Telegram message on every action, veto, and error + a daily digest after the close
- **Market-hours aware**: polls fast 9:25–16:15 ET, sleeps slow overnight/weekends
- **Self-modifying (with your approval)**: `self-edit on`, then `code: add an RSS source` — the agent rewrites its own source, shows you the diff in the dashboard, and one click applies it with backup + compile-check + auto-rollback + self-restart. A **foundation layer** (consent gate, safety rails, terms, the self-edit governor itself) is immutable to the agent
- **Web UI, self-served**: one paste on a desktop (or a few terminal lines on a server), then it's browser-only — a **setup wizard** (`setup --web`) with one-click Robinhood OAuth, and a **dashboard** your own agent serves at `127.0.0.1:8721` with live status, trade log, and a chat box (`pause`, `set budget 500`, `dry off`, or ask it anything about its trades). Localhost-only; no central server ever sees your keys. (Zero-terminal option: have Claude run the three lines for you — see GETTING_STARTED.md)
- **Feels like an app**: `agent.py app` opens the dashboard as a chromeless desktop window (falls back to a normal browser tab). No packaging, no signing — the localhost UI in an app-mode window
- **Always-on ready**: one-line installer, Dockerfile, hardened systemd unit

## 🧑 Quickstart

One-liner — on a Mac, Linux or Windows desktop this is the whole setup: it
installs (bootstrapping Python if needed) and **opens the browser setup wizard
automatically**. You still read and accept the agreement yourself in the page.

Mac / Linux (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.ps1 | iex"
```

(Headless server? It prints the tunnel command instead. Opt out of auto-launch
with `AGENTIC_TRADER_NO_LAUNCH=1`.)

That single line is the whole thing on a desktop. The wizard's last step,
**"Start my agent now"**, starts the agent in the background — you can close
the terminal and it keeps running (and restarts itself if it ever crashes).
Tick "start automatically when I log in" there and it comes back after a
reboot too. From a terminal:

```bash
cd ~/agentic-trader
./.venv/bin/python agent.py start          # background + dashboard at http://127.0.0.1:8721
./.venv/bin/python agent.py stop           # stop it
./.venv/bin/python agent.py autostart on   # start at every login (off to undo)
./.venv/bin/python agent.py status         # running? start at login?
./.venv/bin/python agent.py run            # foreground instead (Ctrl-C stops)
```

**Moving to another computer:** press **Move computer** on the dashboard. It
packs your whole setup (settings, broker login, prepaid pass, wallet, open
positions, history, rules) into one file locked with a passphrase you pick,
then stops the agent on the old computer so two machines never trade the same
account. On the new computer run the installer and choose **"Moving from
another computer?"** on the first setup page. The setup starts there in
dry-run (no new trades); say `dry off` when you're ready. Real positions you
moved over are still sold on their exit signal, so make sure the old computer
is off first. (Terminal: `agent.py export FILE` /
`agent.py import FILE`.)

Only one copy can run at a time — starting a second one (another window,
start-at-login, the wizard) just points you at the running one, so orders are
never placed twice. Background output goes to `~/.agentic-trader/agent-run.log`.
Servers: use the systemd unit in `deploy/` or Docker instead.

The setup wizard and the live dashboard are one app on **http://127.0.0.1:8721**
— `/` shows the dashboard once the agent is running, the setup stepper
otherwise, cross-linked both ways.

<details><summary>Manual install (no curl-pipe)</summary>

One command — clone, install, and launch. `agent.py` with no arguments does
the right thing: it opens the browser setup wizard if you haven't set up yet,
or runs the agent if you have. Nothing trades until you accept the agreement
in the wizard, so a single launch command is safe:

```bash
git clone https://github.com/traderhc123/agentic-trader && cd agentic-trader \
  && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt \
  && ./.venv/bin/python agent.py
```

After setup, `./.venv/bin/python agent.py` again starts the agent in the background (dashboard
at http://127.0.0.1:8721). Prefer terminal prompts to the browser? Use
`agent.py setup`. Other commands: `status`, `check-positions`, `fund <sats>`.
</details>

**Prerequisites:** Python 3.10+; a Robinhood account with an **Agentic
account** (created during setup), **options approval on the Agentic
account**, and funds you can afford to lose entirely. The `agenthc` source
additionally needs prepaid signal days (below) or a Premium API key.

## 💰 Paying for the AgentHC feed

The AgentHC feed costs **~$10 per signal day, paid in bitcoin sats** (the sats
amount floats with Bitcoin's price). Two ways to pay:

**Prepaid signal days (recommended).** In the setup wizard's *Signal source*
step, pick **1 (try it), 5, 20 or 60 signal days**. You get one Lightning invoice (QR
code) — pay it from the wallet you already use (Cash App, Strike, Phoenix,
Alby, …) and the page confirms by itself. The agent never holds money.
A size can be briefly off sale while the server's Lightning node refills
capacity after a large purchase (try a smaller pass or again in about an
hour), and the big passes (≈$200 / ≈$600) may exceed some wallets'
Lightning send limits.

A day is only used when your agent actually **receives a trade signal — an
entry or an exit — that day**; quiet days (no picks, or your agent was off)
are free, and several signals in one day count once. With "main pick only"
only main-pick signals use days; adding "other trades" uses days on those
too. When your days run out, exit signals keep coming for 3 more market days
so open positions can close. You're notified when 2 days are left; buying
more carries unused days over. A pass is valid for at most about twice its
length in market days.

**Advanced: daily auto-pay from a wallet the agent controls.** The agent
keeps an LNbits wallet and buys a 24-hour pass each market day (only when you
turn *Recurring day-pass* ON). Set it up under *Advanced* in the wizard, or
with `python agent.py setup` (instance URL + Admin key), then fund it with
`python agent.py fund 50000`. ⚠️ The one-click wallet lives on
demo.lnbits.com, a public test server that can be reset — keep only a few
days of passes there, or use your own LNbits instance. Safety rails: never
pays an invoice above `max_autopay_sats` (30,000 — the amount is read from
the invoice itself); buys at most one day-pass a day (more only if you set
`max_autopay_sats_per_day` yourself), and only on a market day, from 8:30 AM
ET until the close; a pass that is lost or refused is never paid for twice —
you get an alert instead; the Admin key is stored with `0600` perms.

A Premium AgentHC API key also works (*Advanced* in the wizard).

**Tips.** The dashboard's **Tip** button offers $100, $500 or $1,000, paid in
sats at the live Bitcoin price: AgentHC prices a Lightning invoice and you pay
it from your own wallet. The agent checks the invoice's exact sats, and AgentHC's
Bitcoin price against an independent one, before showing it (no independent
price, no invoice).
A tip is a thank-you and buys nothing; the agent's wallet never pays it,
whatever `max_autopay_sats` is set to.

## 📱 Phone app (remote for your own agent)

The Agentic Trader phone app (iPhone and Android) is a remote control for the
agent **you** run. It talks straight to your computer or VPS. There is no
server in between, and your broker login never leaves your machine.

1. Install [Tailscale](https://tailscale.com) (free) on the computer running
   the agent **and** on your phone, signed in to the same account.
2. Publish the agent's phone API on your private tailnet only (HTTPS, nothing
   on the internet):
   ```
   tailscale serve --bg --https=443 http://127.0.0.1:8722
   ```
3. With the agent running, run `python agent.py pair` and scan the QR code in
   the app. The code works once and expires in 10 minutes.

From the phone you can watch positions and activity, pause/resume, switch
practice/live, change limits, rules, alerts and signal settings, buy a day
pass and ask questions. Alerts arrive as push notifications. Wallet keys,
broker sign-in, AI keys, code changes and the consent stay on the computer.

Each phone gets its own token (stored here only as a SHA-256 hash).
`python agent.py devices` lists paired phones; `python agent.py devices revoke
<id>` cuts one off. Set `AGENT_REMOTE=0` to switch the phone API off entirely.

## How it works

- Every `poll_seconds` (default 30) the agent polls the configured source for
  events in a normalized shape: `ENTERED` / `EXITED` + ticker, expiry,
  strike, call/put (see `sources/__init__.py` for the contract).
- `ENTERED` → resolves the option on Robinhood and buys **your configured
  number of contracts** to open (market, day) in your Agentic account.
- `EXITED` → sells to close **only positions this agent opened**.
- **If you close a trade yourself** in your broker's app, the agent notices
  within a few minutes (Robinhood and Alpaca, plus moomoo once it has seen
  your positions there). It checks your account more than once before
  believing it, then stops tracking what you sold: one Activity row with
  the P&L when your sale price is known, one notification, the open slot
  freed, and the later exit signal is ignored. Sold only some? It keeps
  the rest and the exit signal sells just those. If your broker can't be
  read clearly, nothing changes and exits work as before. Check it any
  time with `python agent.py check-positions` (read-only); turn it off
  with `"position_sync": false` in config.json.
- **Expiry guard** (on by default): an option the agent opened that it still
  holds on its expiry day is sold from `expiry_guard_et` (default `"15:45"`
  ET) until the 4:00 PM close — a marketable limit at the bid −5%, retried
  every ~10 s — so a missed exit signal (computer asleep, agent offline)
  doesn't end in exercise or assignment. The feed's own exit signal still
  comes first. No bid (worthless) = no sell, just a notification. Anything
  still open at `expiry_alert_et` (default `"15:50"`) gets one urgent
  notification and a red dashboard banner; at startup the agent also warns
  about positions that expire today or expired while it was offline. Set
  either time to `"off"` in config.json, or say `expiry guard off` /
  `expiry guard 15:40` / `expiry alert off` in the dashboard. Only the
  agent's own positions are ever sold. Past the guard time it also won't
  open a new position in a contract that expires that day. Early-close days
  (1:00 PM: the day after Thanksgiving, and Dec 24 / Jul 3 when they fall
  Monday–Thursday) move both times 3 hours earlier, never before 09:30; add
  any other with `"early_close_days": ["YYYY-MM-DD"]`. The guard keeps
  running while the agent is paused, and in dry-run it still sells positions
  the agent bought with real orders (dry-run positions stay on paper).
- **No-signal sell** (on by default): a position is closed by its trade's
  exit signal, so when no exit signal can reach the agent any more (your
  signal pass ran out or isn't paid, AgentHC refused the day-pass, the feed
  can't be read) nothing would sell it. If that lasts 5 minutes during the
  session while a real position is open, the agent sends an urgent
  notification saying what it holds, why the signals stopped and when it
  will sell (a feed that comes and goes is not repeated every time: one more
  at most that day, when a later outage is still there 15 minutes before
  the sell time). If the signals are not back by `signal_loss_sell_et` (default
  `"15:30"` ET), it sells every position it opened (any expiry) the same
  way as the expiry guard (a marketable limit at the bid −5%, retried every
  ~10 s; no bid = no sell, a notification instead), always at least 15
  minutes after that notification, and opens nothing new that day. Signals
  that come back before then cancel it, and a short outage sells nothing
  (the next read still delivers the missed exit). It is not a price stop.
  Early-close days move it 3 hours earlier. It keeps running while the agent
  is paused, but not in a hold (the notification then says it won't sell).
  Change the time or switch it off in the /v2 dashboard (Rules & limits →
  No-signal sell time), in config.json, or say `no-signal sell off` /
  `no-signal sell 15:00` in the classic dashboard; off, the agent still
  warns you but never sells on its own.
- **Pause and dry-run, exactly**: **Pause** stops *new entries* only — exit
  signals, the expiry guard and the broker checks keep running for positions
  you already have, and the agent stays paused after a restart until you
  press Resume. (On the pay-per-day AgentHC feed: while a real position is
  open, a paused agent still reads the feed to get its exit signal, which
  uses a signal day. With no real position open it stops reading the feed,
  so the pause costs nothing; dry-run positions are not updated meanwhile.)
  **Dry-run** stops *new real trades* only — a position the agent already
  bought with a real order is still sold for real when its exit signal
  arrives; the dashboard tells you how many such positions are open before
  you switch.
- **Editing config.json by hand**: if a typo (a stray comma or quote) makes
  the file unreadable, the running agent keeps the settings it last read and
  refuses every settings change — the dashboard and the chat tell you so —
  until you fix the file. It never writes a new file over it, and `stop`
  still works. Only the setup wizard, with no agent running, starts a new
  file; it keeps the unreadable one next to it as
  `config.json.damaged-<date>`.
- **Exit remainders** (any day): when an exit sells only part of a position
  (a partial fill, or an order that died or was cancelled with contracts
  left), the agent re-sells the rest on the next loop — a marketable limit,
  sell-to-close, never more than its own ledger remainder and never more
  than the account holds — until it's gone or the market closes, and again
  from the next 9:30 ET open (NYSE holidays skipped; add other closures
  with `"market_holidays": ["YYYY-MM-DD"]`). If the account holds fewer than
  the ledger remainder, it sells only what its own filled orders account
  for. No sell into a zero bid. Without a holdings read it waits; with the
  holdings check off (`"position_sync": false`) it only alerts. You get one
  notification when it happens, one when it's cleared, and one if it's
  still open at 15:50 ET. On an expiry day the expiry guard takes over at
  its time.
- **Ledger check**: if the account holds more of a contract the agent bought
  than its ledger lists (an exit booked as sold that didn't fill), the agent
  re-adds them — only as many as its own filled orders in your broker's
  order history account for (bought minus everything sold since). Your own
  positions are never adopted; a broker whose order history can't be read
  gets an alert instead.
- State lives in `~/.agentic-trader/` (consent record, config, broker tokens
  `0600`, seen-events + positions).
- Deliberately simple by design: no stop-losses, no margin logic, and a
  rejected entry is never retried. An exit the broker refuses is retried
  each poll; an accepted exit is sent again only when the broker reports
  right away that it filled nothing and the agent can read your holdings.
  (On Robinhood, an order placed as a limit in the 9:30–9:35 window is
  re-sent as a market order for whatever hasn't filled once the window
  ends.) Read the code (~700 lines total) before trusting it. Human
  oversight required — don't run unattended for long periods.

## Repo layout

| Path | Purpose |
|---|---|
| `agent.py` | Orchestrator: consent gate, setup wizard, heartbeat |
| `sources/` | Signal source adapters (`manual`, `url`, `agenthc`) — PRs welcome |
| `brokers/` | Broker adapters: Robinhood agentic MCP, **Alpaca (paper/live)**, **moomoo (paper/live, via OpenD)** — more PRs welcome |
| `lightning_wallet.py` | Built-in Lightning wallet (LNbits) for sats-priced sources |
| `install.sh` / `install.ps1` | One-line installer for Mac/Linux / Windows (clone + venv + deps; never auto-consents) |
| `GETTING_STARTED.md` | Plain-English setup guide (local / home server / VPS / Docker) |
| `SECURITY.md` | Security model: built-in rails, blast radii, threat notes |
| `llm_policy.py` | Optional LLM policy brain (your rules, your API key, veto-only) |
| `notifications.py` | Discord / ntfy / Telegram notifications |
| `Dockerfile` + `deploy/` | Container + systemd unit for always-on operation |
| `BOOT.md` | Boot instructions written for LLM assistants |
| `DISCLAIMER.md` | The agreement the consent gate enforces — versioned |
| `SKILL.md` | OpenClaw/ClawHub skill definition |

## Adding a source or broker

A source is one file in `sources/` exposing `NAME`, `DESCRIPTION`,
`setup(cfg)`, and `poll(cfg, state, save_state)` returning events in the
documented contract. A broker exposes `setup(cfg)`, `client(cfg)`, and
`execute(client, cfg, event, state)`. Keep sources declarative (facts, not
recommendations) and keep all sizing/risk decisions with the user.

## Tests

```bash
pip install pytest && pytest
```

The suite covers the safety rails end-to-end with zero network access: event
validation, the staleness guard, the daily entry cap (including the
log-flooding bypass it now resists), policy-brain fail-safe direction, both
brokers' sizing/order builders, dry-run bookkeeping, the consent gate, and
the self-edit FOUNDATION lock with rollback. CI runs it on Python 3.10 and
3.12 for every push and PR. If you add a source or broker, please add tests
in the same style — mock the network, drive the real functions.

## License

MIT for the template (see [LICENSE](LICENSE)). Optional third-party feeds
(including AgentHC's) are separate services under their own terms.
