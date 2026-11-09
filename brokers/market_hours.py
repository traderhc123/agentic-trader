"""US options market close times (ET), shared by the agent and the adapters.

Regular close 4:00 PM ET. 1:00 PM ET early closes follow the NYSE rules:
  - the day after Thanksgiving (the fourth Thursday of November);
  - December 24 when it falls Monday-Thursday;
  - July 3 when it falls Monday-Thursday.
(When Dec 24 or Jul 3 is a Friday, that Friday is the observed holiday and
the market is closed all day.) The early_close_days config list
(["YYYY-MM-DD", ...]) adds any other early close. The expiry guard, the
exit-remainder re-sell and the ledger check skip full NYSE holidays
(is_market_holiday — no option expires on one, and an order sent then
would only queue at a stale price): New Year's Day,
Martin Luther King Jr. Day, Presidents' Day, Good Friday, Memorial Day,
Juneteenth, Independence Day, Labor Day, Thanksgiving and Christmas, moved
to the Friday/Monday the NYSE observes (a Saturday New Year's Day is not
moved). The market_holidays config list (["YYYY-MM-DD", ...]) adds any
other closure (e.g. a national day of mourning).
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


def _nth_weekday(year, month, weekday, n):
    """The n-th `weekday` (0 = Monday) of the month; n = -1 is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year):
    """Easter Sunday (Gregorian; anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d):
    """A fixed-date holiday on a weekend moves to Friday/Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    """The full-day NYSE holidays of `year` (a set of dates)."""
    days = {
        _nth_weekday(year, 1, 0, 3),            # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),            # Presidents' Day
        _easter(year) - timedelta(days=2),      # Good Friday
        _nth_weekday(year, 5, 0, -1),           # Memorial Day
        _observed(date(year, 7, 4)),            # Independence Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day
        _nth_weekday(year, 11, 3, 4),           # Thanksgiving
        _observed(date(year, 12, 25)),          # Christmas
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:                 # a Saturday one isn't moved
        days.add(_observed(new_year))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    return days


def is_market_holiday(cfg, d):
    """True on a full-day NYSE holiday (or a day in market_holidays)."""
    try:
        d = _day(d)
    except ValueError:
        return False
    extra = (cfg or {}).get("market_holidays")
    extra = {str(x).strip() for x in extra} if isinstance(extra, (list, tuple)) else set()
    return d.isoformat() in extra or d in nyse_holidays(d.year)


def is_trading_day(cfg, d):
    """A weekday that isn't a market holiday."""
    try:
        d = _day(d)
    except ValueError:
        return False
    return d.weekday() < 5 and not is_market_holiday(cfg, d)
