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
   `~/.agentic-day-trade-agent/acceptance.json`, and never summarize the
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
   `~/.agentic-day-trade-agent/acceptance.json`. If DISCLAIMER.md ever changes,
   the gate re-triggers.
2. **AgentHC feed access** — registers a free API key
   (`POST https://api.traderhc.com/api/v1/agents/register`) or accepts an
   existing one, then accepts the feed terms
   (`POST /api/v1/trading/day-trade-ideas/accept-terms`). The feed requires
   **Premium tier** — if feed calls return `tier_required`, direct the human to
   `https://api.traderhc.com/docs` (`POST /api/v1/agents/upgrade`).
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

### Step 3 — heartbeat

```bash
python agent.py run
```

Polls the feed every 30s. On new `ENTERED` events it buys the configured
contracts to open; on matching `EXITED` events it sells to close positions it
opened. Logs every action. Runs until Ctrl-C.

For long-running operation suggest the human use `tmux`/`screen` or a systemd
user service — and remind them autonomous trading software requires human
oversight; it should not run unwatched for long periods.

## Troubleshooting map

| Symptom | Cause / fix |
|---|---|
| `403 terms_acceptance_required` | Agent auto-accepts and retries; if it loops, run setup again |
| `403 tier_required` | AgentHC key is FREE tier — upgrade to Premium (`/docs`) |
| `401 Invalid API key` | Key revoked/typo — re-run setup with a valid key |
| Robinhood order rejected, options-level alert | Enable options on the Agentic account (upgrade link in setup) |
| Robinhood order rejected, buying power | Fund the Agentic account |
| `invalid_grant` during OAuth | Code expired (minutes) — rerun setup, paste faster |
| Feed returns `events: []` | Normal — no journal events published yet today, or the feed is paused upstream |
