"""Help panel: "Check my connection" + "Copy support report".

The report is one block of text a user can paste to support instead of a
back-and-forth of terminal commands: version, platform, setup checks, the
connection diagnosis (AgentHC + GitHub), proxy settings and recent errors.
Secrets never go in it — keys, tokens, invoices and wallet ids are scrubbed
from every line before it leaves this module.
"""

import os
import platform
import re
import sys
import time
from collections import deque

# Recent errors from the running agent (agent.py appends; newest last).
RECENT_ERRORS = deque(maxlen=20)

_SECRET_PATTERNS = [
    (re.compile(r"L402\s+\S+", re.I), "L402 [token]"),
    (re.compile(r"\blnbc[0-9a-z]{20,}", re.I), "[invoice]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "[api key]"),
    (re.compile(r"\b(aghc|xai|gsk|pk|rk)_[A-Za-z0-9_\-]{8,}", re.I), "[api key]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[hex secret]"),
    (re.compile(r"\b[A-Za-z0-9+/=_\-]{40,}\b"), "[token]"),
    (re.compile(r"([?&](k|key|token|api_key|usr)=)[^&\s]+", re.I), r"\1[redacted]"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[user:pass]@"),
]


def scrub(text):
    text = str(text)
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def note_error(text):
    RECENT_ERRORS.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {scrub(text)[:300]}")


def _version(repo_dir):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", repo_dir, "rev-parse", "--short=12", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            dirty = subprocess.run(["git", "-C", repo_dir, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
            return f"git {r.stdout.strip()}" + (" (local changes)" if dirty else "")
    except Exception:
        pass
    import snapshot_update
    sha = snapshot_update.installed_commit(repo_dir)
    return f"zip install {sha[:12]}" if sha else "zip install (version not recorded)"


def connection_check(agenthc_host):
    """Diagnoses for the feed server and GitHub (updates), as text lines."""
    import net_check
    lines = []
    for host, name in ((agenthc_host, "AgentHC"), ("github.com", "GitHub (updates)")):
        try:
            d = net_check.diagnose(host)
            lines.append(net_check.summary(d, name=name) if not d["ok"]
                         else f"{name}: OK — {d['detail']}")
        except Exception as exc:
            lines.append(f"{name}: check failed ({exc})")
    if net_check.http.bypassed:
        lines.append(f"Note: the agent is bypassing a broken proxy setting "
                     f"({net_check.http.bypassed}).")
    return lines


def report(repo_dir, doctor_checks, cfg, agenthc_host):
    """The support report (str). `doctor_checks` = agent.doctor_checks."""
    import net_check
    out = ["agentic-trader support report",
           f"time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
           f"version: {_version(repo_dir)}",
           f"python: {sys.version.split()[0]} · {platform.system()} {platform.release()} "
           f"({platform.machine()})",
           ""]
    out.append("setup:")
    out.append(f"  source={cfg.get('source') or 'unset'} broker={cfg.get('broker') or 'unset'} "
               f"mode={'DRY-RUN' if cfg.get('dry_run', True) else 'LIVE'} "
               f"other_trades={'on' if cfg.get('include_other_trades') else 'off'}")
    access = ", ".join(k for k, on in (
        ("prepaid pass", cfg.get("feed_pass")),
        ("pass invoice waiting", cfg.get("feed_pass_pending")),
        ("agent wallet", cfg.get("lnbits_url")),
        ("recurring day-pass ON", cfg.get("day_pass_recurring")),
        ("Premium API key", cfg.get("agenthc_api_key"))) if on)
    out.append(f"  feed access: {access or 'none'}")
    out.append("")
    out.append("checks:")
    try:
        checks, _ = doctor_checks()
        for c in checks:
            line = f"  {'✓' if c['ok'] else '✗'} {c['label']}"
            if c.get("detail"):
                line += f" — {c['detail']}"
            out.append(line)
    except Exception as exc:
        out.append(f"  (checks failed to run: {exc})")
    out.append("")
    out.append("connection:")
    out.extend("  " + ln.replace("\n", "\n  ") for ln in connection_check(agenthc_host))
    proxy = net_check.proxy_for(f"https://{agenthc_host}")
    out.append(f"  proxy setting: {proxy or 'none'}")
    out.append("")
    out.append("recent errors:")
    out.extend(f"  {e}" for e in RECENT_ERRORS) if RECENT_ERRORS else out.append("  none")
    return "\n".join(scrub(line) for line in out)
