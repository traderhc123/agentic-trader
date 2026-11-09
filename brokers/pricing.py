"""Marketable-limit pricing shared by the broker adapters.

A marketable limit fills right away like a market order, but with a price
bound: buys pay up to the ask +5%, sells accept down to the bid -5%.

The price is rounded (UP for buys, DOWN for sells) to a tick every US
option class accepts: $0.05 under $3.00 and $0.10 from $3.00 (penny-program
classes also take these; classes outside the program reject in-between
prices). A sell into a bid under $0.10 goes out AT the bid: the bid is
already a valid price for that class, while bid -5% would round down to
$0.05 or $0.01 — a price a nickel-tick class rejects, which would leave an
expiring position unsold.
"""


def _tick_down(cents):
    step = 10 if cents >= 300 else 5
    return cents // step * step


def _tick_up(cents):
    up = -(-cents // 5) * 5
    return -(-up // 10) * 10 if up >= 300 else up


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
    if cents < 1:
        return None
    if side == "buy":
        ticked = _tick_up(-(-cents * 105 // 100))      # ceil(price * 1.05)
    elif cents < 10:
        ticked = cents                                  # at the bid
    else:
        ticked = max(5, _tick_down(cents * 95 // 100))  # floor(price * 0.95)
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
