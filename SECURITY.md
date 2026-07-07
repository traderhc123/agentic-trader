# Security model

This agent moves real money on your behalf. Here is what protects you, what
you control, and what to check before running it.

## Built-in rails (enforced in code)

| Rail | What it prevents |
|---|---|
| **Hard consent gate** | The agent refuses ALL setup/trading actions until you personally accept the versioned DISCLAIMER.md (SHA-pinned; re-triggers if the text changes). Scripts and LLMs are instructed never to accept for you, and the one-line installer never touches it. |
| **Dry-run mode** (default ON at setup) | No orders while you trial the source and policy — actions are logged/notified only. |
| **Daily entry cap** (`max_entries_per_day`, default 5) | A runaway or malicious feed cannot open unlimited positions. Mechanical — checked before the LLM policy. |
| **Event validation** | Ticker/expiry/strike/type are sanity-checked before any instrument lookup; malformed or absurd events are dropped. |
| **Staleness guard** (`max_event_age_s`, default 300) | A machine waking from sleep will not buy into an hours-old entry. Exits are never stale-blocked. |
| **Auto-pay cap** (`max_autopay_sats`, default 30,000) | The wallet never pays an oversized Lightning invoice, even if a paywall misbehaves. |
| **Exit pairing** | The agent only ever sells positions it opened; dry-run positions can never trigger real orders. |
| **LLM policy brain — veto-only** | The LLM can only BLOCK entries against YOUR written policy; it can never initiate, enlarge, or redirect a trade. On any LLM error the default is skip (fail-safe). Events fed to it are treated as untrusted data — embedded "ignore the policy" text is grounds for a veto, and the worst-case injection outcome equals not having a policy at all. |
| **File permissions** | `~/.agentic-trader/` is `0700`; config (wallet admin key, API keys) and broker tokens are `0600`. |
| **Web UI localhost-only** | The setup wizard (:8721) and dashboard (:8722) bind 127.0.0.1 exclusively — nothing is exposed to the network; remote access requires an SSH tunnel. The dashboard chat is a fixed command allowlist plus answer-only LLM Q&A; it cannot place trades or modify the agent's code. |
| **HTTPS enforcement** | Custom feed URLs and wallet URLs must be HTTPS (or localhost) — signals and spend keys never travel cleartext. |

## What YOU control (read before going live)

- **Robinhood blast radius**: the agent can only trade the dedicated Agentic
  account. Fund it with exactly what you're willing to lose; your main
  account is untouchable by design (Robinhood enforces `agentic_allowed`).
- **Wallet blast radius**: the agent's auto-created wallet lives on a hosted
  LNbits instance (custodial — the operator of that instance technically holds
  the sats; default demo.lnbits.com, override with AGENTIC_TRADER_LNBITS or
  bring your own). This is exactly why the rule is: keep
  ~a month of feed fees in it, nothing more.
- **API keys**: the Anthropic key (policy brain) should be a dedicated key
  with a spend limit set in the Anthropic console.
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
