"""Built-in Lightning wallet for the agent (LNbits backend).

The Agentic Day Trade Ideas feed is sats-priced: agents pay a Lightning
invoice (~$10/day, floating with Bitcoin's price) and receive a 24h access
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
import time

import requests

_TIMEOUT = 20

# Instance used when the agent creates its own wallet. Hosted instances with
# open registration are custodial — the agent keeps only spending money there
# (see SECURITY.md). Override with AGENTIC_TRADER_LNBITS (e.g. your own node).
DEFAULT_LNBITS = os.getenv("AGENTIC_TRADER_LNBITS", "https://demo.lnbits.com")


# Public TEST instances: anyone can register, the operators may wipe them, and
# funds there are not protected. Fine for a few days of passes; never more.
DEMO_HOSTS = ("demo.lnbits.com", "legend.lnbits.com")


def demo_warning(url):
    """A plain-English warning when `url` is a public demo/test instance."""
    from urllib.parse import urlparse
    host = (urlparse(str(url or "")).hostname or "").lower()
    if host not in DEMO_HOSTS:
        return ""
    return (f"{host} is a PUBLIC TEST server — it can be reset or go offline "
            "and anything on it can be lost. Keep only a few days of passes "
            "there (e.g. 50,000 sats), or point the agent at your own LNbits "
            "with AGENTIC_TRADER_LNBITS.")


class WalletError(RuntimeError):
    pass


class PaymentRefused(WalletError):
    """The wallet answered a pay request with an error (e.g. not enough
    sats). Whether anything was sent is still read from payment_status."""


class PaymentNotSent(WalletError):
    """The pay request never left this computer: the connection to the
    wallet could not be opened at all, so nothing can have been paid."""


class WalletKeyRejected(PaymentNotSent):
    """The wallet's server turned the pay request away before acting on it
    (HTTP 401/403/404: it does not accept this wallet key — the wallet may
    no longer exist). Nothing can have been paid."""


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
    bolt11 = wallet.create_invoice(sats, memo="fund agentic trader")
    print(f"\nPay this invoice from ANY Lightning wallet to add {sats:,} sats:\n")
    print(bolt11)
    print("\n(Strike, Cash App, Phoenix, Alby, Wallet of Satoshi, etc. — scan or paste.)")


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
        cfg.setdefault("max_autopay_sats", 30_000)
        print(f"Wallet created on {url} ✓ (custodial hosted wallet — the agent "
              "keeps only spending money here)")
        warn = demo_warning(url)
        if warn:
            print(f"⚠️  {warn}")
        if page:
            print(f"Wallet page (view balance / receive): {page}")
        raw = input("Fund it now — amount in sats (e.g. 50000; blank to skip): ").strip()
        if raw.isdigit() and int(raw) > 0:
            print_funding_invoice(LNbitsWallet(url, key), int(raw))
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
    if bal < 15_000:
        print("Balance looks low for a ~$10/day pass. Fund it now?")
        raw = input("Amount in sats to request (blank to skip): ").strip()
        if raw.isdigit() and int(raw) > 0:
            print_funding_invoice(wallet, int(raw))
    # Safety cap: the agent will never auto-pay an invoice above this.
    cfg.setdefault("max_autopay_sats", 30_000)
    print(f"Auto-pay safety cap: {cfg['max_autopay_sats']:,} sats per invoice "
          "(edit max_autopay_sats in config.json to change).")
    return cfg


class LNbitsWallet:
    def __init__(self, url: str, admin_key: str):
        self.url = url.rstrip("/")
        self.admin_key = admin_key

    def _headers(self):
        return {"X-Api-Key": self.admin_key, "Content-Type": "application/json"}

    def balance_sats(self) -> int:
        """Current spendable balance in sats. Raises WalletError on failure."""
        resp = requests.get(f"{self.url}/api/v1/wallet",
                            headers=self._headers(), timeout=_TIMEOUT)
        if resp.status_code != 200:
            raise WalletError(f"wallet check failed HTTP {resp.status_code}: "
                              f"{resp.text[:200]}")
        return int(resp.json().get("balance", 0)) // 1000  # msats -> sats

    def create_invoice(self, sats: int, memo: str = "fund trading agent") -> str:
        """Create a RECEIVE invoice (bolt11) so a human can fund this wallet."""
        resp = requests.post(f"{self.url}/api/v1/payments",
                             headers=self._headers(),
                             json={"out": False, "amount": int(sats), "memo": memo},
                             timeout=_TIMEOUT)
        if resp.status_code not in (200, 201):
            raise WalletError(f"invoice creation failed HTTP {resp.status_code}: "
                              f"{resp.text[:200]}")
        body = resp.json()
        bolt11 = body.get("payment_request") or body.get("bolt11")
        if not bolt11:
            raise WalletError(f"no payment_request in response: {str(body)[:200]}")
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
            resp = requests.post(f"{self.url}/api/v1/payments",
                                 headers=self._headers(),
                                 json={"out": True, "bolt11": bolt11},
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
            raise WalletKeyRejected("the wallet's server did not accept the "
                                    f"request, HTTP {resp.status_code}: "
                                    f"{resp.text[:300]}")
        if resp.status_code not in (200, 201):
            raise PaymentRefused(f"payment failed HTTP {resp.status_code}: "
                                 f"{resp.text[:300]}")
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
        ("not_found", "") when LNbits itself says it has no such payment, or
        ("unknown", "") when it can't be asked or the answer isn't LNbits'
        own. Never raises."""
        try:
            check = requests.get(f"{self.url}/api/v1/payments/{payment_hash}",
                                 headers=self._headers(), timeout=_TIMEOUT)
            if check.status_code == 404:
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
