"""Why can't this computer reach AgentHC? — a plain-English answer.

When the agent can't connect, the cause is almost always on the user's own
machine or network, not the server: antivirus that scans HTTPS, a VPN or
proxy, a router/ISP/DNS filter. Those all look the same from a raw error
("SSL handshake failed", ERR_SSL_PROTOCOL_ERROR), so support turned into a
back-and-forth of PowerShell commands. `diagnose()` runs those steps itself
and names the culprit with a fix the user can act on:

    python agent.py netcheck

Also here: `use_system_trust()` — make Python trust the same certificates
the OS (and the browser) trusts, so a company network or antivirus that
inspects HTTPS with its own installed certificate no longer breaks the
agent while Chrome works fine.
"""

import ipaddress
import os
import socket
import ssl
import tempfile

HOST = "api.traderhc.com"
PORT = 443

# Certificate issuers that mean "something on this computer/network is
# decrypting HTTPS" → the product to name in the fix.
INTERCEPTORS = (
    ("avast", "Avast"), ("avg", "AVG"), ("kaspersky", "Kaspersky"),
    ("eset", "ESET"), ("bitdefender", "Bitdefender"), ("norton", "Norton"),
    ("mcafee", "McAfee"), ("sophos", "Sophos"), ("trend micro", "Trend Micro"),
    ("webroot", "Webroot"), ("malwarebytes", "Malwarebytes"),
    ("fortinet", "Fortinet / FortiGate"), ("fortigate", "Fortinet / FortiGate"),
    ("zscaler", "Zscaler"), ("netskope", "Netskope"),
    ("umbrella", "Cisco Umbrella"), ("opendns", "Cisco Umbrella / OpenDNS"),
    ("palo alto", "Palo Alto Networks"), ("check point", "Check Point"),
    ("blue coat", "Symantec / Blue Coat"), ("untangle", "Untangle"),
    ("lightspeed", "Lightspeed"), ("goguardian", "GoGuardian"),
    ("securly", "Securly"), ("barracuda", "Barracuda"),
    ("mitmproxy", "mitmproxy"), ("fiddler", "Fiddler"), ("charles", "Charles Proxy"),
)
# Public CAs a real server certificate comes from.
PUBLIC_CAS = ("let's encrypt", "google trust services", "digicert", "sectigo",
              "zerossl", "globalsign", "amazon", "isrg")
# Known DNS block-page ranges (resolver filters answer with these).
_BLOCK_NETS = [ipaddress.ip_network(n) for n in (
    "146.112.61.0/24",   # Cisco Umbrella / OpenDNS block pages
)]

_SYSTEM_TRUST = {"active": False}


def use_system_trust():
    """Verify HTTPS with the OS certificate store (Windows/macOS/Linux) via
    the `truststore` package, instead of Python's bundled list. Fail-soft:
    without the package nothing changes. Opt out: AGENTIC_TRADER_NO_TRUSTSTORE=1."""
    if os.getenv("AGENTIC_TRADER_NO_TRUSTSTORE") == "1":
        return False
    if _SYSTEM_TRUST["active"]:
        return True
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        return False
    _SYSTEM_TRUST["active"] = True
    return True


def is_connection_error(exc):
    """True for 'couldn't reach / couldn't secure the connection' errors —
    the ones diagnose() can explain (not HTTP 4xx/5xx answers)."""
    try:
        import requests
        if isinstance(exc, (requests.exceptions.SSLError,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.ConnectTimeout)):
            return True
    except Exception:
        pass
    return isinstance(exc, (ssl.SSLError, ConnectionError, socket.gaierror,
                            socket.timeout))


def _result(ok, problem, detail, fix=""):
    return {"ok": ok, "problem": problem, "detail": detail, "fix": fix}


def _suspicious_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (a.is_private or a.is_loopback or a.is_unspecified or a.is_reserved
            or a.is_link_local or any(a in n for n in _BLOCK_NETS))


def _resolve(host):
    infos = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
    out = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def _public_dns(host, timeout=5):
    """The host's A records from Cloudflare's DNS-over-HTTPS (by IP, so the
    local resolver can't interfere). None when that lookup isn't possible."""
    try:
        import requests
        r = requests.get("https://1.1.1.1/dns-query",
                         params={"name": host, "type": "A"},
                         headers={"accept": "application/dns-json"}, timeout=timeout)
        r.raise_for_status()
        ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        return ips or None
    except Exception:
        return None


