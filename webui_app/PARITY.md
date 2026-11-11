# /v2 parity with the classic dashboard: acceptance table for PR 4 (`feat/v2-parity-and-switch`)

Base: `origin/main` c73dfc2. Classic = what owners actually get at `/dash` and `/setup`: the React build (`webui_app/src/dash`, `webui_app/src/wizard`), because `webui_dist/index.html` is committed (webui.py:2437-2444). The inline `_DASH_HTML` / `_WIZARD_HTML` are only served with `?ui=classic` or with no build. Every row cites both versions where both exist. `H` = inline page in webui.py, `R` = React classic page.

Sources: `dash-inventory.md` / `dash-rows.json` (D-1..D-75) and `setup-half.md` / `setup-half.json` (S-1..S-57), merged here. The same control on both pages is one row (column *Src*). Route-only rows D-64..D-75 are folded into **Server routes** below. PR 1-3 state was read from `/root/wt/at-v2-look` (2 commits: f8356d5, edd393a) and `/root/wt/at-v2-api` (a21eb96 settings API, cb16684 condition codes); `/root/wt/at-v2-forms` does not exist yet, so every PR 3 status comes from SPEC text.

**Counts:** 93 rows. 23 already in /v2, 22 planned in PR 1-3, **37 missing** (13 of them money-affecting or irreversible), 7 first-run-only, 4 not applicable. Server: 22 GET + 28 POST routes, each with a /v2 use or a classic-only reason. Every route in `W.routes_get`/`W.routes_post` is named by at least one row, and so is every `/api/` string in `webui_app/src`. No route was found without a row.

Status values: `in-v2` = there today at origin/main · `planned PR n` = the SPEC puts it in PR 1-3 (PR 4 checks it as acceptance) · `missing` = PR 4 builds it · `first-run-only` = stays in the wizard · `n/a` = deliberately not carried over (see *Classic-only, because…*).

## PR 4 build status (kept current as rows are built)

Stage A (groups 0-3), branch `feat/v2-parity-and-switch`:

- **Group 0, server groundwork: done.** View-model readings `results.paid_days` (P-26), `limits.budget_hints` (P-33), `signals.preview` (P-48), `proposal` (P-74); conditions `budget_too_small`, `proposal_pending`, `consent_needed` (P-83). `GET /api/v2/settings` gains `expiry`, `computer.open_dashboard_at_login`, `computer.dashboard`, `signals.dry_run_preview`, `code.self_edit_enabled`, `broker.warning` (a code, `options_off`: the classic text carries the whole account number) and `consent`; `GET /api/v2/agreement`. The route-coverage test is `tests/test_v2_parity.py::test_every_route_has_a_v2_caller_or_a_classic_only_reason`. The UI for these readings belongs to groups 5, 7, 8, 9 and 10.
  - Deviation: the paid-day figures sit in `Results.paid_days {real, practice}` (with `paid_on` / `estimated_on` day lists) rather than in each `Book` / `BookDay`, so the Book shape the panels and chart read is unchanged.
- **Group 1, the dashboard switch (P-71): done.** Lives in Settings → This dashboard (decision 7). The classic `/dash` link is a plain link to `/v2` (the smallest edit); remembering the choice is done from /v2.
- **Group 2, the agent (P-07 Stop, P-08 Start): done.** Settings → The agent on this computer.
- **Group 3, this computer (P-66, P-67, P-68, P-69, P-70, P-90 link): done.** Settings → This computer → Move (one Move control).

Stage B (groups 4-7), same branch:

- **Group 4, Settings → Help and support (P-79, P-80, P-81, P-82): done.** Connection check, support report, Tip AgentHC (dialog) and Your agreement (text from `GET /api/v2/agreement`), each one button in Settings. The classic help and tip routes are reused as they are; the tip dialog names no wallet or pass route.

Still to build: groups 5-11. Rows marked *pending: group N* in the Server routes table must all be *v2* when PR 4 is finished.

## Changes to the input inventories after checking the code

13 `in-v2` rows were checked against the v2 source at origin/main and the PR worktrees: D-1, D-5, D-11, D-14, D-16, D-23, D-27, D-31, D-32, D-36, D-60, S-16 and S-27. All of them are there. Two show less than classic (D-16, D-32), and D-14 turned up a duplicate. The changes:

- **D-32 problems** is now `in-v2 (partial)`. The count is there (Today.tsx:535), along with the Activity *Problems* filter (Activity.tsx:16) and the fix hints (copy.ts `e.hint`). The classic page groups problems by kind with ×count and last time; /v2 does not → P-14.
- **D-14 entries today**: confirmed, but the Rules & limits page shows the same three readings twice. The *Daily limits* rows (Pages.tsx:154-192) and the `LimitsPanel` at Pages.tsx:298 both show used today, lost today and open now. Added to the redundancy list.
- **D-16 feed tile**: the wallet-sats half is not in /v2. That half moved to P-51 (missing).
- **S-36 built-in rules** is no longer a gap. PR 2 `v2_rules` (at-v2-api a21eb96, webui.py:4706-4760) accepts all six `rule_*` keys and calls `rules.apply_settings`, so the row is `planned PR 2+3`. PR 3 must render all six fields (acceptance).
- **S-10 policy off**: PR 2 `v2_rules` accepts `{policy:null}` and calls `llm_policy.disable_policy()`. The route is done; the button is a PR 3 acceptance item (P-38).
- **S-21 recurring**: PR 2 added `POST /api/v2/wallet/recurring`, two-step both ways. Only the UI is missing (P-53).
- **S-6 / D-18 readings**: PR 2 `GET /api/v2/settings` carries `ai.*` and `rules.summary/policy/policy_blocks_entries`.
- **D-60 Stop**: the gap is confirmed. PR 2 `v2_command` accepts only pause, resume and set_mode (at-v2-api webui.py:4969-4977).
- **D-31 / pass duplicates**: confirmed. `derive.notes()` (derive.ts:351-389) ignores each notice's `code`. PR 2 cb16684 adds `Status.conditions` (22 codes, including `low_cash` and `signal_pass`), so PR 3 can merge them by code.
- **New redundancy found:** the theme toggle appears 3 times: the sidebar (App.tsx:333), Settings (Pages.tsx:577) and More (Pages.tsx:620).

## Full table

