"""Built-in Lightning wallet for the agent (LNbits backend).

The Agentic Day Trade Ideas feed is sats-priced: agents pay a Lightning
invoice (~$50/day, floating with Bitcoin's price) and receive a 24h access
token (L402). This module gives the agent a wallet it can pay from.

Why LNbits: it's the simplest way to give an agent its own Lightning wallet —
a hosted instance (or your own) exposes a tiny REST API keyed by the wallet's
admin key. Create a wallet in seconds, fund it from ANY Lightning wallet
(Strike, Cash App, Phoenix, Alby, ...), and hand the URL + admin key to this
agent. See README "Give your agent sats".

SECURITY: the admin key can SPEND the wallet. It is stored in the agent's
config with 0600 permissions. Keep only what you're willing to spend in this
wallet (e.g. a month of day-passes), never your savings.
"""

import os
import threading
import time

import requests

_TIMEOUT = 20
# Sats for one ~$50 feed day-pass when neither AgentHC's price nor a live
# BTC price is at hand (≈ $50 at ~$67k/BTC; the real price is on each
# invoice). Only used for "is the wallet funded?" hints, never as a
# spending limit.
DAY_PASS_SATS_EST = 75_000

# Instance used when the agent creates its own wallet. Hosted instances with
# open registration are custodial — the agent keeps only spending money there
# (see SECURITY.md). Override with AGENTIC_TRADER_LNBITS (e.g. your own node).
DEFAULT_LNBITS = os.getenv("AGENTIC_TRADER_LNBITS", "https://demo.lnbits.com")


# Public TEST instances: anyone can register, the operators may wipe them, and
# funds there are not protected. Fine for a few days of passes; never more.
# (legend.lnbits.com stays listed for the warning only: it no longer serves
# HTTPS, so it is never suggested.)
DEMO_HOSTS = ("demo.lnbits.com", "legend.lnbits.com")

# LNbits servers that also charge a service fee on every outgoing payment,
# as (percent, most sats). LNbits counts it in the reserve a wallet must hold
# before it pays ("You must reserve at least (N sat) ...": routing reserve +
# service fee). demo.lnbits.com: 5%, at most 1,000 sats (its settings,
# 2026-11-25).
SERVICE_FEES = {"demo.lnbits.com": (5.0, 1000)}


