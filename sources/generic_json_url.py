"""Signal source: any URL returning events in the standard contract shape.

Point the agent at your own feed. The URL must return JSON like:

    {"events": [
        {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-07-10",
         "strike": 752.0, "type": "C", "event_id": "unique-id-1",
         "occurred_at": "2026-07-10T14:31:05+00:00"}
    ]}

``occurred_at`` (when it happened: ISO-8601; no offset means UTC) is
optional but recommended. The agent never buys an entry older than
``max_event_age_s`` (default 300 s) — a computer waking from sleep must not
buy an hours-old signal. For an event WITHOUT ``occurred_at`` the agent
assumes the worst case: the event may have appeared any time since the
agent last read the feed, so it counts as that old.
  - New between two reads 30 s apart: at most 30 s old — an entry can be
    bought (your caps and rules still apply).
  - New after the computer slept for an hour: up to an hour old — an entry
    is NOT bought (and the owner is told).
  - Already listed the first time the agent reads the feed after it starts
    (or after the URL changes): no telling how old — an entry is NOT bought.
Exits are never held back for their age. (Without ``occurred_at``, keep
``poll_seconds`` well under ``max_event_age_s``: an entry that is new after
a gap that long is never bought. Outside market hours the agent reads the
feed every 5 minutes.)

Optional config: ``source_headers`` (dict) sent with every request — e.g. an
API key for your feed. YOU are responsible for what your feed tells the agent
to do; this template ships no judgment about third-party signals.
"""

from datetime import datetime, timezone

import requests

NAME = "url"
DESCRIPTION = "Generic JSON feed at a URL you provide (bring your own signals)"

# This run's memory, for events the feed gives no time for: when the feed
# was last read, and for each such event still listed, the read before the
# one that first showed it (None: listed on the first read).
_SIGHT = {}


def _utcnow():
    return datetime.now(timezone.utc)


def _feed_time(value):
    """The feed's occurred_at as an ISO-8601 string with an offset (none
    given = UTC), or None when missing or unreadable."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


def poll(cfg, state, save_state=lambda s: None):
    url = cfg.get("source_url", "")
    if not url:
        return []
    started = _utcnow()
    resp = requests.get(url, headers=cfg.get("source_headers") or {}, timeout=15)
    resp.raise_for_status()
    if _SIGHT.get("url") != url:
        _SIGHT.clear()
        _SIGHT.update(url=url, read_at=None, first={})
    last_read, first = _SIGHT["read_at"], {}
    events = []
    for ev in resp.json().get("events", []):
        if ev.get("event") not in ("ENTERED", "EXITED"):
            continue
        try:
            norm = {
                "event": ev["event"],
                "ticker": str(ev["ticker"]).upper(),
                "expiry": str(ev["expiry"]),
                "strike": float(ev["strike"]),
                "type": "C" if str(ev["type"]).upper().startswith("C") else "P",
                "event_id": str(ev.get("event_id")
                                or f"{ev['event']}|{ev['ticker']}|{ev['expiry']}|"
                                   f"{ev['strike']}|{ev['type']}|{ev.get('occurred_at', '')}"),
                "message": ev.get("message", ""),
            }
        except (KeyError, TypeError, ValueError):
            continue
        when = _feed_time(ev.get("occurred_at"))
        if when is None:
            # no time from the feed: as old as it can be (see the top)
            eid = norm["event_id"]
            since = _SIGHT["first"][eid] if eid in _SIGHT["first"] else last_read
            first[eid] = since
            if since is None:
                norm["age_unknown"] = True
            else:
                when = since.isoformat()
        norm["occurred_at"] = when
        events.append(norm)
    _SIGHT.update(read_at=started, first=first)
    return events


def setup(cfg):
    print("\n-- Generic JSON feed --")
    url = input("Feed URL (returns {\"events\": [...]}): ").strip()
    if url and not (url.startswith("https://")
                    or url.startswith("http://127.0.0.1")
                    or url.startswith("http://localhost")):
        print("Refusing non-HTTPS feed URL (trade signals over plain HTTP can be "
              "tampered with in transit). Use https:// or a localhost URL.")
        url = ""
    cfg["source_url"] = url
    return cfg