def _issuer_of(der):
    """Issuer text of a DER certificate, without needing extra packages."""
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
            path = f.name
        decoded = ssl._ssl._test_decode_cert(path)
        parts = []
        for rdn in decoded.get("issuer", ()):
            for key, val in rdn:
                if key in ("organizationName", "commonName"):
                    parts.append(val)
        return " / ".join(parts)
    except Exception:
        return ""
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _peer_issuer(ip, host, timeout):
    """Issuer of whatever certificate this network hands us for `host`."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, PORT), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return _issuer_of(tls.getpeercert(binary_form=True) or b"")


def _plain_probe(ip, host, timeout):
    """Send plain HTTP to port 443: our real server answers with its
    'plain HTTP request was sent to HTTPS port' page; a filter or block page
    answers with something else."""
    with socket.create_connection((ip, PORT), timeout=timeout) as sock:
        sock.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        data = b""
        while len(data) < 4096:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    return data.decode("latin-1", "replace")


def _name_interceptor(issuer):
    low = issuer.lower()
    for needle, product in INTERCEPTORS:
        if needle in low:
            return product
    return ""


_TRY = ("Quick tests: turn off any VPN; try a phone hotspot — if it works "
        "there, your home network or router filter is the cause.")


def diagnose(host=HOST, timeout=8):
    """Walk DNS → TCP → TLS → HTTPS and return
    {ok, problem, detail, fix}. problem is one of: ok, dns_failed,
    dns_blocked, dns_redirected, tcp_blocked, intercepted, not_tls,
    tls_cut, http_failed."""
    # 1. DNS
    try:
        ips = _resolve(host)
    except OSError as exc:
        return _result(False, "dns_failed",
                       f"this computer can't look up {host} ({exc})",
                       "Check the internet connection. If other websites work, "
                       "a DNS filter is blocking this address — set your DNS to "
                       "1.1.1.1 or try a phone hotspot.")
    bad = [ip for ip in ips if _suspicious_ip(ip)]
    if bad:
        return _result(False, "dns_blocked",
                       f"{host} resolves to {bad[0]} — a block page or filter, "
                       "not the AgentHC server",
                       "A DNS filter (router parental controls, ISP filter, "
                       "Cisco Umbrella/OpenDNS, or security software) is blocking "
                       f"{host}. Allow-list it there, or set DNS to 1.1.1.1. " + _TRY)
    # A different answer from public DNS is only blamed when a later step
    # fails too (a CDN can legitimately answer differently per network).
    public = _public_dns(host)
    redirected = None
    if public and not set(ips) & set(public):
        redirected = _result(False, "dns_redirected",
                             f"your network sends {host} to {', '.join(ips)}, but "
                             f"its real address is {', '.join(public)}",
                             "Your router, ISP or security software is redirecting "
                             f"this address. Allow-list {host} there, or set DNS to "
                             "1.1.1.1. " + _TRY)
    ip = ips[0]
    diag = _connect(host, ip, timeout)
    if not diag["ok"] and redirected:
        return redirected
    return diag


def _connect(host, ip, timeout):
    """TCP → TLS → HTTPS against one resolved address."""
    # 2. TCP
    try:
        socket.create_connection((ip, PORT), timeout=timeout).close()
    except OSError as exc:
        return _result(False, "tcp_blocked",
                       f"can't open a connection to {host} ({ip}:{PORT}): {exc}",
                       "A firewall (Windows, antivirus, router or workplace) is "
                       f"blocking {host}. Allow it, or check the internet "
                       "connection. " + _TRY)

    # 3. TLS — what certificate does this network hand us?
    try:
        issuer = _peer_issuer(ip, host, timeout)
    except (ssl.SSLError, OSError) as exc:
        try:
            page = _plain_probe(ip, host, timeout)
        except OSError:
            page = ""
        if "HTTPS port" in page:
            return _result(False, "tls_cut",
                           "the AgentHC server is reachable, but the secure "
                           f"connection is cut off on this computer ({exc})",
                           "Security software or a VPN on this computer is "
                           "breaking HTTPS. Turn off its HTTPS/SSL scanning (often "
                           "called 'web shield' or 'encrypted traffic scanning') or "
                           f"add an exception for {host}. " + _TRY)
        if page.startswith("HTTP/"):
            return _result(False, "not_tls",
                           "something on your network answered instead of AgentHC "
                           "with an unencrypted web page (a filter or block page)",
                           "A router/ISP filter, workplace proxy or security software "
                           f"is blocking {host}. Allow-list it there. " + _TRY)
        return _result(False, "tls_cut",
                       f"the secure connection to {host} fails ({exc})",
                       "A VPN, proxy, security software or network filter is "
                       "interfering with HTTPS. Turn off HTTPS scanning / the VPN, "
                       f"or allow-list {host}. " + _TRY)

    low = issuer.lower()
    if issuer and not any(ca in low for ca in PUBLIC_CAS):
        product = _name_interceptor(issuer)
        who = product or f"'{issuer}'"
        if _SYSTEM_TRUST["active"] and _https_ok(host, timeout):
            return _result(True, "ok",
                           f"connected — {who} inspects HTTPS on this computer, "
                           "and the agent trusts it the same way your browser does")
        return _result(False, "intercepted",
                       f"{who} is intercepting HTTPS (the certificate for {host} "
                       f"was issued by {issuer}, not a public authority)",
                       f"In {product or 'that software'}, turn off HTTPS/SSL "
                       f"scanning or add an exception for {host}. If it's a "
                       "work or school network, ask its admin to allow it.")

    # 4. A real HTTPS request, the way the agent makes them.
    try:
        import requests
        r = requests.get(f"https://{host}/health", timeout=timeout)
        return _result(True, "ok", f"connected to {host} (HTTP {r.status_code})")
    except Exception as exc:
        return _result(False, "http_failed",
                       f"connected securely, but the request failed: {str(exc)[:120]}",
                       "Retry in a minute; if it keeps failing, send this message "
                       "to AgentHC support.")


def _https_ok(host, timeout):
    try:
        import requests
        requests.get(f"https://{host}/health", timeout=timeout)
        return True
    except Exception:
        return False


def summary(diag):
    """One block of text for a log line, notification or wizard message."""
    if diag["ok"]:
        return f"Connection OK — {diag['detail']}"
    return f"Can't reach AgentHC: {diag['detail']}.\nFix: {diag['fix']}"
