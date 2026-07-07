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

import time

import requests

_TIMEOUT = 20


class WalletError(RuntimeError):
    pass


def wallet_from_cfg(cfg):
    if cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
        return LNbitsWallet(cfg["lnbits_url"], cfg["lnbits_admin_key"])
    return None


def print_funding_invoice(wallet, sats):
    bolt11 = wallet.create_invoice(sats, memo="fund agentic trader")
    print(f"\nPay this invoice from ANY Lightning wallet to add {sats:,} sats:\n")
    print(bolt11)
    print("\n(Strike, Cash App, Phoenix, Alby, Wallet of Satoshi, etc. — scan or paste.)")


def wallet_setup(cfg):
    """Attach an LNbits wallet so the agent can pay sats-priced feeds."""
    print("\nGive this agent a Lightning wallet it can pay from (see README")
    print("'Give your agent sats' — an LNbits wallet takes ~2 minutes to make).")
    url = input("LNbits instance URL (e.g. https://demo.lnbits.com): ").strip()
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
    cfg["lnbits_url"] = url
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

        Raises WalletError on failure or timeout.
        """
        resp = requests.post(f"{self.url}/api/v1/payments",
                             headers=self._headers(),
                             json={"out": True, "bolt11": bolt11},
                             timeout=_TIMEOUT + 40)
        if resp.status_code not in (200, 201):
            raise WalletError(f"payment failed HTTP {resp.status_code}: "
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
