"""Signal source: any URL returning events in the standard contract shape.

Point the agent at your own feed. The URL must return JSON like:

    {"events": [
        {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-07-10",
         "strike": 752.0, "type": "C", "event_id": "unique-id-1"}
    ]}

Optional config: ``source_headers`` (dict) sent with every request — e.g. an
API key for your feed. YOU are responsible for what your feed tells the agent
to do; this template ships no judgment about third-party signals.
"""

import requests

NAME = "url"
DESCRIPTION = "Generic JSON feed at a URL you provide (bring your own signals)"


def poll(cfg, state, save_state=lambda s: None):
    url = cfg.get("source_url", "")
    if not url:
        return []
    resp = requests.get(url, headers=cfg.get("source_headers") or {}, timeout=15)
    resp.raise_for_status()
    events = []
    for ev in resp.json().get("events", []):
        if ev.get("event") not in ("ENTERED", "EXITED"):
            continue
        try:
            events.append({
                "event": ev["event"],
                "ticker": str(ev["ticker"]).upper(),
                "expiry": str(ev["expiry"]),
                "strike": float(ev["strike"]),
                "type": "C" if str(ev["type"]).upper().startswith("C") else "P",
                "event_id": str(ev.get("event_id")
                                or f"{ev['event']}|{ev['ticker']}|{ev['expiry']}|"
                                   f"{ev['strike']}|{ev['type']}|{ev.get('occurred_at', '')}"),
                "message": ev.get("message", ""),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return events


def setup(cfg):
    print("\n-- Generic JSON feed --")
    cfg["source_url"] = input("Feed URL (returns {\"events\": [...]}): ").strip()
    return cfg
