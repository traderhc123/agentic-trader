"""US options market close times (ET), shared by the agent and the adapters.

Regular close 4:00 PM ET. 1:00 PM ET early closes follow the NYSE rules:
  - the day after Thanksgiving (the fourth Thursday of November);
  - December 24 when it falls Monday-Thursday;
  - July 3 when it falls Monday-Thursday.
(When Dec 24 or Jul 3 is a Friday, that Friday is the observed holiday and
the market is closed all day.) The early_close_days config list
(["YYYY-MM-DD", ...]) adds any other early close. Full holidays need no
list: no option expires on a day the market is closed.
"""

from datetime import date, timedelta

REGULAR_CLOSE_MIN = 16 * 60
EARLY_CLOSE_MIN = 13 * 60


def _day(d):
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def is_rule_early_close(d):
    """True on a 1:00 PM ET early close per the NYSE rules above."""
    d = _day(d)
    if d.month == 11:
        first = date(d.year, 11, 1)
        thanksgiving = first + timedelta(days=(3 - first.weekday()) % 7 + 21)
        return d == thanksgiving + timedelta(days=1)
    if (d.month, d.day) in ((12, 24), (7, 3)):
        return d.weekday() <= 3
    return False


def close_minute(cfg, d):
    """The market close on day `d` (date or YYYY-MM-DD), in minutes after
    midnight ET."""
    try:
        d = _day(d)
    except ValueError:
        return REGULAR_CLOSE_MIN
    extra = (cfg or {}).get("early_close_days")
    extra = {str(x).strip() for x in extra} if isinstance(extra, (list, tuple)) else set()
    if d.isoformat() in extra or is_rule_early_close(d):
        return EARLY_CLOSE_MIN
    return REGULAR_CLOSE_MIN
