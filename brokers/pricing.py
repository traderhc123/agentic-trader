"""Marketable-limit pricing shared by the broker adapters.

A marketable limit fills right away like a market order, but with a price
bound: buys pay up to the ask +5% (rounded UP to a $0.05 tick), sells accept
down to the bid -5% (rounded DOWN to a $0.05 tick, never below $0.01).
"""


def marketable_limit(price, side):
    """Per-share limit price from a quote (the ask for buys, the bid for
    sells), or None when the quote isn't a usable positive price."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if not 0 < price < float("inf"):
        return None
    cents = int(round(price * 100))
    if side == "buy":
        buffered = -(-cents * 105 // 100)      # ceil(price * 1.05)
        ticked = -(-buffered // 5) * 5         # round UP to a $0.05 tick
    else:
        buffered = cents * 95 // 100           # floor(price * 0.95)
        ticked = max(1, buffered // 5 * 5)     # round DOWN, floor $0.01
    return ticked / 100.0


def quote_number(obj, key):
    """The first `key` value found anywhere in a quote payload, as a float:
    0.0 for an explicit zero, None when the key is absent, null or not a
    number (unknown — never read as "no bid")."""
    if isinstance(obj, dict):
        if key in obj:
            v = obj[key]
            if v is None or isinstance(v, bool) or v == "":
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if 0 <= f < float("inf") else None
        for v in obj.values():
            found = quote_number(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = quote_number(v, key)
            if found is not None:
                return found
    return None
