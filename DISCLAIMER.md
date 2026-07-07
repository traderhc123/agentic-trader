# Agentic Day Trade Ideas — User Agreement & Full Disclaimer

**Terms version: `agent-terms-2026.07`**

> This agent template will not run until you have read this entire document
> and affirmatively accepted it. Your acceptance is recorded locally with the
> terms version and a timestamp. If this document changes, you must accept
> again before the agent will run.

## 1. What this software is

This repository is an open-source **template** for an autonomous AI trading
agent. It (a) reads the "Agentic Day Trade Ideas" feed published by AgentHC —
a machine-readable feed of AgentHC's own published, **hypothetical
paper-trading journal** (past-tense statements of fact such as "ENTERED" and
"EXITED") — and (b) can place real-money options orders in **your** Robinhood
Agentic brokerage account, under **your** configuration, at **your** direction.

## 2. Not investment advice; no adviser relationship

- The Agentic Day Trade Ideas feed is educational and informational commentary
  only. It is **NOT investment advice, NOT trade recommendations, NOT trading
  instructions**, and not an offer or solicitation to buy or sell any security,
  option, or other financial instrument. It is general in nature, identical for
  all subscribers, delivered at the same time to all subscribers, and is not
  tailored to any person's financial situation, objectives, or risk tolerance.
- **Sky Manor Trading LLC ("AgentHC") is not a registered investment adviser or
  broker-dealer.** The authors and maintainers of this template are not
  registered investment advisers or broker-dealers. Nothing in the feed, this
  software, or its documentation creates an advisory, fiduciary, or brokerage
  relationship.
- **AgentHC is not an investment advisor and cannot advise position sizing.**
  Every configuration choice in this software — including whether to run it at
  all, how many contracts to trade, which events to act on, and all risk
  management — is made solely by you.

## 3. You accept ALL liability

By accepting this agreement you acknowledge and agree that:

- **All trading decisions made by this agent are your decisions.** The agent
  acts mechanically on your configuration; you are responsible for every order
  it places in your account, including orders resulting from bugs, feed errors,
  network failures, stale data, duplicate events, or unexpected market
  conditions.
- **You accept all liability** for any losses, damages, costs, taxes, or other
  consequences arising from your use of this software and the feed. To the
  maximum extent permitted by law, neither Sky Manor Trading LLC, nor AgentHC,
  nor the authors or contributors of this template shall be liable for any
  direct, indirect, incidental, consequential, or special damages of any kind.
- This software is provided **"AS IS", without warranty of any kind**, express
  or implied, including fitness for a particular purpose. Autonomous trading
  software can and does fail in unexpected ways. Human oversight is required;
  do not run this agent unattended with money you cannot afford to lose.

## 4. Hypothetical performance

Any accuracy rates, win rates, or profit/loss figures referenced by the feed
(including `paper_pnl_pct` fields) are **HYPOTHETICAL or SIMULATED**, derived
from paper-traded signals modeled at quoted prices. They do not represent
actual trades or the results of any customer account and do not reflect
commissions, slippage, fees, or real-world liquidity. HYPOTHETICAL PERFORMANCE
RESULTS HAVE INHERENT LIMITATIONS. NO REPRESENTATION IS BEING MADE THAT ANY
ACCOUNT WILL OR IS LIKELY TO ACHIEVE PROFITS OR LOSSES SIMILAR TO THOSE SHOWN.
Past performance is not indicative of future results.

## 5. Positions, conflicts, and feed-first publication

Sky Manor Trading LLC and its principals may hold, and may buy or sell,
positions in the securities or options referenced in journal events. Journal
events are published to all subscribers first; AgentHC and its principals may
or may not subsequently enter a corresponding position in their own accounts,
and any such entry is placed no earlier than approximately two (2) minutes
after the corresponding event has been published to the live feed. Exits from
any such position may occur at or around the time the corresponding EXITED
event is published. Assume a position may exist.

## 6. Options risk

Options trading involves substantial risk and is not suitable for all
investors; you may lose your entire investment or more. Before trading
options, read the Options Clearing Corporation's "Characteristics and Risks of
Standardized Options" (the Options Disclosure Document):
<https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document>

## 7. Your broker relationship

Order execution happens in your own Robinhood Agentic account under your
agreement with Robinhood. Robinhood is not affiliated with AgentHC or this
template. You are responsible for complying with Robinhood's terms, including
their agentic-trading terms, and for all fees, margin, and settlement
obligations in your account.

## 8. Acceptance

Always do your own research and consult a licensed financial professional
before making any investment decision. By typing the acceptance phrase when
prompted (or placing a valid acceptance record at `~/.agentic-day-trade-agent/
acceptance.json`), you represent that you have read and understood this entire
agreement, that you accept all of its terms including the liability terms in
Section 3, and that you are legally able to enter into it.

© 2026 Sky Manor Trading LLC (feed) / template contributors. All rights reserved.
