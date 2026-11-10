"""Signal sources — pluggable inputs for the agentic trader.

EVENT CONTRACT — every source's ``poll(cfg, state)`` returns a list of dicts:

    {
      "event": "ENTERED" | "EXITED",   # declarative FACT, never an instruction
      "ticker": "SPY",
      "expiry": "2026-07-10",           # YYYY-MM-DD
      "strike": 752.0,
      "type": "C" | "P",
      "event_id": "<unique, stable string>",
      "occurred_at": "2026-07-10T14:31:05+00:00",  # optional: when it happened
                                        # (ISO-8601 with an offset). An ENTERED
                                        # older than max_event_age_s isn't bought.
      "age_unknown": True,              # optional: no occurred_at and the source
                                        # can't tell how old the entry is —
                                        # an ENTERED like that is never bought
      "paper_pnl_pct": 14.2,            # optional, EXITED only
      "message": "...",                 # optional human-readable line
    }

Sources report facts about what a journal/system/human recorded; the AGENT
decides (per the user's configuration) whether to act. Whether to trade at
all, sizing, and risk are always the user's decisions — see DISCLAIMER.md.

A source module exposes:
    NAME         short id used in config ("source": NAME)
    DESCRIPTION  one line shown in the setup wizard
    setup(cfg) -> cfg     interactive wizard step (may mutate + return cfg)
    poll(cfg, state) -> list[event]   called every heartbeat; must not raise
    had_access() -> bool  optional: False when the last poll couldn't read
                          entry signals (e.g. no feed pass yet)
"""

from . import agenthc_day_trade_ideas, generic_json_url, manual_file

SOURCES = {
    agenthc_day_trade_ideas.NAME: agenthc_day_trade_ideas,
    manual_file.NAME: manual_file,
    generic_json_url.NAME: generic_json_url,
}
