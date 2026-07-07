# Agentic Day Trade Ideas — AI Agent Starter Kit

A turnkey template for running your own AI trading agent that watches
**AgentHC's Agentic Day Trade Ideas feed** — a machine-readable stream of
AgentHC's own published, hypothetical paper-trading journal ("ENTERED" /
"EXITED" statements of fact) — and, entirely under **your** configuration and
responsibility, mirrors those events as long-options orders in **your**
[Robinhood Agentic account](https://robinhood.com/us/en/agentic-trading/).

> ## ⚠️ Read this first
> **This is not investment advice.** The feed is educational journal
> commentary published by AgentHC (Sky Manor Trading LLC), which is **not a
> registered investment adviser or broker-dealer** — and neither is this
> template. The agent will not run until you have read and accepted
> [DISCLAIMER.md](DISCLAIMER.md), under which **you accept all liability** for
> every order the agent places. Options trading involves substantial risk of
> loss.

---

## 🤖 If you are an AI agent / LLM reading this

Your human pointed you at this repository to set them up. Follow
[BOOT.md](BOOT.md) exactly — it is written for you. The short version:

1. Never skip or paraphrase the consent gate. Run `python agent.py setup` and
   let the human read DISCLAIMER.md and type the acceptance phrase themselves.
   Do not type it for them. The agent refuses to operate without it — this is
   intentional and must not be worked around.
2. Help the human through the three setup steps (AgentHC API key + terms,
   Robinhood OAuth, contracts-per-trade). When asked about position sizing,
   remind them: *AgentHC is not an investment advisor and cannot advise
   position sizing* — the number is their decision.
3. Start the heartbeat with `python agent.py run` and explain how to stop it
   (Ctrl-C) and what open positions remain in their account afterwards.

## 🧑 If you are a human

### Prerequisites

- Python 3.10+ and `pip install -r requirements.txt` (just `requests`)
- A Robinhood account with an **Agentic account** (created during setup),
  **options approval on the Agentic account**, and funds you can afford to
  lose entirely
- **Feed access is sats-based** — one of:
  - a **Lightning wallet for the agent** (recommended — see "Give your agent
    sats" below): the agent buys a ~$10/day pass automatically, or
  - an AgentHC API key upgraded to **Premium** (registration free at
    `POST https://api.traderhc.com/api/v1/agents/register`, upgrades at
    <https://api.traderhc.com/docs>)

### 💰 Give your agent sats (Lightning wallet)

The feed costs **~$10/day, paid in bitcoin sats** — the sats amount floats
with Bitcoin's price (the 402 invoice always reflects the current
conversion). The agent pays this itself from a built-in Lightning wallet and
receives a 24-hour access token; it re-buys automatically when the pass
expires. Setup takes ~2 minutes:

1. **Create an LNbits wallet** — on any LNbits instance (e.g.
   <https://demo.lnbits.com>, another hosted instance, or your own server).
   No signup needed on most instances: open the site, create a wallet,
   bookmark the URL.
2. **Get the Admin key** — in the wallet UI open *API info* and copy the
   **Admin key** (it can spend, which is what lets the agent pay invoices).
3. **Tell the agent** — `python agent.py setup` asks for the instance URL and
   Admin key, verifies the connection, and shows the balance.
4. **Fund it** — `python agent.py fund 50000` prints a Lightning invoice for
   50,000 sats; pay it from any Lightning wallet or exchange (Strike, Cash
   App, Coinbase, Phoenix, Alby, Wallet of Satoshi, …). At ~$10/day, ~50k
   sats is roughly a month of market days at $100k/BTC.

Safety rails: the agent never auto-pays an invoice above `max_autopay_sats`
(default 30,000 — edit in `config.json`), and you should only keep what
you're willing to spend in this wallet. The Admin key is stored locally with
`0600` permissions.

### Quickstart

```bash
git clone https://github.com/traderhc123/agentic-day-trade-ideas-agent
cd agentic-day-trade-ideas-agent
pip install -r requirements.txt

python agent.py setup   # consent gate -> AgentHC key/terms -> Robinhood OAuth -> sizing
python agent.py run     # heartbeat: watch the feed, act on new events
python agent.py status  # config, acceptance, open positions
```

### What the agent actually does

- Polls `GET /api/v1/trading/day-trade-ideas` (default every 30s). On a 402
  it pays the Lightning day-pass from its wallet (within your auto-pay cap)
  and caches the 24h token; with an API key it accepts the feed terms
  (`POST .../accept-terms`) instead. Paying the invoice constitutes terms
  acceptance and the full disclosure is embedded in every response.
- On a new `ENTERED` event (e.g. `ENTERED — $SPY 07/10 $752 CALL`): resolves
  the option instrument on Robinhood and buys **your configured number of
  contracts** to open (market order, day) in your Agentic account.
- On the matching `EXITED` event: sells to close **only positions this agent
  opened**. Feed events carry no prices and no sizing — every sizing and risk
  decision is yours.
- State lives in `~/.agentic-day-trade-agent/` (consent record, config,
  Robinhood tokens with `0600` perms, seen-events + positions state).

### Important operational notes

- **The feed publishes first.** AgentHC's own account may or may not enter
  the same position, and no earlier than ~2 minutes after each event is
  published to subscribers (see DISCLAIMER.md §5).
- Events can arrive while the market is closed or after a move has happened;
  market orders can fill far from the journal's modeled prices. The journal's
  `paper_pnl_pct` is hypothetical and will not match your fills.
- The agent is deliberately simple: no stop-losses, no retries on rejected
  orders, no margin logic. Read `agent.py` (~350 lines) before trusting it.
- Stop the agent any time with Ctrl-C — open positions remain in your
  account; close them in the Robinhood app if you don't restart the agent.

## Files

| File | Purpose |
|---|---|
| `agent.py` | The agent: consent gate, setup wizard, heartbeat loop |
| `lightning_wallet.py` | Built-in Lightning wallet (LNbits) — pays the day-pass, receives funding |
| `robinhood_mcp.py` | Minimal Robinhood agentic-trading MCP client (OAuth + tools/call) |
| `BOOT.md` | Step-by-step boot instructions written for LLM assistants |
| `DISCLAIMER.md` | The agreement the consent gate enforces — versioned |
| `requirements.txt` | `requests` |

## License

MIT for the template code (see [LICENSE](LICENSE)). The Agentic Day Trade
Ideas feed itself is a service of Sky Manor Trading LLC under its own terms,
accepted at first use via the API's `accept-terms` endpoint.