| ID | Kind | What the owner sees / does | Classic location (origin/main) | Classic route(s) | Money / irreversible | Status | /v2 home (today, or proposed: one home) | Route /v2 uses | Src |
|---|---|---|---|---|---|---|---|---|---|
| P-01 | reading | Mode: LIVE (real orders) / Practice (no orders), with the dry-run note | H webui.py:1970 #b-mode, setMode() :2181-2201 · R dash/Dashboard.tsx:228, ModeBanner :534-613 | GET /api/status mode, dry_run_note | no | in-v2 | Band mode-word / mode-say (derive.bandModel) | GET /api/v2/status (as is) | D-1, D-13 |
| P-02 | reading | Market open / closed, with open and close times | H webui.py:1971 #b-market · R Dashboard.tsx:231-237 | GET /api/status fields.market | no | in-v2 | Vitals 'market' (derive.marketVital) | GET /api/v2/status | D-2 |
| P-03 | reading | Paused / On hold (+ why) / Restarting, and the pause feed note | H webui.py:1972 #b-paused, :2225 · R Dashboard.tsx:238, ModeBanner | GET /api/status paused, hold, pause_note, restarting | no | in-v2 | Band run-state (pause.kind, capabilities.listening) | GET /api/v2/status | D-3, D-13 |
| P-04 | reading | Agent not running / page can't reach the agent | H webui.py:2187 badMode(), :2309 · R Dashboard.tsx:241 | GET /api/status (OFFLINE / fetch failure) | no | in-v2 | App.tsx Waiting :407; band run-state lost/silent/stopped | GET /api/v2/status | D-4 |
| P-05 | control | Pause new entries / Resume | H webui.py:1981 #q-pause, quick() :2326 · R Dashboard.tsx:281-287 · chat pause\|resume agent.py:5072/:5087 | POST /api/command pause\|resume | no | in-v2 → PR 3 step 6 | Band (only home). Today 'unmanaged' note's Resume button becomes a plain pointer to the band (No-redundancy) | PR 2 POST /api/v2/command {action:pause\|resume} | D-11 |
| P-06 | control | Switch Practice ↔ LIVE | H webui.py:1982 #q-dry, quick() :2328 · R Dashboard.tsx:288-322 · wizard Safety webui.py:974-982, steps.tsx:727-736 · chat dry on\|off\|go live agent.py:5165/:5172 | POST /api/command dry on\|dry off; POST /api/safety .dry | **yes** | in-v2 → PR 3 step 6 | Band (only home); wizard keeps the first-run choice | PR 2 POST /api/v2/command {action:set_mode} (LIVE: two-step + acknowledge) | D-12, S-32 |
| P-07 | control | Stop the agent completely | chat 'stop' agent.py:5045 (no classic button); /v2 Settings Pages.tsx:549-557 → Dialogs.tsx:280 | POST /api/command 'stop' | **yes** | built PR 4 (group 2) | Settings → The agent on this computer | ADD {action:"stop"} to POST /api/v2/command, two-step (preview lists real positions that will no longer be sold) | D-60 |
| P-08 | control | Start the agent again after a stop / crash / reboot without autostart | classic: reopen /setup → Launch → 'Start my agent now' (webui.py:1029-1039, startLocal :1681; steps.tsx:834-859) | POST /api/start-local | **yes** | built PR 4 (group 2) | Settings → The agent on this computer → Agent row (replaces 'Open setup to start it' in Band.tsx:154, Today.tsx:48, Pages.tsx:512) | NEW POST /api/v2/agent/start over start_local (same consent/broker/policy/moved-away checks); two-step when the saved mode is LIVE | S-43 |
| P-09 | reading | New entries today n / cap, with meter | H webui.py:1992-1994 · R StatusStrip Dashboard.tsx:675-693 | GET /api/status fields['entries today'] | no | in-v2 | Today LimitsPanel (Today.tsx:338). Rules & limits shows it twice (row + LimitsPanel): remove one in the PR 3 audit | GET /api/v2/status | D-14 |
| P-10 | reading | Open positions count | H webui.py:1995 · R Dashboard.tsx:694 | GET /api/status fields['open positions'] | no | in-v2 | Nav count (App.tsx:311) + vitals exposure | GET /api/v2/status | D-15 |
| P-11 | reading | Open positions table (symbol, call/put, expiry, strike) and what the agent will do | H webui.py:2021-2025 · R PositionsPanel Dashboard.tsx:851-896 | GET /api/status fields['open positions'] | no | in-v2 | Holdings view; Today HoldingsPanel; PositionDrawer | GET /api/v2/status | D-22 |
| P-12 | reading | Expiry-day, memory-file (records) and second-copy warnings | R ExpiryBanner Dashboard.tsx:515-531 | GET /api/status expiry_warnings | no | in-v2 | Today notes (notices records / expiry / second_copy; PR 2 condition codes records_unwritten, position_expiring*, second_copy) | GET /api/v2/status | D-30 |
| P-13 | reading | Low cash: cannot pay for one trade | R WhyNoTrades Dashboard.tsx:459-464 · wizard Launch steps.tsx:822-827, 952-963 | GET /api/status cash_warning; GET /api/broker/cash | no | in-v2 | Today note 'cash' + vitals. DUPLICATE today: agent notice low_cash AND derived cash note; PR 3 merges by condition low_cash (PR 2 cb16684) | GET /api/v2/status | D-31, S-41 |
| P-14 | reading | Problems today, grouped by kind (×count, last time, contract, message, Fix hint) | R WhyNoTrades Dashboard.tsx:465-478 | GET /api/status problems | no | in-v2 (partial) | Today DayPanel 'N problems today' → Activity Problems filter + EventDrawer hint. Not grouped by kind | GET /api/v2/status activity (add grouping in the Activity Problems filter) | D-32 |
| P-15 | control | Ask your agent a question (answers only; never runs a command) | H webui.py:2051-2055 #cmd, send() :2335 · R ConsolePanel Dashboard.tsx:1158-1216 · fallback d_command webui.py:4199-4210 → _ask_llm :4698 | POST /api/command (free text → _ask_llm) | no | missing | Today → 'Ask your agent' (spec 4a.4) | NEW POST /api/v2/ask → _ask_llm with the same status fields + trades d_command passes; never apply_command, never 'code:' | D-38, S-56 |
| P-16 | reading | Conversation log (questions and answers this visit) | H webui.py:2048-2050 #chatlog · R ChatLog Dashboard.tsx:1184-1200 | POST /api/command replies | no | missing | Today, under the Ask box (kept in the page only, not stored) | POST /api/v2/ask | D-39 |
| P-17 | control | Quick-command chips (status, set cap 3, set positions 5, my rules, start at login?, open at login, keep awake, why the last skip?) | H webui.py:2042-2047 · R Dashboard.tsx:1168-1183 | POST /api/command | **yes** | n/a | Each has a typed control: see Typed commands (why-skip → Today Last decision + Ask box) | — | D-40 |
| P-18 | reading | Activity rows: time ET, entered / exited / skipped + reason, contract, LIVE/practice, detail + Fix, est P&L | H webui.py:2027-2034, :2281-2308 · R Blotter Dashboard.tsx:980-1093 | GET /api/trades | no | in-v2 | Activity view + EventDrawer | GET /api/v2/status activity, GET /api/v2/activity | D-35 |
| P-19 | control | Show all / Load older activity | R Dashboard.tsx:1073-1087, loadOlder :163-187 | GET /api/trades?before=&limit=100 | no | in-v2 | Activity 'Load older' (views/Activity.tsx:170) | GET /api/v2/activity | D-36 |
| P-20 | reading | Activity sparkline in the panel header | H webui.py:2028-2033 | GET /api/trades | no | n/a | Superseded by the Results chart (P-23) | — | D-37 |
| P-21 | reading | Estimated realized P&L, all time (live book, else dry-run book) | R PnlPanel Dashboard.tsx:746-773 · H #pnl-big webui.py:2030 | GET /api/pnl | no | in-v2 | Results BookPanel 'All time' per book (Results.tsx:54) | GET /api/v2/status results.* | D-23 |
| P-22 | reading | Exits, won / lost, period | R Dashboard.tsx:784-786 | GET /api/pnl | no | in-v2 | Results BookPanel (Results.tsx:58-61) | GET /api/v2/status | D-25 |
| P-23 | reading | Cumulative P&L chart | R PnlChart.tsx via Dashboard.tsx:788-796 · H #spark webui.py:2031 | GET /api/pnl points | no | planned PR 1 | Results, first on the page, per book, 7d/30d/All (in progress: /root/wt/at-v2-look edd393a) | GET /api/v2/status results.*.days | D-26 |
| P-24 | reading | P&L by day | R Dashboard.tsx:800-830 (day OPENED) | GET /api/pnl days | no | in-v2 | Results by-day table (day CLOSED, view_model._results). Caption must say 'by the day it closed' | GET /api/v2/status | D-27 |
| P-25 | reading | P&L footnotes: unmeasured exits, dry-run exits excluded, refresh failure | R Dashboard.tsx:793-798, :831-836 | GET /api/pnl | no | in-v2 | BookPanel unmeasured text, separate Practice book, stale stamp | GET /api/v2/status | D-28 |
| P-26 | reading | Result of trades opened on paid signal days, and a per-day 'pass' marker | R Dashboard.tsx:774-783, by-day Pass column :816 | GET /api/pnl passes, pass_days, days[].pass | no | missing | Results → 'What it has cost you': row 'Result on paid signal days' + pass marker in the by-day table | Extend view_model.py (read-only) from A.pnl_summary: results.real.paid_days {days, pnl}, days[].paid | D-24 |
| P-27 | reading | Size per trade (main + other-trades caption) | H webui.py:1999-2000 · R Dashboard.tsx:633-639, :700-715 | GET /api/status fields.sizing | no | in-v2 | Rules & limits → Trade size (Pages.tsx:194-211) | GET /api/v2/status | D-17 |
| P-28 | control | Trade size, main book: dollars / contracts / percent | wizard Sizing webui.py:936-950 doSizing :1646 · steps.tsx:487-567 · chat set budget\|percent\|contracts agent.py:5179-5224 | POST /api/sizing; POST /api/command | **yes** | planned PR 2+3 | Rules & limits → Trade size | PR 2 POST /api/v2/settings/limits trade_size.main (two-step) | S-29, D-56 |
| P-29 | control | Trade size, other trades: same as main / dollars / contracts / percent | wizard webui.py:951-967 · steps.tsx:568-608 · chat set other …, other sizing same agent.py:5189-5246 | POST /api/sizing .other; POST /api/command | **yes** | planned PR 2+3 | Rules & limits → Trade size | PR 2 POST /api/v2/settings/limits trade_size.other (two-step) | S-30, D-56 |
| P-30 | control | New entries per day (cap) | wizard webui.py:986-987 · steps.tsx:744-746 · chat set cap agent.py:5247 | POST /api/safety .cap; POST /api/command | **yes** | planned PR 2+3 | Rules & limits → Daily limits | PR 2 POST /api/v2/settings/limits max_entries_per_day (two-step) | S-33, D-57 |
| P-31 | control | Daily loss limit ($, off) | wizard webui.py:988-990 · steps.tsx:747-754 · chat set max loss agent.py:5261 | POST /api/safety .max_loss; POST /api/command | **yes** | planned PR 2+3 | Rules & limits → Daily limits | PR 2 POST /api/v2/settings/limits max_daily_loss_usd (two-step) | S-34, D-57 |
| P-32 | control | Max open positions | chat only: set positions agent.py:5254 | POST /api/command | **yes** | planned PR 2+3 | Rules & limits → Daily limits | PR 2 POST /api/v2/settings/limits max_open_positions (two-step) | S-35, D-57 |
| P-33 | reading | Budget hint: N signals cost more than $X; $S would have taken k | R WhyNoTrades Dashboard.tsx:479-492 | GET /api/status budget_hints (agent._budget_hints :1924) | no | missing | Rules & limits → Trade size panel. Today shows it once as a note (new condition budget_too_small) linking there | Extend view_model.py (read-only): limits.budget_hints; add condition budget_too_small | D-33 |
| P-34 | control | One-click 'Set budget to $S' from the hint | R Dashboard.tsx:488-490 + ConfirmDialog :495-509 | POST /api/command set budget S \| set other budget S | **yes** | missing | Rules & limits → Trade size: 'Use $S' pre-fills that row's form (no control of its own) | PR 2 POST /api/v2/settings/limits trade_size (two-step); no new route | D-34 |
| P-35 | reading | Your rules: on / none / blocking entries; built-in rules summary; written rules | H webui.py:2001-2002, :2264-2266 · R Dashboard.tsx:669-671, :716-738 · chat rules\|my rules agent.py:4976 | GET /api/status fields.rules, ['policy brain'] | no | planned PR 2+3 | Rules & limits → Which signals to take (today: 'This page does not list them yet', Pages.tsx:295) | PR 2 GET /api/v2/settings rules.{rule_*, summary, policy, policy_blocks_entries} | D-18 |
| P-36 | control | Built-in entry filters: only these tickers, never these tickers, calls/puts/both, min strike, stop after N losses, skip 0DTE | R wizard Safety steps.tsx:756-788 → /api/safety → rules.apply_settings · chat tickers / skip tickers / calls only / min strike / loss stop / no 0dte agent.py:4967-5024 | POST /api/safety rule_*; POST /api/command | no | planned PR 2+3 | Rules & limits → Which signals to take. ACCEPTANCE: all six fields present (PR 2 a21eb96 v2_rules carries all six rule_* keys) | PR 2 POST /api/v2/settings/rules (direct; spec money list excludes rules) | S-36, D-58 |
| P-37 | control | Written rules (plain-English policy, veto-only) | inline Safety webui.py:1007-1013 · R AI step steps.tsx:297-317 → /api/policy | POST /api/policy; POST /api/safety .policy | no | planned PR 2+3 | Rules & limits → Written rules | PR 2 POST /api/v2/settings/rules {policy} | S-8 |
| P-38 | control | Turn written rules off (when they block every entry because no AI is connected) | R Launch steps.tsx:812-819, 937-951 · chat policy off agent.py:5161 | POST /api/policy/disable; POST /api/command | no | planned PR 2 (route) — PR 3 must show it | Rules & limits → Written rules: 'Turn written rules off' (says 'raises your risk'); Today note ai_needed links there once | PR 2 POST /api/v2/settings/rules {policy:null} → llm_policy.disable_policy() | S-10, D-58 |
| P-39 | control | Expiry-day sell time (on / off / HH:MM ET, 09:30-15:59) and its current reading | chat expiry guard [on\|off\|HH:MM] agent._expiry_command agent.py:4636-4659; reading /api/status fields['expiry guard'] (_expiry_guard_desc :4627) | POST /api/command; GET /api/status | **yes** | missing | Rules & limits → 'Expiry sell time' row (replaces the 'Always on' line Pages.tsx:276 as the one place) | ADD expiry_sell_et to PR 2 POST /api/v2/settings/limits (two-step) over a helper split out of agent._expiry_command; GET /api/v2/settings gains expiry {sell_et, alert_et, effective_today} | D-48, D-53 |
| P-40 | reading | 'Nobody here is an investment adviser… money you can afford to lose' | wizard webui.py:931-935 · steps.tsx:538-543 | — | no | missing | Rules & limits → Trade size confirm dialog (once) | — (copy) | S-31 |
| P-41 | reading | Signal access: days left / pass kind / API key / source; red when lapsed; what the saved source is | H webui.py:1997-1998 feedTile() :2155 · R Dashboard.tsx:641-668 · wizard src-msg webui.py:1562, checkPass :1332 · SourceStep.tsx:75 | GET /api/status fields.source, ['feed access']; GET /api/pass/status; GET /api/state | no | in-v2 | Signals & pass → Signal access (Pages.tsx:338-395); nav days count; Today 'pass' note (merge with agent pass notice by condition signal_pass) | GET /api/v2/status signals.* | D-16, S-16, S-17 |
| P-42 | control | Choose the signal source: AgentHC Day Trade Ideas / my own commands.jsonl / a JSON feed URL | wizard Source webui.py:754-765, doSource :1554 · SourceStep.tsx:176-187, save :144 | POST /api/source | **yes** | missing | Signals & pass → Signal source | NEW POST /api/v2/settings/source {source, url} two-step, over a helper extracted from source() (webui.py:3372-3419); /api/source stays byte-identical | S-11 |
| P-43 | control | Which AgentHC signals: main pick only, or main + 'other trades' | wizard webui.py:767-776 #hc-scope · SourceStep.tsx:188-200 · Sizing steps.tsx:501-505 · chat other trades on\|off agent.py:5234 | POST /api/source .scope; POST /api/source/scope; POST /api/command | **yes** | missing | Signals & pass → Which signals. Rules & limits 'Trade size, other trades' may show one link here when main-only | NEW POST /api/v2/settings/signals {scope:'main'\|'all'} two-step, over source_scope() (webui.py:3897) | S-12, D-55 |
| P-44 | control | AgentHC Premium API key (subscription instead of prepaid days) | wizard webui.py:872-873 #hc-key · SourceStep.tsx:332-337 | POST /api/source .agenthc_key | no | missing | Signals & pass → Signal access ('Subscription key') | Same NEW POST /api/v2/settings/source {agenthc_key} (write-only; GET shows {set,last4}, already in PR 2 signals.api_key) | S-23 |
| P-45 | reading | How signal days are billed (~$10/day in sats, used only when a signal arrives, carry over, never held by the agent) | wizard webui.py:778-792 · SourceStep.tsx:201-221 | — | no | planned PR 3 | Signals & pass → Buy signal days dialog | — | S-13 |
| P-46 | control | Buy 1/5/20/60 signal days (only sizes the feed sells now) | wizard webui.py:793-799 buyPass :1303 · SourceStep.tsx:222-250 | GET /api/pass/options; POST /api/pass/buy | **yes** | planned PR 2+3 | Signals & pass → Buy signal days (replaces 'Add signal days in Settings' Pages.tsx:391) | PR 2 POST /api/v2/pass/buy (two-step); reuse GET /api/pass/options | S-14 |
| P-47 | reading | Pass invoice: quote, QR, copy, open in wallet app, waiting… (3 s poll), 'payment not going through?' help after 2 min | wizard webui.py:801-831 buyPassNow :1308, checkPass :1332 · SourceStep.tsx:75-113, 250-310 | GET /api/pass/status | no | planned PR 3 — acceptance items | Signals & pass → Buy signal days dialog | reuse GET /api/pass/status | S-15 |
| P-48 | reading | 'Would have taken · past N days' preview in practice mode (count, W/BE/L, avg %, per-row skip reason) | R WhyNoTrades/WouldHavePanel Dashboard.tsx:369-434 (shown :334 in dry-run) | GET /api/status would_have (dry_run_preview.summary) | no | missing | Signals & pass → 'What it would have taken' panel (practice mode only) | Extend view_model.py (read-only): signals.preview from dry_run_preview.summary | D-29 |
| P-49 | control | Practice preview on / off (dry_run_preview) | chat preview on\|off agent.py:5149 | POST /api/command | no | missing | Signals & pass → on the preview panel (P-48) | Same NEW POST /api/v2/settings/signals {dry_run_preview:bool} (direct when alone) | D-52 |
| P-50 | control | Create my agent's wallet (hosted LNbits); replace it (old keys saved; sats not moved) | wizard Advanced webui.py:832-838 makeWallet :1503 · SourceStep.tsx:360-525 | POST /api/wallet/create | **yes** | missing | Signals & pass → Payment wallet | NEW POST /api/v2/wallet/create two-step (a replace strands sats), over _wallet_create (webui.py:2935) | S-18 |
| P-51 | reading | Payment wallet: connected, balance in sats, link to the wallet page, 'demo.lnbits.com is a TEST server' warning | wizard showWallet webui.py:1473-1501, :855-864 · SourceStep.tsx:407-420, 599-602 · /dash feed tile wallet sats | GET /api/wallet/balance; GET /api/state .wallet | no | missing | Signals & pass → Payment wallet (today only 'Automatic payment On/Off') | reuse GET /api/wallet/balance; connected flag already in PR 2 GET signals.wallet_connected; wallet-page URL (an LNbits bearer link) only via NEW POST /api/v2/wallet/page on click, never in a GET | S-19, D-16 |
| P-52 | control | Fund the agent's wallet: amount in sats, live quote, invoice with QR / copy / open | wizard webui.py:839-861 fundInvoice :1537 · SourceStep.tsx:392-407, 477-487, 532-590 | GET /api/wallet/quote; POST /api/wallet/fund | **yes** | missing | Signals & pass → Payment wallet | NEW POST /api/v2/wallet/fund two-step over a helper split out of wallet_fund (webui.py:3775); reuse GET /api/wallet/quote | S-20 |
| P-53 | control | Recurring day-pass ON / OFF (agent buys each market day's pass from its wallet) | wizard webui.py:847-852 toggleRecurring :1272 · SourceStep.tsx:489-492 | POST /api/wallet/recurring | **yes** | missing (route planned PR 2) | Signals & pass → Payment wallet (PR 3 keeps wallet read-only, so the control is PR 4's) | PR 2 POST /api/v2/wallet/recurring (two-step both ways; a21eb96) | S-21 |
| P-54 | control | Use my own LNbits wallet (instance URL + admin key), replacing the agent's wallet | wizard webui.py:866-870 · SourceStep.tsx:323-331 | POST /api/source .lnbits_url, .lnbits_key | **yes** | missing | Signals & pass → Payment wallet ('Use my own wallet') | NEW POST /api/v2/wallet/connect two-step over a helper split out of source() (:3391-3406 + _use_pasted_wallet); admin key write-only | S-22 |
| P-55 | reading | Broker connected: name, account ····1234, cash | wizard webui.py:895, :1386-1390 · steps.tsx:388-417 · /api/status ['buying power'], ['broker connection'] | GET /api/state .broker; GET /api/status | no | in-v2 | Settings → Connections → Broker row + vitals | GET /api/v2/status broker | S-27, D-50 |
| P-56 | control | Connect a key-based broker (pick platform, fill fields, Connect); replace with confirmation | wizard webui.py:905-913 brkConnect :1588 · steps.tsx:339-356, 426-463 | GET /api/brokers; POST /api/broker/connect | **yes** | planned PR 2+3 | Settings → Connections → Broker | PR 2 POST /api/v2/broker/connect (two-step); reuse GET /api/brokers | S-26 |
| P-57 | control | Connect Robinhood (OAuth sign-in in a new tab; page waits until saved) | wizard webui.py:898-903 rhStart :1599 · steps.tsx:358-375 · GET /callback webui.py:4279-4293 | GET /api/rh/start; GET /callback | **yes** | missing | Settings → Connections → Broker (same panel as P-56: one home for 'connect a broker') | NEW POST /api/v2/broker/robinhood/start two-step (preview names the broker it replaces) over rh_start; /callback unchanged, lands on the remembered dashboard (4b) | S-24 |
| P-58 | reading | Robinhood: 'Options are NOT enabled on your Agentic account' + link | wizard webui.py:1611-1612 · steps.tsx:424 | GET /api/state .rh_warning | no | missing | Settings → Connections → Broker row | GET /api/v2/settings gains broker.warning | S-25 |
| P-59 | control | AI helper: paste key (provider detected) or pick provider / local endpoint, find models, pick one, Connect (one test call) | wizard webui.py:705-751 llmFind :1437, doLLM :1458 · steps.tsx:117-296 | GET /api/llm/providers; POST /api/llm/models; POST /api/llm | no | planned PR 2+3 | Settings → Connections → AI helper | PR 2 POST /api/v2/settings/ai; reuse GET /api/llm/providers, POST /api/llm/models (read-only) | S-4, S-5 |
| P-60 | reading | Which AI is connected (provider + model) | wizard webui.py:1376-1378 · steps.tsx:191, 236-238 | GET /api/state .llm | no | planned PR 2+3 | Settings → Connections → AI helper | PR 2 GET /api/v2/settings ai.* | S-6 |
| P-61 | control | Alerts: Discord webhook, ntfy topic, Telegram token + chat id | wizard webui.py:993-1006 · steps.tsx:789-804 | POST /api/safety | no | planned PR 2+3 | Settings → Connections → Alerts | PR 2 POST /api/v2/settings/alerts (null clears) | S-38 |
| P-62 | reading | Which alert targets are set | never shown in classic | — | no | planned PR 2+3 | Settings → Connections → Alerts | PR 2 GET /api/v2/settings alerts {set,last4} | S-39 |
| P-63 | control | Expiry alert time (on / off / HH:MM): one urgent notification before the expiry sell | chat expiry alert [on\|off\|HH:MM] agent.py:4639 | POST /api/command | no | missing | Settings → Connections → Alerts ('Warn me on expiry day at…'); its reading sits next to the sell time in Rules (P-39) as text only | ADD expiry_alert_et to PR 2 POST /api/v2/settings/alerts (direct) over the same helper as P-39 | D-54 |
| P-64 | control | Start at login (state + switch) | wizard launch checkbox webui.py:1034-1036 · steps.tsx:981-983 · chat autostart on\|off agent.py:5123 · /api/status ['start at login'] | POST /api/start-local .autostart; GET/POST /api/autostart; POST /api/command | no | planned PR 2+3 | Settings → This computer | PR 2 POST /api/v2/settings/computer {autostart}; GET /api/v2/settings computer.autostart | S-44, D-49, D-59 |
| P-65 | control | Keep this computer awake | R wizard steps.tsx:737-742 · chat keep awake on\|off agent.py:5128 | POST /api/safety .keep_awake; POST /api/command | no | planned PR 2+3 | Settings → This computer | PR 2 POST /api/v2/settings/computer {keep_awake} | S-37, D-59 |
| P-66 | control | Open the dashboard after login (open_dashboard_at_login) | chat open at login on\|off agent.py:5136-5148 · R chip Dashboard.tsx:1175 | POST /api/command | no | built PR 4 (group 3) | Settings → This computer, under Start at login | ADD open_dashboard_at_login to PR 2 POST /api/v2/settings/computer (direct; same config key the chat writes) and to GET | D-51, S-45 |
| P-67 | control | Move to another computer, step 1-2: passphrase (8+ chars, twice) → encrypted .atmove file → download / download again; instructions | R header button Dashboard.tsx:250 → MovePanel.tsx:20-36, 54-101 (not in inline pages) | POST /api/move/export; GET /api/move/download | no | built PR 4 (group 3) | Settings → This computer → Move to another computer (the ONLY Move control in /v2) | reuse POST /api/move/export and GET /api/move/download as is | D-8, D-43, D-45, S-48 |
| P-68 | control | Move step 3: 'I saved the file' → retire this computer and stop the agent here | R MovePanel.tsx:38-44, 102-134 (ConfirmDialog) | POST /api/move/finish | **yes** | built PR 4 (group 3) | Settings → This computer → Move to another computer | NEW POST /api/v2/move/finish two-step over move_finish (webui.py:4022) | D-44, S-49 |
| P-69 | control | Move to a new cloud server: DigitalOcean token + region → provisions, copies setup, retires this computer | wizard Launch Option B webui.py:1040-1052 deploy :1698 · steps.tsx:861-920 · handler webui.py:3496-3626 | POST /api/deploy | **yes** | built PR 4 (group 3) | Settings → This computer → Move to another computer, second option (one section for both moves) | NEW POST /api/v2/move/deploy two-step over deploy() (lock + moved-away refusals unchanged); token write-only | S-46 |
| P-70 | reading | Cloud move progress: status → IP, ssh tunnel command, cloud-init log hint, 'this computer is now retired' | wizard poll2 webui.py:1705-1720 · steps.tsx:868-918 | GET /api/deploy/status; POST /api/finish | no | built PR 4 (group 3) | Settings → This computer → Move (with P-69) | reuse GET /api/deploy/status; /v2 does NOT call /api/finish (wizard-server lifecycle only) | S-47 |
| P-71 | control | Switch dashboards: 'Dashboard: New \| Classic' (remembered server-side; '/' and post-setup land there) | R classic Dashboard.tsx:274-276 'Try the new dashboard' · inline _DASH_HTML has none (add one <a href=/v2> after webui.py:1977) · landing W.landing webui.py:3313, '/' :2432 | page /v2, /dash | no | built PR 4 (group 1) | /v2 Settings → This dashboard (only control; becomes the action of the single 'Back to the classic dashboard'). Remove App.tsx:327 sidebar, Pages.tsx:624 More links; keep App.tsx:454 Waiting fallback (no status = no Settings) | ADD dashboard:'classic'\|'v2' to PR 2 POST /api/v2/settings/computer (direct); classic link → same route | D-10, S-57 |
| P-72 | control | Update available: review commits, install exactly that commit, restart, reload | H #upd-badge webui.py:1975 (→ /setup) · wizard webui.py:682-686 · R UpdateBox.tsx:37-82, Dashboard.tsx:190-194 | GET /api/update/check; POST /api/update/pull | **yes** | in-v2 | UpdateDialog (#80): Settings update row is the home; band badge is the one allowed pointer (spec: 'stays as it is') | reuse as is (lib/update.ts) | D-5, S-51 |
| P-73 | reading | Settings entry point from the dashboard | H webui.py:1977 href=/setup · R Dashboard.tsx:270 | page /setup | no | planned PR 3 | /v2 Settings tab ('must never take the owner out of /v2') | — | D-7 |
| P-74 | reading | Proposed code change: explanation, warnings, diff | H webui.py:2007-2019 #p-proposal · R ProposalPanel Dashboard.tsx:1096-1155 | GET /api/proposal | no | missing | Settings → Code changes; Today shows one note (new condition proposal_pending) linking there | reuse GET /api/proposal; view-model gains proposal {pending, diff_sha256} | D-19 |
| P-75 | control | Apply the code change and restart | H webui.py:2013 propApply() :2311 · R Dashboard.tsx:1128-1137 | POST /api/proposal/apply | **yes** | missing | Settings → Code changes | NEW POST /api/v2/proposal/apply two-step, token bound to sha256 of the pending diff → self_edit.apply_and_restart | D-20 |
| P-76 | control | Reject the code change | H webui.py:2014 propReject() :2316 · R Dashboard.tsx:1138-1145 | POST /api/proposal/reject | no | missing | Settings → Code changes | reuse POST /api/proposal/reject | D-21 |
| P-77 | control | Allow AI code proposals (self-edit on / off) | chat self-edit on\|off agent.py:5272-5281 · wizard broker hint steps.tsx:464-472 | POST /api/command | no | missing | Settings → Code changes | NEW POST /api/v2/settings/code {self_edit_enabled:bool} (direct); GET /api/v2/settings code.self_edit_enabled | D-61, S-28 |
| P-78 | control | Ask the AI to write a code change ('code: …', e.g. a connector for another broker) | chat 'code: <request>' agent.py:5035 → self_edit.propose · wizard broker hint webui.py:914-924, steps.tsx:464-472 | POST /api/command | no | missing | Settings → Code changes 'Describe a change' (NOT the Ask box). Broker panel shows one plain link here ('Don't see your platform?') | NEW POST /api/v2/proposal/request {request} → self_edit.propose directly (never apply_command); proposing changes nothing | D-62, S-28 |
| P-79 | control | Check my connection (AgentHC / GitHub / broker reachable?) | H webui.py:1976 #help-btn, :2066 helpNet :2104 · R HelpPanel Dashboard.tsx:1219-1248 | GET /api/help/netcheck | no | built PR 4 (group 4) | Settings → Help and support | reuse GET /api/help/netcheck | D-6, D-41, S-52 |
| P-80 | control | Copy support report (keys, tokens, invoices, wallet ids removed) | H webui.py:2067 helpReport :2105-2110 · R Dashboard.tsx:1249-1257 | GET /api/help/report | no | built PR 4 (group 4) | Settings → Help and support | reuse GET /api/help/report | D-42, S-53 |
| P-81 | control | Tip AgentHC: tiers in $ priced in sats, Lightning invoice + QR, copy/open, wait for paid / expired | R header Dashboard.tsx:254-265 → TipPanel.tsx:66-348 (not in inline pages) | GET /api/tip/options; POST /api/tip/create; POST /api/tip/status | no | built PR 4 (group 4) | Settings → Help and support → 'Tip AgentHC' (dialog) | reuse the three tip routes (an invoice the owner pays from their own wallet; the agent spends nothing) | D-9, D-46, D-47, S-54 |
| P-82 | reading | Agreement on file: signed date / terms version, and the agreement text to re-read | wizard restoredNote webui.py:1373-1375 · steps.tsx:50 | GET /api/state .notes.consent, .disclaimer | no | built PR 4 (group 4) | Settings → Help and support → 'Your agreement' row with a disclosure | GET /api/v2/settings gains consent {accepted, accepted_at, terms_version}; NEW GET /api/v2/agreement (text only). Never reuse /api/state (it carries the wallet page URL) | S-2 |
| P-83 | reading | Agreement changed since you accepted (consent_ok() false after a TERMS_VERSION bump) | wizard Agreement step webui.py:690-703 · agent.py:1356 | GET /api/state .consent | no | missing | Today note (new condition consent_needed) with one link to /setup?from=v2 (acceptance stays in the wizard) | view-model condition consent_needed; no new write route | S-1 |
| P-84 | control | Accept the agreement (tick, type the phrase, Accept) | webui.py:690-703 doConsent :1405 · steps.tsx:23-109 | POST /api/consent | no | first-run-only | /setup (P-83 links back when terms change) | — | S-1 |
| P-85 | control | Wizard step list, Back / Continue, already-configured ticks | webui.py:1095-1156 · Wizard.tsx:160, 200-330 | GET /api/state | no | first-run-only | /setup | — | S-3 |
| P-86 | control | Skip the AI step | webui.py:750 skipLLM · steps.tsx:200-203 | — | no | first-run-only | /setup | — | S-7 |
| P-87 | control | 'Claude API key — only if you skipped step 2' on the Safety step | inline only webui.py:1014-1017 | POST /api/safety .anthropic_key | no | n/a | None: would be a second home for the AI key; the one home is P-59 (React wizard already dropped it) | — | S-9 |
| P-88 | control | Finish setup (+ terminal commands to start the agent) | webui.py:1020-1022 doSafety :1657-1680 · steps.tsx:703-706, 964-973 | POST /api/safety (.next) | no | first-run-only | /setup | — | S-40 |
| P-89 | control | First launch: 'Start my agent now' (+ start at login), then hop to the dashboard | webui.py:1029-1039 startLocal :1681-1697 · steps.tsx:834-859 | POST /api/start-local; GET /api/status | no | first-run-only | /setup; afterwards lands on the remembered dashboard (4b). Later restarts: P-08 | — | S-42 |
| P-90 | control | Bring a whole setup from another computer (.atmove + passphrase) | R wizard Wizard.tsx:173 + MoveImport.tsx:18-100 | POST /api/move/import | **yes** | first-run-only | /setup (the receiving computer's first run). /v2 Move section shows one plain link 'Bringing a setup here?' → /setup?from=v2 | — | S-50, D-45 |
| P-91 | control | Setup assistant (answers about setup steps) | webui.py:1059-1071 ask :1722 · Wizard.tsx:369-400 · handler webui.py:4232 | POST /api/ask | no | first-run-only | /setup (its prompt is about setup); /v2 uses P-15 | — | S-55 |
| P-92 | control | Wizard 'Dashboard' links and post-start redirect | webui.py:662, :667 · Wizard.tsx:143 · steps.tsx:851 location='/dash' | — | no | planned PR 3 + 4b | ?from=v2 returns to /v2 (PR 3); without it, the remembered dashboard (4b) | — | S-57 |
| P-93 | control | Show-only chat commands: status, rules, autostart, start at login, expiry guard, expiry alert | agent.py:5065 _SHOW_COMMANDS, :5156, :5282 | POST /api/command | no | n/a | Each reading has its /v2 home (band/vitals/Today, P-35, P-64, P-39, P-63) | — | D-63 |

## Server routes (`W.routes_get` / `W.routes_post`, webui.py:4212-4268)

Input for the 4a.5 test (`tests/test_v2_parity.py::test_every_route_has_a_v2_caller_or_a_classic_only_reason`): every key must be *v2* (the committed /v2 bundle calls it) or *classic* (listed with its reason). A route added later with neither fails the test. While PR 4 is being built, a row whose /v2 caller a later group adds says *pending: group N*; the test checks that the bundle does not call it yet, and PR 4 is finished only when no *pending* row is left.

| Route | Method | What | After PR 4 | Kind |
|---|---|---|---|---|
| `/api/state` | GET | wizard state (consent, source, broker, llm, wallet incl. LNbits page URL, notes) | classic: wizard only; /v2 reads GET /api/v2/settings + /api/v2/status, and this payload carries the wallet page bearer URL (P-51, P-82) | classic |
| `/api/rh/start` | GET | Robinhood OAuth start URL | classic: wrapped by NEW POST /api/v2/broker/robinhood/start (P-57) | classic |
| `/api/wallet/balance` | GET | wallet balance in sats | /v2 Signals & pass → Payment wallet (P-51) | v2 |
| `/api/deploy/status` | GET | cloud deploy progress | /v2 Settings → This computer → Move (P-70) | v2 |
| `/api/brokers` | GET | key-based broker list + fields | /v2 Settings → Connections → Broker (P-56) | v2 |
| `/api/broker/cash` | GET | broker buying power + warning | classic: the view-model already carries limits.cash + broker.buying_power (P-13) | classic |
| `/api/update/check` | GET | update check | /v2 lib/update.ts:36 (P-72) | v2 |
| `/api/help/netcheck` | GET | connection check | /v2 Settings → Help and support (P-79) | pending: group 4 |
| `/api/help/report` | GET | scrubbed support report | /v2 Settings → Help and support (P-80) | pending: group 4 |
| `/api/wallet/quote` | GET | sats ↔ $ quote | /v2 Signals & pass → Payment wallet (P-52) | pending: group 7 |
| `/api/llm/providers` | GET | AI provider list | /v2 Settings → Connections → AI helper (P-59) | v2 |
| `/api/pass/status` | GET | pass / invoice status | /v2 Buy signal days dialog (P-47) | v2 |
| `/api/pass/options` | GET | pass sizes on sale | /v2 Buy signal days dialog (P-46) | v2 |
| `/api/tip/options` | GET | tip tiers | /v2 Settings → Help and support → Tip (P-81) | pending: group 4 |
| `/api/status` | GET | classic status payload | classic: classic page payload; every reading it has that /v2 lacks moves into /api/v2/status (P-26, P-33, P-39, P-48) | classic |
| `/api/trades` | GET | trade log page | classic: /v2 uses /api/v2/activity (P-18, P-19) | classic |
| `/api/pnl` | GET | all-time P&L | classic: /v2 uses view-model results.* (P-21..P-26) | classic |
| `/api/v2/status` | GET | view-model | /v2 useStatus.ts:124, useUpdate.ts:36 | v2 |
| `/api/v2/activity` | GET | older activity | /v2 useStatus.ts:179 | v2 |
| `/api/v2/settings` | GET | typed settings, secret-free (PR 2; PR 4 adds expiry, open at login, dashboard, preview switch, self-edit, broker warning, consent) | /v2 useSettings.ts | v2 |
| `/api/v2/agreement` | GET | the agreement's text (P-82) | /v2 Settings → Help and support | pending: group 4 |
| `/api/proposal` | GET | pending code proposal | /v2 Settings → Code changes (P-74) | pending: group 5 |
| `/api/autostart` | GET | start-at-login state | classic: no page caller even at origin/main; /v2 reads GET /api/v2/settings computer.autostart (P-64) | classic |
| `/api/move/download` | GET | download the .atmove file | /v2 Settings → This computer → Move (P-67) | v2 |
| `/api/consent` | POST | accept the agreement | classic: first-run wizard (P-84); /v2 links /setup?from=v2 when terms change (P-83) | classic |
| `/api/source` | POST | save source / scope / own wallet / API key | classic: wrapped by NEW /api/v2/settings/source and /api/v2/wallet/connect over helpers split out of source() (P-42, P-44, P-54) | classic |
| `/api/source/scope` | POST | main / all signals | classic: wrapped by NEW /api/v2/settings/signals {scope} (P-43) | classic |
| `/api/sizing` | POST | trade size per book | classic: wrapped by PR 2 /api/v2/settings/limits (P-28, P-29) | classic |
| `/api/safety` | POST | mode, cap, loss, rules, alerts, keep awake, policy | classic: wrapped by PR 2 limits / rules / alerts / computer and /api/v2/command set_mode | classic |
| `/api/finish` | POST | release the setup server's wait | classic: wizard-server lifecycle; the /v2 cloud move does not call it (P-70) | classic |
| `/api/ask` | POST | setup assistant | classic: setup-step prompt (P-91); /v2 uses NEW /api/v2/ask (P-15) | classic |
| `/api/policy/disable` | POST | written rules off | classic: wrapped by PR 2 /api/v2/settings/rules {policy:null} (P-38) | classic |
| `/api/start-local` | POST | start the agent (+autostart) | classic: first launch (P-89); /v2 restart via NEW /api/v2/agent/start (P-08) | classic |
| `/api/policy` | POST | save written rules | classic: wrapped by PR 2 /api/v2/settings/rules (P-37) | classic |
| `/api/move/export` | POST | create the move file | /v2 Settings → This computer → Move (P-67) | v2 |
| `/api/move/finish` | POST | retire this computer | classic: wrapped by NEW /api/v2/move/finish two-step (P-68) | classic |
| `/api/move/import` | POST | import a move file | classic: first run on the receiving computer (P-90) | classic |
| `/api/autostart` | POST | start at login on/off | classic: no page caller; PR 2 /api/v2/settings/computer calls the same autostart_set (P-64) | classic |
| `/api/wallet/create` | POST | create / replace hosted wallet | classic: wrapped by NEW /api/v2/wallet/create two-step (P-50) | classic |
| `/api/pass/buy` | POST | pass invoice | classic: wrapped by PR 2 /api/v2/pass/buy (P-46) | classic |
| `/api/tip/create` | POST | tip invoice | /v2 Settings → Help and support → Tip (P-81) | pending: group 4 |
| `/api/tip/status` | POST | tip paid? | /v2 Tip dialog (P-81) | pending: group 4 |
| `/api/wallet/fund` | POST | wallet funding invoice | classic: wrapped by NEW /api/v2/wallet/fund two-step (P-52) | classic |
| `/api/llm` | POST | connect AI helper | classic: wrapped by PR 2 /api/v2/settings/ai (P-59) | classic |
| `/api/llm/models` | POST | list models for a key | /v2 Settings → Connections → AI helper (P-59) | v2 |
| `/api/deploy` | POST | create cloud server | classic: wrapped by NEW /api/v2/move/deploy two-step (P-69) | classic |
| `/api/update/pull` | POST | install update | /v2 lib/update.ts:37 (P-72) | v2 |
| `/api/wallet/recurring` | POST | recurring day-pass | classic: wrapped by PR 2 /api/v2/wallet/recurring (P-53) | classic |
| `/api/broker/connect` | POST | connect key broker | classic: wrapped by PR 2 /api/v2/broker/connect (P-56) | classic |
| `/api/proposal/apply` | POST | apply code change + restart | classic: wrapped by NEW /api/v2/proposal/apply two-step (P-75) | classic |
| `/api/proposal/reject` | POST | reject code change | /v2 Settings → Code changes (P-76) | pending: group 5 |
| `/api/command` | POST | free-text command / question | classic: after PR 3 step 6 + {action:'stop'} (P-07) and /api/v2/ask (P-15), /v2 sends no free text | classic |
| `/api/v2/settings/limits` | POST | trade size, cap, loss limit, positions (two-step) | /v2 Rules & limits | v2 |
| `/api/v2/settings/rules` | POST | built-in filters, written rules | /v2 Rules & limits | v2 |
| `/api/v2/settings/alerts` | POST | alert targets | /v2 Settings → Connections | v2 |
| `/api/v2/settings/computer` | POST | start at login, keep awake, open at login (P-66), dashboard switch (P-71) | /v2 Settings → This computer / This dashboard | v2 |
| `/api/v2/settings/ai` | POST | AI helper | /v2 Settings → Connections | v2 |
| `/api/v2/broker/connect` | POST | key-based broker (two-step) | /v2 Settings → Connections | v2 |
| `/api/v2/broker/robinhood` | POST | Robinhood sign-in (two-step; PARITY's /api/v2/broker/robinhood/start) | /v2 Settings → Connections | v2 |
| `/api/v2/pass/buy` | POST | signal days invoice (two-step) | /v2 Signals & pass | v2 |
| `/api/v2/wallet/recurring` | POST | recurring day-pass (two-step) | /v2 Signals & pass → Payment wallet (P-53) | pending: group 7 |
| `/api/v2/command` | POST | pause, resume, practice, LIVE (two-step), stop (two-step, P-07) | /v2 band; Settings → The agent on this computer | v2 |
| `/api/v2/agent/start` | POST | start the agent (start_local; two-step when the saved mode is LIVE, P-08) | /v2 Settings → The agent on this computer | v2 |
| `/api/v2/move/finish` | POST | retire this computer after the move file is saved (two-step, P-68) | /v2 Settings → This computer → Move | v2 |
| `/api/v2/move/deploy` | POST | move to a new cloud server (two-step, P-69) | /v2 Settings → This computer → Move | v2 |

Page paths (not in the route tables):

| Path | Today | After PR 4 |
|---|---|---|
| `/` | landing redirect → W.landing (webui.py:2432, :3313) | 4b: remembered dashboard (P-71) |
| `/setup, /dash` | React build, else inline _WIZARD_HTML/_DASH_HTML (webui.py:2437-2444; ?ui=classic forces inline) | unchanged; /dash gains one 'Try the new dashboard' link in _DASH_HTML (P-71) |
| `/v2` | v2 build, else redirect to landing (webui.py:2445-2457) | unchanged |
| `/assets/*` | hashed build assets | unchanged |
| `/callback` | Robinhood OAuth return, before the cookie check (Host guard only) → 302 '/' (webui.py:4279-4293) | unchanged; lands on the remembered dashboard via '/'. Do not widen |

### New or extended typed routes PR 4 adds

Every route below is registered in `W.routes_get`/`W.routes_post`, so it inherits the Host/Origin guard and cookie auth. Each one calls the same function as the classic path. Two-step means the PR 2 `_two_step` token, bound to the exact payload and to the settings fingerprint.

| Route | Calls | Confirmation | Rows |
|---|---|---|---|
| `POST /api/v2/ask` | answer-only → _ask_llm (same args as d_command's fallback); never apply_command | direct | P-15, P-16 |
| `POST /api/v2/command +{action:'stop'}` | CONTROLS stop via the fixed string 'stop' | two-step | P-07 |
| `POST /api/v2/agent/start` | start_local (same refusals) | two-step when saved mode is LIVE | P-08 |
| `POST /api/v2/settings/computer +open_dashboard_at_login, +dashboard` | same config keys as the chat command / 4b | direct | P-66, P-71 |
| `POST /api/v2/settings/limits +expiry_sell_et` | helper split out of agent._expiry_command | two-step | P-39 |
| `POST /api/v2/settings/alerts +expiry_alert_et` | same helper | direct | P-63 |
| `POST /api/v2/settings/signals {scope, dry_run_preview}` | source_scope(); chat 'preview on\|off' key | two-step when scope is sent | P-43, P-49 |
| `POST /api/v2/settings/source {source, url, agenthc_key}` | helper split out of source() | two-step | P-42, P-44 |
| `POST /api/v2/settings/code {self_edit_enabled}` | same key as chat 'self-edit on\|off' | direct | P-77 |
| `POST /api/v2/proposal/request {request}` | self_edit.propose | direct (proposing changes nothing) | P-78 |
| `POST /api/v2/proposal/apply` | self_edit.apply_and_restart; token bound to sha256(diff) | two-step | P-75 |
| `POST /api/v2/move/finish` | move_finish | two-step | P-68 |
| `POST /api/v2/move/deploy` | deploy() | two-step | P-69 |
| `POST /api/v2/wallet/create` | _wallet_create | two-step | P-50 |
| `POST /api/v2/wallet/fund` | helper split out of wallet_fund | two-step | P-52 |
| `POST /api/v2/wallet/connect` | helper split out of source() + _use_pasted_wallet | two-step | P-54 |
| `POST /api/v2/wallet/page` | returns the LNbits wallet page URL on click (never in a GET) | direct | P-51 |
| `POST /api/v2/broker/robinhood/start` | rh_start | two-step | P-57 |
| `GET /api/v2/agreement` | A._disclaimer_text() | read-only | P-82 |
| `GET /api/v2/settings +expiry, +computer.open_dashboard_at_login/dashboard, +signals.dry_run_preview, +code.self_edit_enabled, +broker.warning, +consent` | read-only, secret-free | read-only | P-39, P-58, P-66, P-71, P-77, P-82 |
| `GET /api/v2/status (view-model) +results.real.paid_days/days[].paid, +limits.budget_hints, +signals.preview, +proposal.pending, conditions +budget_too_small/+proposal_pending/+consent_needed` | read-only, from the readers the classic /api/status and /api/pnl use | read-only | P-26, P-33, P-48, P-74, P-83 |

## Missing: build list

Grouped by the /v2 page that owns each item, in build order. Server work comes first in each group, then the UI. Every item names the test that proves parity: route equivalence as in PR 2 for writes (classic path vs v2 path from the same starting config gives a deep-equal `config.json`, the same policy file and the same side-effect calls on stubs), or a fixture render test for readings. Every new route also gets the PR 2 checks: 401 without the cookie, 403 for a bad Host or Origin, and nothing saved without a valid token.

### 0. Groundwork (server only, no UI)
1. **View-model readings** (`view_model.py`, read-only):
   - `results.real.paid_days {days, pnl}` + `days[].paid` (P-26)
   - `limits.budget_hints` (P-33)
   - `signals.preview` (P-48)
   - `proposal {pending, diff_sha256}` (P-74)
   - conditions `budget_too_small`, `proposal_pending`, `consent_needed` (P-33, P-74, P-83), added to PR 2's `CONDITIONS`

   *Tests:*
   - Paid-day P&L equals `A.pnl_summary` passes on a seeded trade log.
   - `budget_hints` equals `agent._budget_hints(cfg)`.
   - `preview` equals `dry_run_preview.summary(cfg)`.
   - Each new condition appears once in a fixture and never when false.
2. **`GET /api/v2/settings` additions**: `expiry {sell_et, alert_et, effective_today}`, `computer.open_dashboard_at_login`, `computer.dashboard`, `signals.dry_run_preview`, `code.self_edit_enabled`, `broker.warning`, `consent {accepted, accepted_at, terms_version}`; plus `GET /api/v2/agreement`.

   *Tests:*
   - Each value equals the classic reader (`_expiry_guard_desc`, `/api/state .rh_warning`, `.notes.consent`).
   - The PR 2 secret test is extended: seed `lnbits_wallet_page`, the LNbits admin key, a DO token and an AgentHC key; none appears in any GET body.
3. **Route-coverage test (4a.5)**: parse `W.routes_get`/`W.routes_post` from a built app. For each key, assert that either the `webui_dist` v2 bundle contains the path, or the key appears in the *classic* rows of the Server routes table in `webui_app/PARITY.md`.

### 1. Settings → This dashboard: the switch (4b)
- **P-71**:
  - Add `dashboard: "classic"|"v2"` to `POST /api/v2/settings/computer` (direct save).
  - `/` and the post-setup redirect read it. Fall back to classic when the v2 build is missing.
  - `_DASH_HTML` gets one `<a href="/v2">Try the new dashboard</a>` after webui.py:1977 (React /dash already has it at Dashboard.tsx:274).
  - In /v2, the single "Back to the classic dashboard" row becomes "Dashboard: New | Classic". Remove the sidebar link (App.tsx:327) and the More link (Pages.tsx:624). Keep the Waiting fallback link (App.tsx:454): with no status there is no Settings to reach.

  *Tests (spec 4b):*
  - Default lands on `/dash`.
  - After choosing v2, `/` → `/v2`; choosing classic restores it; an unknown value is rejected.
  - `/dash` and `/v2` both serve either way.
  - No v2 build → classic.
  - The classic page's only diff is the one link (snapshot of `_DASH_HTML` minus the link equals origin/main).

### 2. Settings → The agent on this computer
- **P-07 Stop**: add `{action:"stop"}` to `/api/v2/command` (fixed string `stop`, two-step; the preview counts real positions that will no longer be sold). Move `Dialogs.tsx:280` off free-text `/api/command`.

  *Test:* v2 stop vs chat `stop` → same `CONTROLS["stop"]` and config. No token → no stop.
- **P-08 Start**: `POST /api/v2/agent/start` over `start_local` (two-step when the saved mode is LIVE). It replaces the three "Open setup to start it" links (Band.tsx:155, Today.tsx:48, Pages.tsx:512). The Today `agent_stopped` note carries the one link to Settings when it is shown; otherwise the band run-state links there.

  *Tests:*
  - v2 start vs `/api/start-local` on stubs → same supervisor call.
  - The same refusals (no consent, moved-away, policy blocks).
  - LIVE without a token → not started.
  - A fixture shows no `/setup` href for start.

### 3. Settings → This computer
- **P-66 Open dashboard at login**: extend `/api/v2/settings/computer`.

  *Test:* equivalence vs chat `open at login on|off`.
- **P-67 Move, create and download**: reuse `/api/move/export` and `/api/move/download`.

  *Test:* a fixture render where the form posts to exactly those paths. The passphrase rules are the same as MovePanel (8+ characters, typed twice).
- **P-68 Move, finish**: `POST /api/v2/move/finish`, two-step.

  *Test:* v2 vs `/api/move/finish` → same moved-away marker, autostart off and agent stop on stubs. No token → nothing.
- **P-69 / P-70 Move to a cloud server**: `POST /api/v2/move/deploy`, two-step, token write-only. Progress reuses `/api/deploy/status`; /v2 never calls `/api/finish`.

  *Tests:*
  - A stubbed DigitalOcean client is called identically by `/api/deploy` and by the confirmed v2 call.
  - The lock and moved-away refusals are unchanged.
- **P-90 import**: one plain link, "Bringing a setup to this computer?" → `/setup?from=v2`.

  *Test:* the redundancy test finds exactly one Move control in /v2.

### 4. Settings → Help and support
- **P-79 / P-80** connection check and support report (reuse).
- **P-81** Tip dialog (reuse the three tip routes).
- **P-82** agreement row with `GET /api/v2/agreement`.

*Tests:*
- Fixture renders call exactly those routes.
- The tip flow never calls a wallet or pay route.
- The agreement text equals `A._disclaimer_text()`.

### 5. Settings → Code changes
- **P-74** review panel: reuse `GET /api/proposal`. Today shows one `proposal_pending` note linking here.
- **P-75** Apply: `POST /api/v2/proposal/apply`, two-step, token bound to sha256 of the diff.

  *Tests:*
  - No token → not applied.
  - The diff changes after the preview → refused.
  - The confirmed call reaches `self_edit.apply_and_restart` exactly as `/api/proposal/apply` does (stub).
- **P-76** Reject: reuse.
- **P-77** Self-edit switch: `POST /api/v2/settings/code`.

  *Test:* equivalence vs chat `self-edit on|off`.
- **P-78** Describe a change: `POST /api/v2/proposal/request` → `self_edit.propose`.

  *Test:* the request text `pause` / `dry off` is proposed and never executed: `apply_command` is not called and the config is unchanged.

### 6. Settings → Connections
- **P-57** Robinhood: `POST /api/v2/broker/robinhood/start`, two-step, over `rh_start`.

  *Test:* the confirmed call returns the same auth URL as `GET /api/rh/start` (stubbed OAuth). `/callback` is untouched.
- **P-58** options warning: `GET /api/v2/settings broker.warning`, fixture render.
- **P-63** Expiry alert time: `expiry_alert_et` on `/api/v2/settings/alerts`, direct.

  *Tests:* equivalence vs chat `expiry alert 15:40` and `expiry alert off`. `16:00` and `09:00` are rejected with the allowed range.

### 7. Signals & pass
- **P-42 / P-44** source and subscription key: `POST /api/v2/settings/source`, two-step, over a helper extracted from `source()`; `/api/source` stays byte-identical.

  *Test:* the same payload through both gives a deep-equal config.
- **P-43** scope: `POST /api/v2/settings/signals {scope}`, two-step.

  *Test:* equivalence vs `/api/source/scope` and vs chat `other trades on`.
- **P-48 / P-49** preview panel (practice only) and its switch: `{dry_run_preview}`, direct.

  *Tests:*
  - Fixture render (practice shows it, LIVE does not).
  - Equivalence vs chat `preview on|off`.
- **Payment wallet panel**:
  - P-51 balance: reuse `/api/wallet/balance`; the page URL comes only from `POST /api/v2/wallet/page`.
  - P-50 create / replace: `POST /api/v2/wallet/create`.
  - P-52 fund: `POST /api/v2/wallet/fund` + reuse `/api/wallet/quote`.
  - P-53 recurring: PR 2 route.
  - P-54 own LNbits: `POST /api/v2/wallet/connect`.

  *Tests:*
  - Each write is equivalent to its classic route on a stubbed LNbits (same config, same old-keys file).
  - No token → no wallet call.
  - The wallet page URL never appears in a GET.

### 8. Rules & limits
- **P-39** Expiry sell time: `expiry_sell_et` on `/api/v2/settings/limits`, two-step. It becomes the one place for it, replacing the "Always on" line at Pages.tsx:276.

  *Tests:*
  - Equivalence vs chat `expiry guard 15:30`, `expiry guard on` and `expiry guard off`.
  - Range 09:30-15:59.
  - No token → not saved.
- **P-33 / P-34** budget hint in the Trade size panel, plus a "Use $S" button that pre-fills the row's form (no route of its own).

  *Tests:* fixture render; the pre-filled save is the same `/api/v2/settings/limits` payload as typing $S.
- **P-40** adviser disclaimer inside the trade-size confirm dialog.

  *Test:* fixture text.
- **PR 3 acceptance checks owned here:**
  - P-36: all six built-in filter fields are present.
  - P-38: a "Turn written rules off" button exists (`{policy:null}`).
  - P-35: rules are listed (no "does not list them yet").

### 9. Results
- **P-26** "Result on paid signal days" row and the per-day paid marker.

  *Test:* fixture where the paid-day total equals the seeded `/api/pnl` passes figure.
- **P-24** caption "by the day it closed".

### 10. Today and Activity
- **P-15 / P-16** Ask box + conversation log: `POST /api/v2/ask`.

  *Tests:*
  - Answers through stubbed `_ask_llm` with the same status fields and trades as `d_command`'s fallback.
  - The texts `pause`, `dry off`, `stop`, `set budget 1` and `code: x` change nothing: config, `CONTROLS` and the proposal are untouched, and `apply_command` is not called.
- **P-14** group Problems by kind in Activity (×count, last time).

  *Test:* fixture with 3 problems of 2 kinds renders 2 groups.
- **P-83** `consent_needed` note → `/setup?from=v2`.

  *Test:* fixture.

### 11. Redundancy fixes this inventory found (fold into the PR 3 audit if it lands first)
- **Low cash said twice on Today**: the agent's `low_cash` notice and the derived "cash" note (derive.ts:351-389). Merge by condition `low_cash`.
- **No pass said twice**: the agent's pass notice (view_model `_pass_notice`, code `pass`) and the derived "pass" note. This is the operator's example. Merge by `signal_pass`. Also drop the classic pointer "dashboard → Settings → Signal source" from the shown text.
- **"Back to the classic dashboard" ×4** (App.tsx:327, App.tsx:454, Pages.tsx:568, Pages.tsx:624) → one switch (P-71) + the Waiting fallback.
- **Theme toggle ×3** (App.tsx:333, Pages.tsx:577, Pages.tsx:620) → keep Settings → This dashboard only.
- **"Open setup to start it" ×3** → P-08.
- **Resume**: the band button and the Today "unmanaged" note button. The note keeps a pointer, not a second control.
- **Rules & limits repeats used today, lost today and open now**: the Daily limits rows and the LimitsPanel at Pages.tsx:298. Drop one.
- **Settings "Start at login, keep awake, move…" → `/setup`**: that link reaches no move export at all (it lives in React /dash). Replaced by P-64/65/67.
- **Update dialog has two openers** (band badge Band.tsx:132 and the Settings row): allowed. The spec keeps #80 as is, and the band badge is the single pointer.

## Typed commands (classic chat → /v2 control)

Every command `agent._apply_command` (agent.py:5027-5290) accepts. "Planned" means PR 2 has the route and PR 3 the form.

| Command (agent.py line) | Effect | Money | /v2 control | Status |
|---|---|---|---|---|
| `pause` (:5072) / `resume` (:5087) | pause new entries / clear pause or memory hold | no | Band (P-05) via `/api/v2/command` | in-v2 → PR 3 step 6 |
| `dry on` (:5165), `dry off`/`go live` (:5172) | Practice / LIVE | yes | Band (P-06), LIVE two-step + acknowledge | in-v2 → PR 3 step 6 |
| `stop` (:5045) | stop the agent | yes | Settings → The agent (P-07) | in-v2 on free text; **add `{action:"stop"}`** |
| `status`, `rules`/`my rules`, `autostart`/`start at login` (show), `expiry guard`/`expiry alert` (no arg) | print only | no | readings: band/vitals/Today, P-35, P-64, P-39, P-63 | n/a |
| `set budget N` (:5179), `set percent N` (:5209), `set contracts N` (:5224) | main trade size | yes | Rules → Trade size (P-28) | planned PR 2+3 |
| `set other budget/percent/contracts N` (:5189-5209), `other sizing same`/`clear other sizing` (:5243) | other-trades size | yes | Rules → Trade size (P-29) | planned PR 2+3 |
| `set cap N` (:5247) | entries/day | yes | Rules → Daily limits (P-30) | planned PR 2+3 |
| `set positions N` (:5254) | max positions | yes | Rules → Daily limits (P-32) | planned PR 2+3 |
| `set max loss N` (:5261) | daily loss limit | yes | Rules → Daily limits (P-31) | planned PR 2+3 |
| `tickers …`/`set tickers`/`only tickers`/`tickers any` (:4981) | rule_tickers | no* | Rules → Which signals (P-36) | planned PR 2+3 |
| `skip tickers …`/`skip tickers none` (:4994) | rule_skip_tickers | no* | P-36 | planned PR 2+3 |
| `calls only`/`puts only`/`calls and puts`/`both sides` (:5007) | rule_sides | no* | P-36 | planned PR 2+3 |
| `min strike N` (:5010) | rule_min_strike | no* | P-36 | planned PR 2+3 |
| `loss stop N` (:5017) | rule_loss_streak | no* | P-36 | planned PR 2+3 |
| `no 0dte on\|off`/`skip 0dte on\|off` (:5021) | rule_skip_0dte | no* | P-36 | planned PR 2+3 |
| `policy off`/`disable policy` (:5161) | written rules → .disabled | no* | Rules → Written rules "Turn off" (P-38) | planned (route PR 2; button PR 3) |
| `expiry guard on\|off\|HH:MM` (:4639) | expiry_guard_et | **yes** | Rules → Expiry sell time (P-39) | **missing** |
| `expiry alert on\|off\|HH:MM` (:4639) | expiry_alert_et | no | Settings → Alerts (P-63) | **missing** |
| `autostart on\|off`/`start at login on\|off` (:5123) | OS autostart | no | Settings → This computer (P-64) | planned PR 2+3 |
| `keep awake on\|off`/`stay awake on\|off` (:5128) | keep_awake | no | Settings → This computer (P-65) | planned PR 2+3 |
| `open at login on\|off`/`open dashboard at login on\|off` (:5136) | open_dashboard_at_login | no | Settings → This computer (P-66) | **missing** |
| `preview on\|off` (:5149) | dry_run_preview | no | Signals & pass preview panel (P-49) | **missing** |
| `other trades on\|off` (:5234) | include_other_trades | **yes** | Signals & pass → Which signals (P-43) | **missing** |
| `self-edit on\|off`/`self edit on\|off` (:5272) | self_edit_enabled | no | Settings → Code changes (P-77) | **missing** |
| `code: <request>` (:5035) | self_edit.propose | no (Apply is the risky step) | Settings → Code changes "Describe a change" (P-78) | **missing** |
| anything else / any question | `_ask_llm`, answer only (webui.py:4698) | no | Today → Ask your agent (P-15) | **missing** |

\* The filters only remove entries, so they are not on the SPEC's money list and save directly. Loosening one (e.g. `tickers any`) raises risk; the form says so.

## Classic-only, because…

Kept short on purpose, because the owner asked for everything. Features that /v2 does **not** get:

1. **The first-run wizard** (P-84 accept agreement, P-85 stepper, P-86 skip AI, P-88 Finish setup, P-89 first launch, P-90 move import, P-91 setup assistant). /v2 needs a set-up agent behind it, and on a receiving computer the import *is* the first run. /v2 links back with `?from=v2` only for: unfinished setup (Band/Today), an updated agreement (P-83) and "Bringing a setup here?" (P-90).
2. **Free-text commands and the quick-command chips** (P-17, P-93; `POST /api/command`). The SPEC forbids free text in /v2. Every command that changes something has a typed control in the table above, and questions go to the answer-only Ask box.
3. **"Claude API key — only if you skipped step 2"** on the inline Safety step (P-87). It would be a second home for the AI key; the one home is Settings → Connections → AI helper. The React wizard already dropped it.
4. **The activity sparkline** in the inline /dash header (P-20). The Results chart replaces it.

Routes /v2 does not call directly (the *classic* rows of the Server routes table): `/api/state`, `/api/broker/cash`, `/api/status`, `/api/trades`, `/api/pnl`, `/api/autostart` (GET, POST), `/api/consent`, `/api/finish`, `/api/ask`, `/api/move/import`, `/api/command`. The classic write routes that /v2 reaches through a typed wrapper over the same function are:
- `/api/source`, `/api/source/scope`, `/api/sizing`, `/api/safety`
- `/api/policy`, `/api/policy/disable`, `/api/llm`
- `/api/start-local`, `/api/move/finish`, `/api/deploy`, `/api/rh/start`
- `/api/wallet/create`, `/api/wallet/fund`, `/api/wallet/recurring`
- `/api/pass/buy`, `/api/broker/connect`, `/api/proposal/apply`

## One action, one home: choices where the two inventories differed

| Action | Dash inventory proposed | Setup inventory proposed | Chosen | Why |
|---|---|---|---|---|
| Tip AgentHC | Settings → Help panel | Settings → This dashboard | **Settings → Help and support** | "This dashboard" holds the dashboard switch and theme. Tip, help, support report and the agreement are about AgentHC, not this page, so they share one section |
| Agreement on file | (none) | Settings → This dashboard | **Settings → Help and support** | Same section as above |
| Self-edit switch, "code:" requests | Settings → Code changes; `/api/v2/settings/code`, `/api/v2/proposal/request` | Broker panel disclosure; `/api/v2/command {action:self_edit}`, `/api/v2/self-edit/propose` | **Settings → Code changes**; `/api/v2/settings/code` + `/api/v2/proposal/request` | The feature is not broker-specific, and its Apply/Reject live there. The broker panel keeps one plain link ("Don't see your platform?"). Routes sit with `/api/v2/proposal/apply` and with PR 2's settings family |
| Expiry alert time | Settings → Connections → Alerts | same home as expiry sell (Rules) | **Settings → Alerts** (Rules shows it as text only) | It only notifies, so it is non-money and saves directly on the alerts route. The sell time is money and needs two steps on the limits route. One control each |
| Practice preview on/off | Signals & pass, `/api/v2/settings/signals` | `/api/v2/settings/computer` or a display key | **Signals & pass**, on the panel it controls | The switch sits next to what it turns on |
| Start the agent | — | Settings → This computer | **Settings → The agent on this computer** (Agent row) | Start and Stop belong in one section. Band and Today carry at most one link per screen |
| Move + cloud deploy + import | Settings → This computer | Settings → This computer (deploy as 2nd option) | **Settings → This computer → Move** (export/finish + cloud); import = one link to `/setup?from=v2` | The operator's own example: one Move spot |
| Dashboard switch | Settings → This computer | (4b) | **Settings → This dashboard** ("Dashboard: New \| Classic") | The SPEC says "Settings → This computer". The current "Back to the classic dashboard" row lives in *This dashboard* (Pages.tsx:560-583), and the SPEC says that row *becomes* the switch. Owner question 7 |

## `/setup` links left in /v2 after PR 4
1. "Finish setup", shown only while setup is unfinished (Band.tsx, Today.tsx) → `/setup?from=v2`.
2. Updated-agreement note (P-83) → `/setup?from=v2`.
3. "Bringing a setup to this computer?" in Settings → This computer → Move (P-90) → `/setup?from=v2`.

Nothing else in /v2 navigates to `/setup` or `/dash`, apart from the Waiting fallback link and the dashboard switch itself.

## Open questions for the owner
1. **Code changes in /v2 (P-74..P-78).** This puts "let the AI rewrite the agent and restart it" behind a browser button (two-step, bound to the exact diff). SPEC 4a.3 implies it should be there. Build it, or keep proposal review/apply and `code:` classic-only? (Default in this table: build.)
2. **Confirmations beyond the SPEC's money list.** These are marked two-step here:
   - Stop (P-07)
   - Start when the saved mode is LIVE (P-08)
   - Signal source, scope and subscription key (P-42..P-44)
   - Wallet create and connect-own (P-50, P-54)
   - Expiry sell time (P-39)

   The built-in filters and written rules save directly. Agree?
3. **Route family for the missing typed commands.** SPEC 4a.4 says "typed, via `/api/v2/command`". This plan uses `/api/v2/command` for run-state only (pause, resume, mode, stop). The config switches (open at login, preview, other trades, expiry times, self-edit) go on `/api/v2/settings/*`, next to PR 2's autostart and rules keys, so "This computer" settings never have two routes. OK?
4. **Tip.** Reuse `/api/tip/create` as is (it only makes an invoice the owner pays from their own wallet), or wrap it in a two-step like pass/buy?
5. **LNbits wallet page link.** It is a bearer link that opens the wallet. Show it in /v2, fetched on click via POST, or drop it?
6. **Results by day.** Keep the /v2 rule (day the trade closed) with a caption, or switch to classic's day opened? Totals match either way.
7. **Where the dashboard switch lives.** SPEC: "Settings → This computer". The existing classic-link row is in "This dashboard". Either is one home; pick one.