def host_of(url):
    """The host name of a wallet address ("" when it has none)."""
    from urllib.parse import urlparse
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def fee_reserve_sats(sats, url=None):
    """Sats LNbits wants on top of `sats` before it pays them: the routing-fee
    reserve, the larger of 2 sats and 1% (its defaults,
    lnbits_reserve_fee_min and lnbits_reserve_fee_percent), plus the service
    fee of a server known to charge one (SERVICE_FEES; `url` is the wallet's
    address). LNbits refuses a payment unless the wallet holds the amount
    plus this ("You must reserve at least ...")."""
    sats = int(sats)
    reserve = max(2, -(-sats // 100))
    fee = SERVICE_FEES.get(host_of(url))
    if fee:
        pct, most = fee
        reserve += min(int(sats * pct / 100), most)
    return reserve


def reserve_from_refusal(text):
    """The sats LNbits said it must keep back ("You must reserve at least
    (1860 sat) to cover potential routing fees"), or None."""
    import re
    m = re.search(r"reserve at least \(?\s*([\d,]+)\s*sat", str(text or ""), re.I)
    return int(m.group(1).replace(",", "")) if m else None


def demo_warning(url):
    """A plain-English warning when `url` is a public demo/test instance."""
    host = host_of(url)
    if host not in DEMO_HOSTS:
        return ""
    return (f"{host} is a PUBLIC TEST server: it is wiped without notice "
            "(\"nuked monthly\", its operators say) and anything on it can be "
            "lost. Add only a few days of passes at a time (e.g. 100,000 "
            "sats), or point the agent at your own LNbits with "
            "AGENTIC_TRADER_LNBITS.")


# Where the owner makes a new wallet, in the words each place uses. The
# dashboard's is the classic page's place: /v2 shows "(Settings → Signal
# source)" as "Signals & pass" (webui_app/src/v2/lib/settings.ts v2Words).
NEW_WALLET_DASHBOARD = (
    "press “Create a new wallet” in the dashboard (Settings → Signal source)")
NEW_WALLET_BUTTON = "press “Create a new wallet”"
NEW_WALLET_CLI = "run `python agent.py wallet new` in the agent's folder"

CASH_APP_HINT = (
    "Cash App, Strike, Phoenix, Wallet of Satoshi and other Lightning apps "
    "pay it: scan the QR code or paste the invoice. In Cash App: Bitcoin → "
    "Send → scan the code, or paste the invoice. It is a standard Lightning "
    "invoice and expires in about an hour; make a new one if it does.")


def wallet_gone_text(url, how=NEW_WALLET_DASHBOARD, old_file=None):
    """What the owner reads when the wallet's server no longer accepts the
    agent's wallet key: where the wallet was, the one step that makes a new
    one (`how`), and what became of the old one."""
    host = host_of(url) or "its server"
    reset = (" (public test servers like this one get reset)"
             if host in DEMO_HOSTS else "")
    kept = (f"Your old wallet's details are saved in {old_file}" if old_file
            else "Your old wallet's details are saved to a file in the "
                 "agent's folder first")
    return (f"Your agent's wallet no longer exists on {host}{reset}: the "
            "server no longer accepts its key, so it can't receive or pay "
            f"sats. Create a new wallet: {how}. The new wallet starts empty; "
            f"add sats to it after. {kept}; any sats left in the old wallet "
            "may be gone. (If the old wallet's page still opens and shows "
            "your sats, only its keys were changed: send those sats to the "
            "new wallet from that page.)")


def wallet_unreachable_text(url):
    host = host_of(url) or "its server"
    return (f"The agent can't reach its wallet's server ({host}) right now, "
            "so it can't make a funding invoice or pay a day-pass. Check this "
            "computer's internet connection; the wallet's server may also be "
            "down. Nothing was paid. Try again in a few minutes.")


class WalletError(RuntimeError):
    pass


class WalletGone(WalletError):
    """The wallet's server no longer accepts this wallet's key: LNbits says
    "Wallet not found." (HTTP 404) or "Invalid adminkey." / "No Api Key
    provided." (401/403). The wallet was deleted, its server was wiped, or
    its keys were changed. Nothing can be paid from, or received into, it
    with this key. str() is the owner's message (wallet_gone_text)."""

    def __init__(self, message="", url="", answer=""):
        self.url = url
        self.answer = answer   # the server's own words, for the log
        super().__init__(message or wallet_gone_text(url))


class WalletUnreachable(WalletError):
    """The wallet's server could not be asked: no connection, a timeout, or
    a server error page (HTTP 5xx). The wallet itself may be fine."""


class PaymentRefused(WalletError):
    """The wallet answered a pay request with an error (e.g. not enough
    sats). Whether anything was sent is still read from payment_status."""


class PaymentNotSent(WalletError):
    """The pay request never left this computer: the connection to the
    wallet could not be opened at all, so nothing can have been paid."""


class WalletKeyRejected(PaymentNotSent, WalletGone):
    """The wallet's server turned the pay request away before acting on it
    (HTTP 401/403/404: it does not accept this wallet key — the wallet may
    no longer exist). Nothing can have been paid."""

    def __init__(self, message="", url="", answer=""):
        WalletGone.__init__(self, message, url, answer)


# LNbits' own words when it does not know the key it was sent (decorators.py
# KeyChecker: 404 "Wallet not found.", 403 "Invalid adminkey.", 401 "No Api
# Key provided."), lower case
_KEY_REFUSALS = ("wallet not found", "wallet does not exist", "invalid adminkey",
                 "invalid admin key", "invalid api key", "invalid key",
                 "no api key", "invalid inkey")


def _detail(resp):
    """LNbits' {"detail": "..."} text, or ""."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else ""


def _key_refused(resp):
    """True when the answer is LNbits saying it does not know this wallet's
    key (any of _KEY_REFUSALS with HTTP 401/403/404). A 404 or 403 from
    anything else (a proxy, a protection page) is not that."""
    if getattr(resp, "status_code", 0) not in (401, 403, 404):
        return False
    text = _detail(resp).lower()
    return any(s in text for s in _KEY_REFUSALS)


def _answer_words(resp, limit=200):
    """The server's answer in a few words, for an error message: LNbits'
    own detail, a short plain text, or — for a web page (an error or
    protection page in front of the server) — a note saying so instead of
    its HTML."""
    detail = _detail(resp)
    if detail:
        return detail[:limit]
    text = str(getattr(resp, "text", "") or "")
    head = text.lstrip()[:300].lower()
    if head.startswith("<") or "<html" in head or "<!doctype" in head:
        return ("the server answered with a web page instead of the wallet's "
                "answer (it may be down, or behind a protection page)")
    return " ".join(text.split())[:limit]


def _unreachable(url, exc):
    host = host_of(url) or url
    return WalletUnreachable(f"could not reach the wallet's server {host} "
                             f"({str(exc)[:150]})")


def _server_trouble(resp):
    """HTTP 5xx: the wallet's server (or one in front of it) is failing."""
    return 500 <= int(getattr(resp, "status_code", 0) or 0) <= 599


def _never_left(exc):
    """True only when a request provably never left this computer — the
    wallet's name could not be looked up, the connection was refused, or
    connecting timed out. A request that was cut off or timed out AFTER it
    was sent is not that: the wallet may be acting on it."""
    seen, todo = set(), [exc]
    while todo:
        e = todo.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen.add(id(e))
        if type(e).__name__ in ("ConnectTimeout", "ConnectTimeoutError",
                                "NewConnectionError", "NameResolutionError"):
            return True
        todo += [getattr(e, "reason", None), e.__cause__]
        todo += list(e.args)
    return False


def _no_such_payment(resp):
    """True when a 404 is LNbits itself saying it has no such payment
    ({"detail": "Payment does not exist."}). A 404 from anything else — a
    proxy in front of the wallet, a server that isn't LNbits — says nothing
    about the payment."""
    try:
        body = resp.json()
    except ValueError:
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    text = detail.lower() if isinstance(detail, str) else ""
    return "payment" in text and ("not exist" in text or "not found" in text)


def wallet_fingerprint(cfg):
    """A short id for the wallet set up in `cfg` — a hash of its address and
    admin key, never the key itself — or "" when no wallet is set up. Saved
    with a payment so the agent can tell later whether the wallet it has now
    is the one that was asked to pay."""
    if not (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")):
        return ""
    import hashlib
    ident = f"{str(cfg['lnbits_url']).rstrip('/')}\n{cfg['lnbits_admin_key']}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


def wallet_from_cfg(cfg):
    if cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
        return LNbitsWallet(cfg["lnbits_url"], cfg["lnbits_admin_key"])
    return None


def keep_old_wallet(cfg):
    """Call BEFORE the agent's wallet is replaced. Writes the current
    wallet's address, admin key and page link to a new file
    (old-wallet-<date>-<time>.json) in the agent's private folder, so sats
    left in that wallet can still be reached. Returns the file's path, or ""
    when no wallet is set up. Raises OSError when the file can't be written
    — the caller must then leave the wallet as it is."""
    if not (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")):
        return ""
    import json
    home = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
    os.makedirs(home, exist_ok=True)
    kept = {
        "note": ("This is a Lightning wallet your agent used before it was "
                 "replaced. If sats are left in it, open lnbits_wallet_page "
                 "in a browser (or add this wallet to any LNbits-compatible "
                 "app with lnbits_url + lnbits_admin_key) and move them "
                 "out. Anyone with this file can spend that wallet."),
        "replaced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lnbits_url": cfg.get("lnbits_url"),
        "lnbits_admin_key": cfg.get("lnbits_admin_key"),
        "lnbits_wallet_page": cfg.get("lnbits_wallet_page", ""),
    }
    base = os.path.join(home, "old-wallet-" + time.strftime("%Y%m%d-%H%M%S"))
    for n in range(1000):
        path = f"{base}{'-' + str(n) if n else ''}.json"
        try:  # never overwrite an earlier copy; private from the first byte
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        return path
    raise OSError("could not save the old wallet's details")


def print_funding_invoice(wallet, sats):
    """Print an invoice that adds `sats` to `wallet`. The wallet's server
    makes it, so a wallet that is gone raises WalletGone here, before any
    invoice is shown."""
    bolt11 = wallet.create_invoice(sats, memo="fund agentic trader")
    print(f"\nPay this invoice from ANY Lightning wallet to add {sats:,} sats:\n")
    print(bolt11)
    print(f"\n{CASH_APP_HINT}")
    warn = demo_warning(getattr(wallet, "url", ""))
    if warn:
        print(f"⚠️  {warn}")


def funding_error_text(exc, url, how=NEW_WALLET_CLI):
    """The owner's words for a funding invoice (or balance read) that
    failed with `exc`."""
    if isinstance(exc, WalletGone):
        return wallet_gone_text(url, how)
    if isinstance(exc, WalletUnreachable):
        return wallet_unreachable_text(url)
    return f"The wallet could not make the invoice: {str(exc)[:200]}"


def _print_funding_or_why(wallet, sats):
    try:
        print_funding_invoice(wallet, sats)
    except WalletError as exc:
        print(funding_error_text(exc, wallet.url))


def create_wallet(instance_url=None, name="agentic-trader"):
    """The agent creates ITS OWN wallet on an open-registration LNbits
    instance. Returns (url, admin_key). Raises WalletError on failure."""
    url = (instance_url or DEFAULT_LNBITS).rstrip("/")
    try:
        resp = requests.post(f"{url}/api/v1/account",
                             json={"name": name}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise WalletError(f"could not reach {url}: {exc}")
    if resp.status_code not in (200, 201):
        raise WalletError(
            f"wallet creation failed HTTP {resp.status_code} — this instance "
            "may not allow open registration; set AGENTIC_TRADER_LNBITS to "
            f"one that does, or supply your own wallet. {resp.text[:150]}")

    def _find_key(obj):
        if isinstance(obj, dict):
            for k in ("adminkey", "admin_key", "adminKey"):
                if obj.get(k):
                    return str(obj[k])
            for v in obj.values():
                found = _find_key(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _find_key(v)
                if found:
                    return found
        return ""

    key = _find_key(resp.json())
    if not key:
        raise WalletError(f"no adminkey in response: {resp.text[:150]}")

    # Best-effort wallet-page URL so the user can OPEN the wallet in a
    # browser later (LNbits response shapes vary by version: `user`/`id`
    # at the account level, or nested wallet objects).
    def _find_str(obj, names):
        if isinstance(obj, dict):
            for k in names:
                v = obj.get(k)
                if isinstance(v, str) and v:
                    return v
            for v in obj.values():
                found = _find_str(v, names)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _find_str(v, names)
                if found:
                    return found
        return ""

    data = resp.json()
    usr = _find_str(data, ("user", "user_id", "usr"))
    wal = _find_str(data, ("id", "wallet_id"))
    if usr:
        page = f"{url}/wallet?usr={usr}" + (f"&wal={wal}" if wal else "")
    elif wal:
        page = f"{url}/wallet?wal={wal}"
    else:
        page = ""
    return url, key, page


def wallet_setup(cfg):
    """Attach a Lightning wallet the agent can pay from."""
    if cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
        # Re-running setup must not swap the wallet by accident: a new one
        # starts empty and the sats in the current one would be left behind.
        print("\nYour agent already has a Lightning wallet"
              + (f": {cfg['lnbits_wallet_page']}" if cfg.get("lnbits_wallet_page")
                 else f" on {cfg['lnbits_url']}") + ".")
        print("Replacing it leaves any sats in it behind (its details would "
              "be saved to a file so you can still move them out).")
        if input("Type REPLACE to replace it, or press Enter to keep it: ").strip() != "REPLACE":
            print("Kept your current wallet.")
            return cfg
    print("\nThe agent needs a Lightning wallet to pay sats-priced feeds.")
    print("  1) Create one for me automatically (recommended)")
    print("  2) I already have an LNbits wallet")
    choice = input("Choose [1/2, default 1]: ").strip() or "1"

    def _old_wallet_kept():
        try:
            path = keep_old_wallet(cfg)
        except OSError as exc:
            print(f"Could not save the old wallet's details ({exc}) — the "
                  "wallet was not replaced.")
            return False
        if path:
            print(f"Old wallet's details saved to: {path}")
        return True

    if choice == "1":
        try:
            url, key, page = create_wallet()
        except WalletError as exc:
            print(f"Automatic wallet creation failed: {exc}")
            return cfg
        if not _old_wallet_kept():
            return cfg
        cfg["lnbits_url"] = url
        cfg["lnbits_admin_key"] = key
        cfg.pop("lnbits_wallet_page", None)
        if page:
            cfg["lnbits_wallet_page"] = page
        cfg.setdefault("max_autopay_sats", "auto")
        print(f"Wallet created on {url} ✓ (custodial hosted wallet — the agent "
              "keeps only spending money here)")
        warn = demo_warning(url)
        if warn:
            print(f"⚠️  {warn}")
        if page:
            print(f"Wallet page (view balance / receive): {page}")
        raw = input("Fund it now — amount in sats (e.g. 50000; blank to skip): ").strip()
        if raw.isdigit() and int(raw) > 0:
            _print_funding_or_why(LNbitsWallet(url, key), int(raw))
        return cfg
    url = input("LNbits instance URL (e.g. https://demo.lnbits.com): ").strip()
    if url and not (url.startswith("https://")
                    or url.startswith("http://127.0.0.1")
                    or url.startswith("http://localhost")):
        print("Refusing non-HTTPS wallet URL — the admin key would travel in "
              "cleartext. Use https:// or a localhost URL.")
        return cfg
    key = input("Wallet ADMIN key (Wallet -> API info -> Admin key): ").strip()
    if not url or not key:
        print("Skipped — configure a wallet later by re-running setup.")
        return cfg
    wallet = LNbitsWallet(url, key)
    try:
        bal = wallet.balance_sats()
    except WalletGone:
        print(f"Wallet check FAILED: {host_of(url) or url} does not know that "
              "admin key (\"Wallet not found\"): the wallet was deleted or "
              "reset, or the key is not the wallet's ADMIN key. Copy the admin "
              "key again from the wallet's page (API info), or choose 1 to let "
              "the agent create a new wallet.")
        return cfg
    except WalletError as exc:
        print(f"Wallet check FAILED: {exc}")
        print("Fix the URL/key and re-run setup.")
        return cfg
    if key != cfg.get("lnbits_admin_key") and not _old_wallet_kept():
        return cfg
    cfg["lnbits_url"] = url
    if key != cfg.get("lnbits_admin_key"):
        cfg.pop("lnbits_wallet_page", None)  # that was the old wallet's page
    cfg["lnbits_admin_key"] = key
    print(f"Wallet connected ✓  balance: {bal:,} sats")
    try:  # AgentHC's own day price (imported here: that module imports this one)
        from sources.agenthc_day_trade_ideas import day_pass_price, usd_words
        day = day_pass_price()
        day_usd, day_sats = usd_words(day["usd"]), day["sats"] or DAY_PASS_SATS_EST
    except Exception:
        day_usd, day_sats = "$50", DAY_PASS_SATS_EST
    if bal < day_sats + fee_reserve_sats(day_sats, url):   # LNbits keeps a fee reserve
        print(f"Balance looks low for a ~{day_usd}/day pass. Fund it now?")
        raw = input("Amount in sats to request (blank to skip): ").strip()
        if raw.isdigit() and int(raw) > 0:
            _print_funding_or_why(wallet, int(raw))
    # Safety cap: the agent will never auto-pay an invoice above this.
    # "auto" follows AgentHC's day price (sources/agenthc_day_trade_ideas
    # autopay_caps); a number the owner saves is used as is.
    cfg.setdefault("max_autopay_sats", "auto")
    try:
        from sources.agenthc_day_trade_ideas import autopay_cap_words
        words = autopay_cap_words(cfg)
    except Exception:
        words = str(cfg["max_autopay_sats"])
    print(f"Auto-pay safety cap per invoice: {words}.")
    print("  Set a fixed number with max_autopay_sats in config.json (or say "
          "'set autopay cap 80000' on the dashboard); \"auto\" (or 'set "
          "autopay cap auto') follows AgentHC's price again.")
    return cfg


# ── wallet health ─────────────────────────────────────────────────────────────
# The last thing each wallet's server said about it, by wallet_fingerprint:
# every balance read, funding invoice and payment of an LNbitsWallet notes it
# here, and the dashboard shows it (a wallet that is gone gets its banner
# before the owner tries to add sats). wallet_health() re-asks the server at
# most every HEALTH_TTL_S, in the background. Memory only: nothing is saved.
HEALTH_TTL_S = 600
HEALTH_BACKGROUND = True   # tests switch the background asks off (no network)
_HEALTH = {}
_HEALTH_ASKING = set()
_HEALTH_LOCK = threading.Lock()


def _health_key(url, key):
    return wallet_fingerprint({"lnbits_url": url, "lnbits_admin_key": key})


def _note_health(url, key, status, sats=None, error=""):
    fp = _health_key(url, key)
    if not fp:
        return
    with _HEALTH_LOCK:
        if status == "ok" and sats is None:   # keep the last balance read
            sats = (_HEALTH.get(fp) or {}).get("sats")
        _HEALTH[fp] = {"status": status, "sats": sats, "error": str(error)[:300],
                       "at": time.time(), "site": host_of(url)}


def check_wallet(cfg, timeout=10):
    """Ask the wallet in `cfg` for its balance now. Returns {status, sats,
    error, at, site}: status "ok", "gone" (its server no longer accepts the
    key: WalletGone), "unreachable" (no answer: WalletUnreachable),
    "error" (another failure) or "none" (no wallet set up). Never raises."""
    wallet = wallet_from_cfg(cfg)
    if wallet is None:
        return {"status": "none", "sats": None, "error": "", "at": time.time(),
                "site": None}
    try:
        wallet.balance_sats(timeout=timeout)   # notes the answer itself
    except Exception as exc:
        if not isinstance(exc, WalletError):
            _note_health(wallet.url, wallet.admin_key, "error", error=exc)
    with _HEALTH_LOCK:
        return dict(_HEALTH.get(_health_key(wallet.url, wallet.admin_key))
                    or {"status": "error", "sats": None, "error": "",
                        "at": time.time(), "site": host_of(wallet.url)})


def wallet_health(cfg, max_age=HEALTH_TTL_S):
    """The last known health of the wallet in `cfg` (as check_wallet), or
    None when it was never asked. When that is older than `max_age` it is
    asked again in the background (one ask per wallet at a time): this
    never waits for the wallet's server."""
    fp = wallet_fingerprint(cfg)
    if not fp:
        return None
    with _HEALTH_LOCK:
        last = _HEALTH.get(fp)
        stale = last is None or time.time() - float(last.get("at") or 0) > max_age
        start = stale and HEALTH_BACKGROUND and fp not in _HEALTH_ASKING
        if start:
            _HEALTH_ASKING.add(fp)
    if start:
        snapshot = {"lnbits_url": cfg.get("lnbits_url"),
                    "lnbits_admin_key": cfg.get("lnbits_admin_key")}

        def ask():
            try:
                check_wallet(snapshot)
            finally:
                with _HEALTH_LOCK:
                    _HEALTH_ASKING.discard(fp)
        threading.Thread(target=ask, name="wallet-health", daemon=True).start()
    return dict(last) if last else None


def forget_health():
    """Drop every remembered answer (tests; a wallet that was replaced is
    simply a different fingerprint)."""
    with _HEALTH_LOCK:
        _HEALTH.clear()


class LNbitsWallet:
    def __init__(self, url: str, admin_key: str):
        self.url = url.rstrip("/")
        self.admin_key = admin_key

    def _headers(self):
        return {"X-Api-Key": self.admin_key, "Content-Type": "application/json"}

    def _note(self, status, sats=None, error=""):
        _note_health(self.url, self.admin_key, status, sats, error)

    def _refused(self, resp, what):
        """The error for an answer that isn't the wallet's data: WalletGone
        when LNbits does not know the key, WalletUnreachable for a failing
        server (5xx), else WalletError — each with a short reason, never a
        whole web page."""
        words = _answer_words(resp)
        if _key_refused(resp):
            self._note("gone", error=words)
            return WalletGone(url=self.url,
                              answer=f"HTTP {resp.status_code}: {words}")
        if _server_trouble(resp):
            self._note("unreachable", error=words)
            return WalletUnreachable(f"{what} failed: the wallet's server "
                                     f"{host_of(self.url)} answered HTTP "
                                     f"{resp.status_code}: {words}")
        self._note("error", error=words)
        return WalletError(f"{what} failed HTTP {resp.status_code}: {words}")

    def _json(self, resp, what):
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            words = _answer_words(resp)
            self._note("error", error=words)
            raise WalletError(f"{what} failed: the wallet's server did not "
                              f"answer like an LNbits wallet ({words})")
        return body

    def balance_sats(self, timeout=None) -> int:
        """Current spendable balance in sats. Raises WalletGone when the
        server no longer accepts the wallet's key, WalletUnreachable when it
        can't be asked, WalletError on any other failure."""
        try:
            resp = requests.get(f"{self.url}/api/v1/wallet",
                                headers=self._headers(),
                                timeout=timeout or _TIMEOUT)
        except requests.RequestException as exc:
            self._note("unreachable", error=exc)
            raise _unreachable(self.url, exc)
        if resp.status_code != 200:
            raise self._refused(resp, "wallet check")
        body = self._json(resp, "wallet check")
        try:
            sats = int(body.get("balance", 0)) // 1000  # msats -> sats
        except (TypeError, ValueError):
            self._note("error", error="no balance in the answer")
            raise WalletError("wallet check failed: no balance in the answer")
        self._note("ok", sats=sats)
        return sats

    def create_invoice(self, sats: int, memo: str = "fund trading agent") -> str:
        """Create a RECEIVE invoice (bolt11) so a human can fund this wallet.
        Raises as balance_sats does."""
        try:
            resp = requests.post(f"{self.url}/api/v1/payments",
                                 headers=self._headers(),
                                 json={"out": False, "amount": int(sats), "memo": memo},
                                 timeout=_TIMEOUT)
        except requests.RequestException as exc:
            self._note("unreachable", error=exc)
            raise _unreachable(self.url, exc)
        if resp.status_code not in (200, 201):
            raise self._refused(resp, "invoice creation")
        body = self._json(resp, "invoice creation")
        bolt11 = body.get("payment_request") or body.get("bolt11")
        if not bolt11:
            raise WalletError(f"no payment_request in response: {str(body)[:200]}")
        self._note("ok")   # the key works (the balance is not in this answer)
        return bolt11

    def pay_invoice(self, bolt11: str, wait_seconds: int = 60) -> str:
        """Pay a bolt11 invoice; return the preimage (needed for L402 tokens).

        Raises WalletError on failure or timeout. PaymentRefused means the
        wallet said no; PaymentNotSent that the request never reached it,
        or (WalletKeyRejected) that its server turned the request away
        unread. Any other error leaves the outcome unknown (the payment may
        still go through) — ask payment_status().
        """
        try:
            # payment_request is LNbits' name since 1.x; bolt11 (the same
            # invoice) is its deprecated one, still read by older servers
            resp = requests.post(f"{self.url}/api/v1/payments",
                                 headers=self._headers(),
                                 json={"out": True, "payment_request": bolt11,
                                       "bolt11": bolt11},
                                 timeout=_TIMEOUT + 40)
        except requests.RequestException as exc:
            if _never_left(exc):
                raise PaymentNotSent("could not connect to the wallet "
                                     f"({str(exc)[:150]}) — nothing was sent")
            raise WalletError("no answer from the wallet while paying "
                              f"({str(exc)[:150]}) — the payment may or may "
                              "not have been sent")
        if resp.status_code in (401, 403, 404):
            # turned away at the door (key not accepted / no such wallet):
            # the request was never acted on, so no payment was started
            words = _answer_words(resp, 300)
            if _key_refused(resp):
                self._note("gone", error=words)
            raise WalletKeyRejected("the wallet's server did not accept the "
                                    f"request, HTTP {resp.status_code}: "
                                    f"{words}", url=self.url,
                                    answer=f"HTTP {resp.status_code}: {words}")
        if resp.status_code not in (200, 201):
            raise PaymentRefused(f"payment failed HTTP {resp.status_code}: "
                                 f"{_answer_words(resp, 300)}")
        payment_hash = resp.json().get("payment_hash", "")
        if not payment_hash:
            raise WalletError("payment accepted but no payment_hash returned")

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            check = requests.get(f"{self.url}/api/v1/payments/{payment_hash}",
                                 headers=self._headers(), timeout=_TIMEOUT)
            if check.status_code == 200:
                body = check.json()
                paid = body.get("paid") or (body.get("details") or {}).get("paid")
                preimage = (body.get("preimage")
                            or (body.get("details") or {}).get("preimage") or "")
                if paid and preimage and set(preimage) != {"0"}:
                    return preimage
            time.sleep(2)
        raise WalletError("payment sent but preimage not available within "
                          f"{wait_seconds}s — check the wallet UI")

    def payment_status(self, payment_hash: str):
        """What the wallet says became of an outgoing payment, by its payment
        hash: ("paid", preimage), ("pending", ""), ("failed", ""),
        ("paid_no_proof", "") when the wallet says it paid but gives no
        usable preimage (the receipt an L402 token is built from),
        ("not_found", "") when LNbits itself says it has no such payment,
        ("wallet_gone", "") when LNbits no longer knows the wallet itself
        (its key is refused: nothing can be learnt about the payment), or
        ("unknown", "") when it can't be asked or the answer isn't LNbits'
        own. Never raises."""
        try:
            check = requests.get(f"{self.url}/api/v1/payments/{payment_hash}",
                                 headers=self._headers(), timeout=_TIMEOUT)
            if check.status_code == 404:
                if _key_refused(check):
                    # the WALLET is gone (or its key changed): it can't say
                    # what became of the payment — undecided, never "failed"
                    self._note("gone", error=_answer_words(check))
                    return "wallet_gone", ""
                return ("not_found" if _no_such_payment(check) else "unknown"), ""
            if check.status_code != 200:
                return "unknown", ""
            body = check.json()
        except (requests.RequestException, ValueError):
            return "unknown", ""
        if not isinstance(body, dict):
            return "unknown", ""
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        paid = body.get("paid") or details.get("paid")
        preimage = body.get("preimage") or details.get("preimage") or ""
        if paid and isinstance(preimage, str) and preimage and set(preimage) != {"0"}:
            return "paid", preimage
        if paid:
            return "paid_no_proof", ""
        status = str(body.get("status") or details.get("status") or "").lower()
        if status.endswith("failed"):
            return "failed", ""
        return "pending", ""
