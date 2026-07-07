# agentic-trader

A consent-gated, open-source **autonomous trading agent** for Robinhood's new
[agentic trading](https://robinhood.com/us/en/agentic-trading/) platform.
Pick a **signal source**, connect **your own Robinhood Agentic account**, set
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

1. Never skip or paraphrase the consent gate. Run `python agent.py setup` and
   let the human read DISCLAIMER.md and type the acceptance phrase
   themselves. Do not type it for them, ever.
2. Help them pick a signal source and walk through broker auth (Robinhood
   OAuth). When asked about position sizing, state that no party here is an
   investment advisor and cannot advise sizing — the number is their decision.
3. Start the heartbeat with `python agent.py run` and explain how to stop it
   (Ctrl-C) and that open positions remain in their account afterwards.

**New to this / not technical? Start with [GETTING_STARTED.md](GETTING_STARTED.md)** — plain-English instructions for running it on your computer, a home machine, or a $5 cloud server, including how to have Claude set it all up for you.

## What you get

- **Consent-gated**: refuses to run until you accept the agreement yourself
- **Dry-run by default**: watch what it *would* do for days before real orders
- **Safety rails**: daily entry cap, event validation, staleness guard, wallet auto-pay cap ([SECURITY.md](SECURITY.md))
- **LLM policy brain (optional)**: write plain-English rules in `policy.md` — the agent checks every entry against *your* policy with *your* Anthropic API key (veto-only; fails safe to skip)
- **Notifications**: Discord / ntfy / Telegram message on every action, veto, and error + a daily digest after the close
- **Market-hours aware**: polls fast 9:25–16:15 ET, sleeps slow overnight/weekends
- **Always-on ready**: one-line installer, Dockerfile, hardened systemd unit

## 🧑 Quickstart

One-liner (clones to `~/agentic-trader`, installs into a venv — it never
runs the agent or accepts anything for you):

```bash
curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/traderhc123/agentic-trader
cd agentic-trader
pip install -r requirements.txt

python agent.py setup   # consent gate -> signal source -> Robinhood OAuth -> sizing
python agent.py run     # heartbeat: watch the source, act on new events
python agent.py status  # config, wallet balance, day-pass, open positions
```

**Prerequisites:** Python 3.10+; a Robinhood account with an **Agentic
account** (created during setup), **options approval on the Agentic
account**, and funds you can afford to lose entirely. The `agenthc` source
additionally needs sats (below) or a Premium API key.

## 💰 Give your agent sats (for sats-priced sources)

The AgentHC feed costs **~$10/day, paid in bitcoin sats** — the sats amount
floats with Bitcoin's price. The agent pays this itself from a built-in
Lightning wallet and receives a 24-hour access token; it re-buys
automatically when the pass expires. Setup takes ~2 minutes:

1. **Create an LNbits wallet** — on any LNbits instance (e.g.
   <https://demo.lnbits.com>, another hosted instance, or your own server).
2. **Get the Admin key** — wallet UI → *API info* → **Admin key** (it can
   spend, which is what lets the agent pay invoices).
3. **Tell the agent** — `python agent.py setup` asks for the instance URL and
   Admin key, verifies the connection, and shows the balance.
4. **Fund it** — `python agent.py fund 50000` prints a Lightning invoice for
   50,000 sats; pay it from any Lightning wallet or exchange (Strike, Cash
   App, Coinbase, Phoenix, Alby, Wallet of Satoshi, …). ~50k sats ≈ a month
   of market days at $100k/BTC.

Safety rails: the agent never auto-pays an invoice above `max_autopay_sats`
(default 30,000 — edit in `config.json`); keep only what you're willing to
spend in this wallet. The Admin key is stored locally with `0600` perms.

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
| `brokers/` | Broker adapters (Robinhood agentic MCP) — PRs welcome |
| `lightning_wallet.py` | Built-in Lightning wallet (LNbits) for sats-priced sources |
| `install.sh` | One-line installer (clone + venv + deps; never auto-consents) |
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

## License

MIT for the template (see [LICENSE](LICENSE)). Optional third-party feeds
(including AgentHC's) are separate services under their own terms.
