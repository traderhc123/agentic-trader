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
dry-run; say `dry off` when you're ready. (Terminal: `agent.py export FILE` /
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

After setup, `./.venv/bin/python agent.py` again just runs the agent (dashboard
at http://127.0.0.1:8721). Prefer terminal prompts to the browser? Use
`agent.py setup`. Other commands: `status`, `fund <sats>`.
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
pays an invoice above `max_autopay_sats` (30,000) or more than
`max_autopay_sats_per_day` in a day; the Admin key is stored with `0600`
perms.

A Premium AgentHC API key also works (*Advanced* in the wizard).

## How it works

- Every `poll_seconds` (default 30) the agent polls the configured source for
  events in a normalized shape: `ENTERED` / `EXITED` + ticker, expiry,
  strike, call/put (see `sources/__init__.py` for the contract).
- `ENTERED` → resolves the option on Robinhood and buys **your configured
  number of contracts** to open (market, day) in your Agentic account.
- `EXITED` → sells to close **only positions this agent opened**.
- State lives in `~/.agentic-trader/` (consent record, config, broker tokens
  `0600`, seen-events + positions).
- Deliberately simple by design: no stop-losses, no retries on rejected
  orders, no margin logic. Read the code (~700 lines total) before trusting
  it. Human oversight required — don't run unattended for long periods.

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
