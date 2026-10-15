"""net_check — 'can't reach AgentHC' must come back as a named cause + fix.

Every network step is stubbed: no test opens a socket or resolves a name.
"""

import shutil
import ssl
import subprocess
import sys

import pytest
import requests

import agent
import net_check as nc

HOST = "api.traderhc.com"
REAL = "167.71.174.199"


@pytest.fixture
def net(monkeypatch):
    """A healthy network; each test breaks one step."""
    monkeypatch.setattr(nc, "_resolve", lambda host: [REAL])
    monkeypatch.setattr(nc, "_public_dns", lambda host, timeout=5: [REAL])
    monkeypatch.setattr(nc.socket, "create_connection",
                        lambda addr, timeout=None: _Sock())
    monkeypatch.setattr(nc, "_peer_issuer",
                        lambda ip, host, timeout: "Let's Encrypt / YE2")
    monkeypatch.setattr(nc, "_plain_probe", lambda ip, host, timeout: "")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200))
    monkeypatch.setitem(nc._SYSTEM_TRUST, "active", False)
    return monkeypatch


class _Sock:
    def close(self):
        pass


class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_healthy_network_is_ok(net):
    d = nc.diagnose(HOST)
    assert d["ok"] and d["problem"] == "ok" and "HTTP 200" in d["detail"]


def test_dns_lookup_failure(net):
    def boom(host):
        raise nc.socket.gaierror(11001, "getaddrinfo failed")
    net.setattr(nc, "_resolve", boom)
    d = nc.diagnose(HOST)
    assert not d["ok"] and d["problem"] == "dns_failed" and "1.1.1.1" in d["fix"]


@pytest.mark.parametrize("ip", ["146.112.61.106", "0.0.0.0", "127.0.0.1", "192.168.1.1"])
def test_dns_block_page_address(net, ip):
    net.setattr(nc, "_resolve", lambda host: [ip])
    d = nc.diagnose(HOST)
    assert d["problem"] == "dns_blocked" and ip in d["detail"]


def test_dns_redirect_blamed_only_when_connection_also_fails(net):
    net.setattr(nc, "_resolve", lambda host: ["104.21.7.7"])
    # different answer but everything works (a CDN could do this) → ok
    assert nc.diagnose(HOST)["ok"]

    def refuse(addr, timeout=None):
        raise ConnectionRefusedError("refused")
    net.setattr(nc.socket, "create_connection", refuse)
    d = nc.diagnose(HOST)
    assert d["problem"] == "dns_redirected"
    assert "104.21.7.7" in d["detail"] and REAL in d["detail"]


def test_public_dns_unavailable_is_not_blamed(net):
    net.setattr(nc, "_public_dns", lambda host, timeout=5: None)

    def refuse(addr, timeout=None):
        raise TimeoutError("timed out")
    net.setattr(nc.socket, "create_connection", refuse)
    assert nc.diagnose(HOST)["problem"] == "tcp_blocked"


def test_antivirus_interception_is_named(net):
    net.setattr(nc, "_peer_issuer",
                lambda ip, host, timeout: "Avast Web/Mail Shield Root")
    d = nc.diagnose(HOST)
    assert d["problem"] == "intercepted"
    assert "Avast" in d["detail"] and "In Avast" in d["fix"]


def test_unknown_interceptor_quotes_the_issuer(net):
    net.setattr(nc, "_peer_issuer", lambda ip, host, timeout: "Contoso Proxy CA")
    d = nc.diagnose(HOST)
    assert d["problem"] == "intercepted" and "Contoso Proxy CA" in d["detail"]


def test_interception_is_fine_once_system_trust_is_on(net):
    net.setattr(nc, "_peer_issuer", lambda ip, host, timeout: "ESET SSL Filter CA")
    net.setitem(nc._SYSTEM_TRUST, "active", True)
    d = nc.diagnose(HOST)
    assert d["ok"] and "ESET" in d["detail"]


def _tls_fails(net):
    def fail(ip, host, timeout):
        raise ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    net.setattr(nc, "_peer_issuer", fail)


def test_tls_cut_on_this_computer_when_server_answers(net):
    """REGRESSION (user report 10-15): TCP 443 open, Python + Chrome TLS
    failing — our nginx answers a plain probe, so the cut is local."""
    _tls_fails(net)
    net.setattr(nc, "_plain_probe", lambda ip, host, timeout:
                "HTTP/1.1 400 Bad Request\r\n\r\nThe plain HTTP request was sent to HTTPS port")
    d = nc.diagnose(HOST)
    assert d["problem"] == "tls_cut" and "is reachable" in d["detail"]
    assert "HTTPS/SSL scanning" in d["fix"]


def test_block_page_instead_of_tls(net):
    _tls_fails(net)
    net.setattr(nc, "_plain_probe", lambda ip, host, timeout:
                "HTTP/1.1 200 OK\r\n\r\n<html>This site is blocked</html>")
    assert nc.diagnose(HOST)["problem"] == "not_tls"


def test_tls_fails_and_nothing_answers(net):
    _tls_fails(net)

    def dead(ip, host, timeout):
        raise ConnectionResetError("reset")
    net.setattr(nc, "_plain_probe", dead)
    d = nc.diagnose(HOST)
    assert d["problem"] == "tls_cut" and "VPN" in d["fix"]


