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
   it (`stop` in the dashboard, or `python agent.py stop`; Ctrl-C only if it
   runs in the foreground) — and that stopping leaves any open positions in
   their account.

## ⚠️ Where to install — read this if your code runs in a sandbox or the cloud

Assistants like **Claude Cowork** run their own code in an isolated VM or in
the cloud. **Do not install the agent there.** The agent must keep running
all market day, and the human's browser must reach its wizard at
`http://127.0.0.1:8721` — neither works from inside your sandbox, and the
agent stops when your session does.

Install on the **human's own computer** instead: open their terminal
(PowerShell on Windows, Terminal on Mac/Linux) with your computer-use /
on-screen tools — or ask them to — and paste the one-line installer:

```text
Windows (PowerShell):
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.ps1 | iex"

Mac / Linux (Terminal):
curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
```

Then drive the wizard in **their** browser (e.g. Claude in Chrome). If you
run directly on their computer already (Claude Code, a local agent), just
run the installer yourself.

## Fast path — the setup plan (recommended for assistants)

Instead of clicking through every wizard page, collect the human's choices
and apply them in one step. Order:

1. **Install** (above). The wizard opens in their browser.
2. **The human accepts the agreement themselves** in the wizard (rule 1).
   Plans refuse to run before this.
3. **Ask the human** for their choices — signal source, broker (and its API
   keys, if Alpaca/moomoo), an AI key + model (optional, any provider),
   per-trade size (rule 2: *their* number), daily caps, a notification
   channel, and any plain-English policy rules. Write them to a plan file;
   `python agent.py setup --example-plan` prints the format.
4. **Apply**: `python agent.py setup --from plan.json --json`.
   All-or-nothing — nothing is saved unless every part passes, including a
   real test call to the AI model and the broker's own key check. Then
   **delete the plan file** (it holds keys).
5. **Hand the `owner_steps` to the human.** Plans can never do these; the
   human does them in the wizard: log in to Robinhood (if chosen), buy and
   pay for AgentHC signal days from their own wallet (if the AgentHC feed),
   and anything else listed.
6. **Verify**: `python agent.py doctor --json` until `"ready": true`. Each
   failing check says `"who": "owner"` or `"who": "agent"` — fix yours,
   hand theirs over. Don't report setup as done before `ready` is true.
7. **Start** it (wizard "Start my agent now", or `python agent.py start` —
   both run it in the background, so closing the terminal doesn't stop it)
   and offer to make it start with the computer (`python agent.py autostart
   on`; ask first — it's their machine). It stays in **dry-run**: plans
   cannot turn dry-run off — the human says `dry off` in the dashboard when
   they decide to go live (rule 3).

## Boot sequence (manual / terminal)

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
2. **AgentHC feed access (sats-based)** — in the wizard's Signal source
   step:
   - **Prepaid signal days (recommended)**: the human picks 1 / 5 / 20 / 60
     signal days and pays ONE Lightning invoice (QR code) from the wallet
     they already use (Cash App, Strike, Phoenix…). ~$10 per signal day,
     priced in sats; a day is only used when the agent receives an entry or
     exit that day (quiet days free; "other trades" signals count only if
     they chose main + other). The agent holds no money. You never pay it
     for them.
     Paying constitutes acceptance of the feed terms (stated with the
     invoice).
   - **Advanced — daily auto-pay from an agent wallet**: an LNbits wallet the
     agent controls buys a ~$10 day-pass each market day (only with
     "Recurring day-pass" ON). The one-click wallet is on demo.lnbits.com, a
     public test server — keep only a few days of passes there. Safety caps:
     `max_autopay_sats` per invoice and `max_autopay_sats_per_day`.
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
`~/.agentic-trader/policy.md`, checked with the human's own AI API key — any
provider, picked in the wizard's "Connect your AI" step; veto-only). Help the human write their policy rules if they enable it —
the rules are theirs; you may translate their intent into clear bullet
points but never invent rules they didn't express.

### Step 3 — heartbeat

```bash
python agent.py start        # background; output in ~/.agentic-trader/agent-run.log
python agent.py autostart on # optional, with the human's OK: start at every login
python agent.py stop         # stop it
```

Polls the feed every 30s. On new `ENTERED` events it buys the configured
contracts to open; on matching `EXITED` events it sells to close positions it
opened. Logs every action. `start` keeps it running after the terminal
closes and restarts it after a crash; `python agent.py run` is the
foreground version (Ctrl-C stops). Only one copy can run at a time.

On a server, walk the human through GETTING_STARTED.md's options (the
`deploy/agentic-trader.service` systemd unit, Docker, or tmux) —
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
| `SSL handshake failed`, `ERR_SSL_PROTOCOL_ERROR`, can't connect | Almost always the human's own computer/network (antivirus HTTPS scanning, VPN, router/DNS filter). Run `python agent.py netcheck` — it names the cause and the fix |


## Optional: provisioning a VPS for the human

If the human wants always-on operation and asks you to handle hosting, you can
create the server for them — with their cloud API token, on their account:

1. Ask which provider (DigitalOcean / Hetzner / Vultr) and for an API token
   (read-write). Never store the token in the agent's config; use it once.
2. Create the smallest Ubuntu instance via the provider's API, passing
   `deploy/cloud-init.yaml` as user-data — the box installs agentic-trader on
   first boot. (e.g. DigitalOcean: POST /v2/droplets with `user_data`.)
3. SSH in as `trader` and run `./.venv/bin/python agent.py setup`,
   relaying the interactive steps to the human — the consent phrase and the
   Robinhood browser login are theirs alone; you must not perform them.
4. Enable always-on: `sudo systemctl enable --now agentic-trader`, confirm
   with `systemctl status agentic-trader`, and make sure the human received
   the test notification before you disconnect.
