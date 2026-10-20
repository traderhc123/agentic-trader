# Security model

This agent moves real money on your behalf. Here is what protects you, what
you control, and what to check before running it.

## Built-in rails (enforced in code)

| Rail | What it prevents |
|---|---|
| **Hard consent gate** | The agent refuses ALL setup/trading actions until you personally accept the versioned DISCLAIMER.md (SHA-pinned; re-triggers if the text changes). Scripts and LLMs are instructed never to accept for you, and the one-line installer never touches it. |
| **Dry-run mode** (default ON at setup) | No orders while you trial the source and policy — actions are logged/notified only. |
| **Daily entry cap** (`max_entries_per_day`, default 5) | A runaway or malicious feed cannot open unlimited positions in a day. Counted from the full trade log — flooding the log with skippable events cannot push today's entries out of view. Mechanical — checked before the LLM policy. |
| **Open-positions cap** (`max_open_positions`, default 10) | The daily cap resets each day; this one doesn't — total concurrent exposure stays bounded no matter how many days a feed misbehaves. Exits are never blocked. |
| **Event validation** | Ticker/expiry/strike/type are sanity-checked before any instrument lookup; malformed or absurd events are dropped. |
| **Staleness guard** (`max_event_age_s`, default 300) | A machine waking from sleep will not buy into an hours-old entry. Exits are never stale-blocked. |
| **Auto-pay cap** (`max_autopay_sats`, default 30,000) | The wallet never pays an oversized Lightning invoice, even if a paywall misbehaves. |
| **Daily auto-pay cap** (`max_autopay_sats_per_day`, default = one invoice's worth) | A paywall that keeps rejecting a valid pass cannot make the agent re-buy on every poll — once today's budget is spent, auto-pay stops until tomorrow and you're told why. The spend is counted before paying, so a payment that errors mid-flight still counts. |
| **Contract cap** (`max_contracts_per_trade`, default 100) | Applies to BOTH sizing modes — a typo like `contracts_per_trade: 1000` places at most 100. |
| **Max daily loss** (`max_daily_loss_usd`, opt-in; wizard Safety step or `set max loss 300`) | Once today's **estimated** realized loss reaches the limit, no new entries until tomorrow; exits always run. Estimated from live quotes at entry/exit (falling back to the feed's own paper P&L %) — brokers don't report fill prices to the agent, so slippage isn't counted. |
| **Exit pairing** | The agent only ever sells positions it opened; dry-run positions can never trigger real orders. |
| **LLM policy brain — veto-only** | The LLM can only BLOCK entries against YOUR written policy; it can never initiate, enlarge, or redirect a trade. On any LLM error the default is skip (fail-safe). Events fed to it are treated as untrusted data — embedded "ignore the policy" text is grounds for a veto, and the worst-case injection outcome equals not having a policy at all. |
| **File permissions** | `~/.agentic-trader/` is `0700`; config (wallet admin key, API keys), `state.json` (day-pass token), the dashboard token and broker tokens are `0600`. On Windows the folder's inherited ACL is replaced with one granting only your user (via `icacls`). |
| **Web UI localhost-only + login** | The combined setup wizard + dashboard (127.0.0.1:8721) binds localhost exclusively — nothing is exposed to the network; remote access requires an SSH tunnel. Every page and API call also requires a per-install secret (`~/.agentic-trader/ui_token`), handed to your browser once via the login link the agent prints/opens (`python agent.py link` prints it again) and kept in an HttpOnly cookie — other OS users and web pages can't drive it. (A program running as *your* user can read the token file, as it can any of your files.) The dashboard chat is a fixed command allowlist plus answer-only LLM Q&A; it cannot place trades. |
| **Verified-source updates** | The Update button only pulls from github.com/traderhc123/agentic-trader (forks: set `AGENTIC_TRADER_UPDATE_REMOTE`), shows the incoming commit list first, and installs exactly the commit you reviewed — if anything new lands in between, nothing is installed. Fast-forward only, so local edits are never overwritten. |
| **Self-edit — human merge gate** | Off by default (`self-edit on` to enable). The agent's LLM can WRITE changes to its own code, but nothing applies until the owner reviews the unified diff in the dashboard and clicks Apply; applies are backed up, compile-checked, and auto-rolled-back on failure; and a **FOUNDATION layer is immutable to the agent**: agent.py (consent gate + safety pipeline), self_edit.py (the governor itself), llm_policy.py, llm_client.py, lightning_wallet.py, webui.py, platform_compat.py, keep_running.py (the one-copy lock), broker_events.py (dropped-trade reporting), requirements.txt, DISCLAIMER.md, SECURITY.md, install.sh, install.ps1, LICENSE, and every **existing** module in `brokers/` and `sources/` (the order and signal paths) can never be self-rewritten — the agent may only ADD new adapters there (registry edits are flagged in the review) — — enforced at both propose and apply time, case-insensitively and against Windows name aliasing (`Agent.py`, `agent.py.`, `agent.py:stream`). A prompt-injected proposal can only sit unapplied in a diff view, and can never target the foundation at all. |
| **HTTPS enforcement** | Custom feed URLs and wallet URLs must be HTTPS (or localhost) — signals and spend keys never travel cleartext. |

## What YOU control (read before going live)

- **Robinhood blast radius**: the agent can only trade the dedicated Agentic
  account. Fund it with exactly what you're willing to lose; your main
  account is untouchable by design (Robinhood enforces `agentic_allowed`).
- **Wallet blast radius**: the agent's auto-created wallet lives on a hosted
  LNbits instance (custodial — the operator of that instance technically holds
  the sats; default demo.lnbits.com, override with AGENTIC_TRADER_LNBITS or
  bring your own). ⚠️ demo.lnbits.com is a **public test server** — it can be
  reset or go offline and its balance lost; the wizard, setup and `doctor`
  warn whenever you're on it. On a demo instance keep only a few days of
  passes; on your own/paid instance, ~a month of feed fees, nothing more.
- **API keys**: the AI key (policy brain / Q&A — any provider) should be a
  dedicated key with a spend limit set in that provider's console. Keys are
  only ever sent over HTTPS, or to a model on this computer (localhost).
- **Model choice**: the policy brain is veto-only, and any error, refusal or
  reply that isn't a clear JSON yes/no skips the trade. A weaker model can
  still misread your policy or be swayed by text inside an event — the worst
  case is the same as having no policy at all. Prefer a capable model.
- **Machine hygiene**: anyone with access to `~/.agentic-trader/` has your
  wallet key and broker session. Run on a machine you control; full-disk
  encryption recommended; don't run on shared hosts.
- **Watch it**: configure notifications (Discord/ntfy/Telegram). Autonomous
  and silent is the failure mode — every action, veto, and error notifies.

## Threat model notes

- **Malicious/compromised signal source**: bounded by validation + staleness
  + the daily cap + your policy + 1-contract-scale sizing. A bad source can
  at worst open `max_entries_per_day` positions of your configured size per
  day until you stop it — set the cap accordingly.
- **Prompt injection via feed content**: the policy brain's system prompt
  pins it to veto-only judgment of your policy; injected instructions in
  event fields cannot make it exceed that authority (approving is the
  ceiling, and that equals mechanical mode).
- **This template is code you should read.** ~900 lines total, dependency
  surface is `requests` + the official `anthropic` SDK. Pin or vendor if
  you fork.

## Reporting

Found a vulnerability? Open a GitHub issue with the label `security` (no
exploit details in public), or note it and we'll coordinate a fix.
