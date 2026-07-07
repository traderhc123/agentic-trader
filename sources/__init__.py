"""Signal sources — pluggable inputs for the agentic trader.

EVENT CONTRACT — every source's ``poll(cfg, state)`` returns a list of dicts:

    {
      "event": "ENTERED" | "EXITED",   # declarative FACT, never an instruction
      "ticker": "SPY",
      "expiry": "2026-07-10",           # YYYY-MM-DD
      "strike": 752.0,
      "type": "C" | "P",
      "event_id": "<unique, stable string>",
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
"""

from . import agenthc_day_trade_ideas, generic_json_url, manual_file

SOURCES = {
    agenthc_day_trade_ideas.NAME: agenthc_day_trade_ideas,
    manual_file.NAME: manual_file,
    generic_json_url.NAME: generic_json_url,
}
