# BOOT.md — instructions for AI agents setting up a human

You are an AI assistant (Claude, ChatGPT, or any LLM with shell access) whose
human asked you to set up this trading agent. Follow these steps **in order**.
Do not improvise around the consent gate — it exists for legal reasons and the
software enforces it.

## Rules you must follow

1. **The human accepts the agreement, not you.** When `agent.py setup` prints
   DISCLAIMER.md and asks for the acceptance phrase, hand control to the human
   (or have them run that command in their own terminal). Never type
   `I AGREE AND ACCEPT ALL LIABILITY` on their behalf, never pre-create
   `~/.agentic-trader/acceptance.json`, and never summarize the
   disclaimer as a substitute for the human reading it.
2. **Position sizing is the human's decision.** When the setup wizard asks for
   contracts-per-trade, state verbatim: *"AgentHC is not an investment advisor
   and cannot advise position sizing."* You may explain mechanics (1 contract =
   100 shares of exposure; an option can go to zero) but do not recommend a
   number.
3. **Real money.** Before starting `run`, confirm the human understands the
   agent places real orders in their Robinhood Agentic account and how to stop
   it (Ctrl-C) — and that stopping leaves any open positions in their account.

## Boot sequence

### Step 0 — environment

```bash
python3 --version          # need 3.10+
pip install -r requirements.txt
```

### Step 1 — consent gate + setup wizard

```bash
python agent.py setup
```

This walks through, in order:

1. **Consent gate** — human reads DISCLAIMER.md, types the acceptance phrase.
   Acceptance (terms version + SHA-256 of the text + timestamp) is recorded at
   `~/.agentic-trader/acceptance.json`. If DISCLAIMER.md ever changes,
   the gate re-triggers.
1b. **Signal source choice** — `manual` (the human's own commands via
   `~/.agentic-trader/commands.jsonl`), `url` (their own JSON feed), or
   `agenthc` (the optional Agentic Day Trade Ideas journal feed). Explain the
   options neutrally; the choice is theirs. The AgentHC-specific steps below
   apply only when they pick `agenthc`.
2. **AgentHC feed access (sats-based)** — the wizard offers two paths:
   - **Lightning day-pass (default, recommended)**: the agent gets its own
     LNbits wallet and auto-buys a ~$10/day pass (price floats with Bitcoin's
     USD price). Help the human create an LNbits wallet (any instance, e.g.
     demo.lnbits.com — README "Give your agent sats" has the steps), paste the
     instance URL + wallet **Admin key**, then fund it: `python agent.py fund
     50000` prints a Lightning invoice payable from any wallet or exchange.
     Note the safety cap: the agent refuses to auto-pay invoices above
     `max_autopay_sats` (default 30,000). Paying a day-pass invoice
     constitutes acceptance of the feed terms (stated in the 402 response).
   - **Premium API key**: registers a free key
     (`POST https://api.traderhc.com/api/v1/agents/register`), then upgrade to
     Premium with sats (`https://api.traderhc.com/docs`,
     `POST /api/v1/agents/upgrade`) and accept the feed terms
     (`POST /api/v1/trading/day-trade-ideas/accept-terms`).
3. **Robinhood Agentic account** — prints an OAuth URL. The human opens it in
   their browser (logged into Robinhood), approves, lands on a dead
   `http://127.0.0.1:8721/callback?...` page (expected), and pastes that full
   URL back. **Authorization codes expire in minutes** — have them paste it
   promptly; if it fails with `invalid_grant`, rerun setup for a fresh URL.
   The wizard then lists their accounts, selects the `agentic_allowed=true`
   account, and warns if options are not enabled on it (they must apply via
   the printed `applink.robinhood.com/upgrade_options` link — orders are
   rejected until approved). The Agentic account must also be **funded**.
4. **Sizing** — contracts per trade (see rule 2).

### Step 2 — verify before going live

```bash
python agent.py status
```

Confirm: consent accepted, AgentHC key set, Robinhood account selected,
contracts/trade as the human intended. Also confirm with the human that their
Agentic account has options approval and cash in it.

### Step 2.5 — safety rails (setup step 4)

The wizard asks about dry-run (default ON — keep it on for new users and say
why), a daily entry cap, notifications (strongly encourage configuring at
least one channel), and the optional LLM policy brain (plain-English rules in
`~/.agentic-trader/policy.md`, checked with the human's own Anthropic API
key; veto-only). Help the human write their policy rules if they enable it —
the rules are theirs; you may translate their intent into clear bullet
points but never invent rules they didn't express.

### Step 3 — heartbeat

```bash
python agent.py run
```

Polls the feed every 30s. On new `ENTERED` events it buys the configured
contracts to open; on matching `EXITED` events it sells to close positions it
opened. Logs every action. Runs until Ctrl-C.

For long-running operation, walk the human through GETTING_STARTED.md's
options (tmux, the `deploy/agentic-trader.service` systemd unit, or Docker) —
and remind them autonomous trading software requires human oversight; it
should not run unwatched for long periods. Keep dry-run ON until they have
watched several days of activity and explicitly ask to go live.

## Troubleshooting map

| Symptom | Cause / fix |
|---|---|
| `403 feed_not_live` | Feed isn't publishing yet — agent idles and keeps polling; normal |
| `402` loops / "exceeds your auto-pay cap" | Day-pass price above `max_autopay_sats` — raise it in config.json if the price is legitimate |
| `WalletError: payment failed` | Wallet balance too low — `python agent.py fund <sats>` and pay the invoice |
| `403 terms_acceptance_required` | Agent auto-accepts and retries; if it loops, run setup again |
| `403 tier_required` | AgentHC key is FREE tier — upgrade to Premium (`/docs`) or switch to the day-pass |
| `401 Invalid API key` | Key revoked/typo — re-run setup with a valid key |
| Robinhood order rejected, options-level alert | Enable options on the Agentic account (upgrade link in setup) |
| Robinhood order rejected, buying power | Fund the Agentic account |
| `invalid_grant` during OAuth | Code expired (minutes) — rerun setup, paste faster |
| Feed returns `events: []` | Normal — no journal events published yet today, or the feed is paused upstream |
