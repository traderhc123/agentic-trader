# Getting started (for non-technical users)

You don't need to be a programmer to run this agent. This guide assumes
nothing. Read [DISCLAIMER.md](DISCLAIMER.md) first — real money, your
responsibility, not investment advice.

## The easiest path: let an AI set you up

If you use **Claude** (or ChatGPT or any AI assistant that can run commands
on your computer — e.g. the Claude desktop app with Claude Code):

> Paste this to your AI assistant:
> **"Set me up with https://github.com/traderhc123/agentic-trader — follow
> its BOOT.md"**

The assistant will install everything and walk you through setup step by
step. Two things it will (correctly) make YOU do yourself: read and accept
the agreement, and log into Robinhood in your own browser. That's by design.

> **Does it run "inside" my Claude subscription?** No — Claude helps you set
> it up and can start it for you, but the agent itself is a small program
> that runs on a computer you choose (below). It keeps running after you
> close Claude, and it doesn't consume your Claude subscription. (The
> optional "policy brain" feature uses an Anthropic **API key**, which is a
> separate pay-per-use thing from a Claude.ai subscription.)

## Where should it run?

The agent must be **awake during US market hours** (9:30 AM–4:00 PM Eastern,
Mon–Fri). It sleeps itself outside those hours. Pick one:

| Option | Good for | Cost |
|---|---|---|
| **Your everyday computer** | Trying it out (start here, in dry-run mode) | Free |
| **An old computer / Mac mini / Raspberry Pi at home** | Set-and-forget, no monthly fee | Free |
| **A tiny cloud server (VPS)** | Most reliable — always on, survives power cuts | ~$5/month |

**Laptop caveat:** if the lid closes or it sleeps, the agent pauses. It
catches up when it wakes (and refuses to act on entries older than 5
minutes — a safety feature), but exits can be delayed. Fine for dry-run
trials; not ideal for live money.

**How much terminal is involved, really?** Three pasted lines, once — the
install, `setup --web`, and `run`. Every decision and all day-to-day use
happens in your browser (the setup wizard, then the dashboard). If you'd
rather touch a terminal zero times, use the AI-assistant path above: Claude
pastes the lines, you use the browser.

### Option A — your computer (10 minutes, start here)

1. Install Python 3.10+ (python.org, or it's already on Macs).
2. Open Terminal (Mac: Cmd-Space, type "Terminal") and paste:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
   cd ~/agentic-trader
   ./.venv/bin/python agent.py setup --web
   ```
   A setup page opens in your browser — read and accept the agreement, pick
   your signal source, click **Connect Robinhood** (it bounces you to
   Robinhood and straight back), choose a dollar budget per trade, and set
   the safety rails. It defaults to **dry-run mode** — no real orders — so
   you can watch it for a few days risk-free. That's the last of the
   terminal apart from one more pasted line below.
3. Start it:
   ```bash
   ./.venv/bin/python agent.py run
   ```
   Leave that window open, then open **http://127.0.0.1:8722** — your
   agent's own dashboard: live status, every action it's taken, and a chat
   box where you can say `pause`, `resume`, `set budget 500`, `dry off`, or
   just ask it questions about what it's been doing. On a remote server,
   tunnel first: `ssh -L 8722:127.0.0.1:8722 user@yourserver`.

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
  worst day.
- **Notifications**: set up Discord/ntfy/Telegram during setup — you get a
  message on every action, veto, and error, plus a daily digest after the
  close. Never run it silently.
- **Your policy, enforced**: optionally write plain-English rules in
  `~/.agentic-trader/policy.md` ("skip puts", "max 2 trades a day") and the
  agent checks every entry against them before acting.

Full details: [SECURITY.md](SECURITY.md).

## Checklist before going live (turning dry-run off)

- [ ] You read DISCLAIMER.md and accepted it yourself
- [ ] Robinhood **Agentic account** exists, has **options approval**, and is
      funded with only what you can afford to lose
- [ ] If using the AgentHC feed: agent wallet holds sats (~50k ≈ a month)
- [ ] Notifications tested (you got the test message)
- [ ] You watched at least a few days of dry-run activity and it did what
      you expected
- [ ] You know how to stop it: Ctrl-C (or `docker stop` / `systemctl stop
      agentic-trader`) — open positions stay in your account; close them in
      the Robinhood app if needed