def test_https_request_failure_after_good_tls(net):
    def boom(*a, **k):
        raise requests.exceptions.ReadTimeout("read timed out")
    net.setattr(requests, "get", boom)
    assert nc.diagnose(HOST)["problem"] == "http_failed"


def test_is_connection_error():
    assert nc.is_connection_error(requests.exceptions.SSLError("x"))
    assert nc.is_connection_error(requests.exceptions.ConnectionError("x"))
    assert nc.is_connection_error(ssl.SSLError("x"))
    assert not nc.is_connection_error(RuntimeError("403 from feed"))
    assert not nc.is_connection_error(ValueError("bad json"))


def test_summary_text():
    bad = nc._result(False, "tls_cut", "the cut", "do this")
    assert nc.summary(bad) == "Can't reach AgentHC: the cut.\nFix: do this"
    assert nc.summary(nc._result(True, "ok", "fine")).startswith("Connection OK")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl CLI")
def test_issuer_of_reads_a_real_certificate(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.der"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-outform", "DER", "-out", str(crt),
                    "-days", "1", "-subj", "/O=Avast Software/CN=Avast Web Shield Root"],
                   check=True, capture_output=True)
    issuer = nc._issuer_of(crt.read_bytes())
    assert "Avast Software" in issuer
    assert nc._name_interceptor(issuer) == "Avast"


# ── system trust store ──

def test_system_trust_opt_out(monkeypatch):
    monkeypatch.setitem(nc._SYSTEM_TRUST, "active", False)
    monkeypatch.setenv("AGENTIC_TRADER_NO_TRUSTSTORE", "1")
    assert nc.use_system_trust() is False


def test_system_trust_without_the_package_is_harmless(monkeypatch):
    monkeypatch.setitem(nc._SYSTEM_TRUST, "active", False)
    monkeypatch.delenv("AGENTIC_TRADER_NO_TRUSTSTORE", raising=False)
    monkeypatch.setitem(sys.modules, "truststore", None)  # import fails
    assert nc.use_system_trust() is False


def test_system_trust_injects_once(monkeypatch):
    calls = []

    class _TS:
        @staticmethod
        def inject_into_ssl():
            calls.append(1)
    monkeypatch.setitem(nc._SYSTEM_TRUST, "active", False)
    monkeypatch.delenv("AGENTIC_TRADER_NO_TRUSTSTORE", raising=False)
    monkeypatch.setitem(sys.modules, "truststore", _TS)
    assert nc.use_system_trust() and nc.use_system_trust()
    assert calls == [1]


# ── wiring: run loop, doctor, wizard ──

def test_run_loop_explains_once_per_day_per_cause(home, monkeypatch):
    diag = nc._result(False, "tls_cut", "cut", "fix it")
    runs, sent = [], []
    monkeypatch.setattr(nc, "diagnose", lambda host: runs.append(host) or diag)
    monkeypatch.setattr(agent, "notify", lambda cfg, text: sent.append(text))
    state = {}
    exc = requests.exceptions.SSLError("handshake failed")
    agent._explain_connection_error(exc, {}, state, lambda s: None, now=1000)
    # inside the 15-min window: not re-diagnosed
    agent._explain_connection_error(exc, {}, state, lambda s: None, now=1100)
    # later the same day, same cause: diagnosed again, not re-notified
    agent._explain_connection_error(exc, {}, state, lambda s: None, now=3000)
    assert runs == ["api.traderhc.com", "api.traderhc.com"]
    assert len(sent) == 1 and "fix it" in sent[0]


def test_run_loop_ignores_non_connection_errors(home, monkeypatch):
    monkeypatch.setattr(nc, "diagnose", lambda host: pytest.fail("diagnosed"))
    assert agent._explain_connection_error(RuntimeError("403 from feed"), {}, {},
                                           lambda s: None) is None


def test_doctor_reports_the_diagnosed_cause(home, monkeypatch):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    (home / "config.json").write_text('{"source": "agenthc"}')

    def boom(*a, **k):
        raise requests.exceptions.SSLError("SSL handshake failed")
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(nc, "diagnose", lambda host: nc._result(
        False, "intercepted", "Kaspersky is intercepting HTTPS", "In Kaspersky, …"))
    monkeypatch.setattr(agent, "_feed_access_check", lambda cfg, st, check: None)
    checks, _ = agent.doctor_checks()
    c = {c["id"]: c for c in checks}["agenthc_api"]
    assert not c["ok"] and "Kaspersky" in c["detail"]
    assert c["fix"] == "In Kaspersky, …" and c["who"] == "owner"


def test_wizard_error_text_uses_the_diagnosis(monkeypatch):
    import webui
    monkeypatch.setattr(nc, "diagnose", lambda host: nc._result(
        False, "not_tls", "a block page answered", "allow-list it"))
    text = webui._net_error_text(requests.exceptions.SSLError("x"))
    assert "a block page answered" in text and "allow-list it" in text
    assert webui._net_error_text(RuntimeError("pass purchase failed (400)")) \
        == "pass purchase failed (400)"
