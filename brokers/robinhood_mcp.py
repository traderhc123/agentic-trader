"""Minimal client for Robinhood's agentic-trading MCP endpoint.

Speaks just enough of the MCP streamable-HTTP transport for tools/list and
tools/call against https://agent.robinhood.com/mcp/trading, plus the OAuth2
flow (dynamic client registration + authorization-code + PKCE, then headless
refresh-token renewal). No MCP framework dependency — requests + stdlib only.

Robinhood may rotate the refresh token on every refresh; this client
re-persists the token file atomically after each refresh. Token file mode is
0600 — it is a live credential for your brokerage account.
"""

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests

MCP_URL = "https://agent.robinhood.com/mcp/trading"
REGISTER_URL = "https://agent.robinhood.com/oauth/trading/register"
AUTHORIZE_URL = "https://robinhood.com/oauth"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
REDIRECT_URI = "http://127.0.0.1:8721/callback"
SCOPE = "internal"

_EXPIRY_MARGIN_S = 120
_TIMEOUT_S = 15


def _write_private(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rh_")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


class RobinhoodMCP:
    def __init__(self, token_path):
        self.token_path = token_path
        self._lock = threading.Lock()
        self._session_id = None
        self._rpc_id = 0

    # ── OAuth ────────────────────────────────────────────────────────────

    def is_authenticated(self):
        tokens = self._load_tokens()
        return bool(tokens and tokens.get("refresh_token"))

    def _load_tokens(self):
        try:
            with open(self.token_path) as f:
                return json.load(f)
        except Exception:
            return None

    def _save_tokens(self, tokens):
        _write_private(self.token_path, tokens)

    def auth_start(self, client_name="agentic-day-trade-ideas-agent"):
        """Dynamic registration + PKCE. Returns (authorize_url, pending_state)."""
        resp = requests.post(REGISTER_URL, json={
            "client_name": client_name,
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        client_id = resp.json()["client_id"]
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTHORIZE_URL + "?" + urlencode({
            "client_id": client_id, "redirect_uri": REDIRECT_URI,
            "response_type": "code", "scope": SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state,
        })
        return url, {"client_id": client_id, "code_verifier": verifier,
                     "state": state, "redirect_uri": REDIRECT_URI}

    def auth_finish(self, pending, redirect_url):
        """Exchange the pasted redirect URL for tokens. Returns True on success."""
        qs = parse_qs(urlparse(redirect_url.strip()).query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if not code:
            raise ValueError("No ?code= in the pasted URL — paste the FULL redirect URL.")
        if pending.get("state") and state != pending["state"]:
            raise ValueError("OAuth state mismatch — restart the auth flow.")
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"],
            "code_verifier": pending["code_verifier"],
        }, headers={"Accept": "application/json"}, timeout=_TIMEOUT_S)
        if resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed HTTP {resp.status_code}: "
                               f"{resp.text[:300]} (codes expire in minutes — retry)")
        body = resp.json()
        self._save_tokens({
            "client_id": pending["client_id"],
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token", ""),
            "expires_at": time.time() + float(body.get("expires_in", 3600)),
            "scope": body.get("scope", SCOPE),
        })
        return True

    def _refresh(self, tokens):
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.get("refresh_token", ""),
            "client_id": tokens.get("client_id", ""),
            "scope": tokens.get("scope", SCOPE),
        }, headers={"Accept": "application/json"}, timeout=_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        body = resp.json()
        tokens = dict(tokens)
        tokens["access_token"] = body.get("access_token", "")
        if body.get("refresh_token"):
            tokens["refresh_token"] = body["refresh_token"]
        tokens["expires_at"] = time.time() + float(body.get("expires_in", 3600))
        self._save_tokens(tokens)
        self._session_id = None
        return tokens

    def _access_token(self, force=False):
        tokens = self._load_tokens()
        if not tokens or not tokens.get("refresh_token"):
            return None
        if force or time.time() >= float(tokens.get("expires_at", 0)) - _EXPIRY_MARGIN_S \
                or not tokens.get("access_token"):
            tokens = self._refresh(tokens)
            if not tokens:
                return None
        return tokens.get("access_token") or None

    # ── MCP transport ────────────────────────────────────────────────────

    def _headers(self, access_token):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {access_token}"}
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    @staticmethod
    def _parse_body(text, want_id):
        if not text or not text.strip():
            return None
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except ValueError:
                return None
        candidate = None
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:].strip())
            except ValueError:
                continue
            candidate = msg
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        return candidate

    def _rpc(self, access_token, method, params, notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        req_id = None
        if not notification:
            self._rpc_id += 1
            req_id = self._rpc_id
            payload["id"] = req_id
        if params is not None:
            payload["params"] = params
        resp = requests.post(MCP_URL, json=payload,
                             headers=self._headers(access_token), timeout=_TIMEOUT_S)
        if resp.status_code in (401, 403):
            return resp.status_code, None
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if notification:
            return resp.status_code, None
        msg = self._parse_body(resp.text, req_id)
        if not isinstance(msg, dict) or msg.get("error"):
            return resp.status_code, None if not isinstance(msg, dict) else None
        return resp.status_code, msg.get("result")

    def _initialize(self, access_token):
        self._session_id = None
        status, result = self._rpc(access_token, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "agentic-day-trade-ideas-agent", "version": "1.0"},
        })
        if status in (401, 403) or result is None:
            return False
        try:
            self._rpc(access_token, "notifications/initialized", {}, notification=True)
        except Exception:
            pass
        return True

    def _request(self, method, params):
        access = self._access_token()
        if not access:
            return None
        for attempt in (0, 1):
            if self._session_id is None and not self._initialize(access):
                if attempt == 0:
                    access = self._access_token(force=True)
                    if not access:
                        return None
                    continue
                return None
            status, result = self._rpc(access, method, params)
            if status in (401, 403):
                if attempt == 0:
                    access = self._access_token(force=True)
                    if not access:
                        return None
                    self._session_id = None
                    continue
                return None
            if result is None and status in (400, 404) and attempt == 0:
                self._session_id = None
                continue
            return result
        return None

    # ── public API ───────────────────────────────────────────────────────

    def call_tool(self, name, arguments):
        with self._lock:
            return self._request("tools/call", {"name": name,
                                                "arguments": arguments or {}})

    def list_tools(self):
        with self._lock:
            result = self._request("tools/list", {})
        return result.get("tools") if isinstance(result, dict) else None


def content_json(result):
    """Parse the first JSON text block from an MCP tool result (None on miss)."""
    try:
        for item in (result or {}).get("content", []):
            if item.get("type") != "text":
                continue
            try:
                return json.loads(item.get("text", ""))
            except ValueError:
                continue
    except Exception:
        pass
    return None


def tool_ok(result):
    return isinstance(result, dict) and not result.get("isError")
