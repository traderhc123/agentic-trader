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


def exit_qty(pos, event):
    """How many contracts an EXITED sells: the ledger row's qty, or fewer
    when the event carries max_qty (the agent re-selling an exit remainder
    caps it at what the account holds). Never more than the ledger row."""
    qty = int(pos.get("qty") or 0)
    cap = event.get("max_qty")
    if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap < qty:
        return cap
    return qty


# The most contracts one trade buys when max_contracts_per_trade was never
# set (the same number as agent.CONTRACT_CAP_DEFAULT).
CONTRACT_CAP_DEFAULT = 100


def contract_cap(cfg):
    """max_contracts_per_trade as the adapters read it: never set = the
    default. (A value that is not a whole number raises, as it always did.)"""
    v = cfg.get("max_contracts_per_trade")
    return CONTRACT_CAP_DEFAULT if v is None else int(v)


def capped(cfg, qty, note):
    """(qty, note) after max_contracts_per_trade. When the cap cuts the
    order the note says so, with the size the sizing asked for, so a capped
    trade never looks like a full one."""
    cap = contract_cap(cfg)
    if qty <= cap:
        return qty, note
    if cap < 1:
        return 0, f"{note} — max contracts per trade is {cap} — skipped"
    return cap, (f"{note} — capped at {cap} by max contracts per trade "
                 "(change it with: set max contracts N)")


def fixed_count(cfg):
    """(qty, note) for sizing_mode "contracts": contracts_per_trade (at
    least 1), then the cap."""
    n = max(1, int(cfg.get("contracts_per_trade", 1)))
    return capped(cfg, n, "fixed contract count" if n <= contract_cap(cfg)
                  else f"fixed contract count ({n})")


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
