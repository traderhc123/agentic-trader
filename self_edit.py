"""Self-modification — the agent changes its own production code.

Flow (human merge gate, deliberately):
  1. Owner asks in the dashboard chat:  code: add a source that reads RSS
  2. The agent's LLM (owner's API key) writes the change against its own
     source tree and files a PROPOSAL: explanation + full new file contents
     + a unified diff.
  3. The dashboard shows the diff. The owner clicks Apply — the agent backs
     up the old files, writes the new ones, compile-checks every changed
     .py (automatic rollback on failure), and restarts itself.

Why the gate: an autonomous trading agent that applies its own code edits
unattended is one prompt-injection away from deleting its own safety rails.
With the gate, the worst a bad proposal can do is sit unapplied in a diff
view. Enable with the dashboard command `self-edit on`.

Hard limits regardless of approval: edits stay inside the repo, never touch
.git/.venv or hidden files, and proposals touching DISCLAIMER.md or the
consent gate are flagged in red (the code refuses to *silently* alter them).
"""

import difflib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import threading
import time

MAX_FILE_BYTES = 120_000

# THE FOUNDATION — files the agent can NEVER rewrite about itself, no matter
# what it (or anything talking to it) proposes. These hold the consent gate,
# the safety rails, the legal terms, and this self-edit governor. Humans can
# still edit them manually; the foundation binds only the agent's own hand.
FOUNDATION = frozenset({
    "agent.py",          # consent gate, event safety pipeline, caps
    "self_edit.py",      # this governor (or it could ungate itself)
    "llm_policy.py",     # the veto-only policy brain
    "lightning_wallet.py",  # the spend path (a rewrite could drain the wallet)
    "DISCLAIMER.md",     # the legal terms the human accepted
    "SECURITY.md",       # the promises this table makes
    "install.sh",        # what new users pipe into bash
    "LICENSE",
})
# NOTE: brokers/ and sources/ are deliberately NOT locked — extending them is
# a supported use of self-edit. Order-placement code changes are still gated
# by human diff-review; the wallet spend path is not, hence its lock above.
_GUARDED_MARKERS = ("consent_ok", "require_consent_or_exit", "TERMS_VERSION")


def _repo():
    return os.path.dirname(os.path.abspath(__file__))


def _home():
    return os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))


def _proposal_path():
    return os.path.join(_home(), "proposal.json")


def _repo_files():
    """All editable source files (path -> content)."""
    out = {}
    for root, dirs, files in os.walk(_repo()):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"
                   and d != ".venv"]
        for name in files:
            if name.startswith(".") or not name.endswith((".py", ".md", ".txt",
                                                          ".yaml", ".service")):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, _repo())
            try:
                if os.path.getsize(path) <= MAX_FILE_BYTES:
                    with open(path) as f:
                        out[rel] = f.read()
            except (OSError, UnicodeDecodeError):
                continue
    return out


def enabled(cfg):
    return bool(cfg.get("self_edit_enabled"))


def current():
    try:
        with open(_proposal_path()) as f:
            return json.load(f)
    except Exception:
        return None


def reject():
    try:
        os.unlink(_proposal_path())
    except OSError:
        pass


def propose(cfg, request):
    """Ask the agent's LLM to write the change. Returns (ok, message)."""
    if not enabled(cfg):
        return False, ("Self-edit is off. Say 'self-edit on' first (you'll "
                       "approve every change as a diff before it applies).")
    if not (cfg.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY")):
        return False, "Connect your Anthropic API key first (Setup tab)."
    try:
        import anthropic
    except ImportError:
        return False, "pip install anthropic to enable self-edit."

    files = _repo_files()
    corpus = "\n\n".join(f"===== {p} =====\n{c}" for p, c in sorted(files.items()))
    schema = {
        "type": "object",
        "properties": {
            "explanation": {"type": "string"},
            "files": {"type": "array", "items": {
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False}},
        },
        "required": ["explanation", "files"],
        "additionalProperties": False,
    }
    try:
        client = anthropic.Anthropic(api_key=cfg.get("anthropic_api_key") or None)
        with client.messages.stream(
            model=cfg.get("llm_model", "claude-opus-4-8"),
            max_tokens=64000,
            thinking={"type": "adaptive"},
            system=(
                "You are the self-modification engine of agentic-trader, an "
                "open-source autonomous trading agent, editing YOUR OWN source "
                "tree at the owner's request. Rules: return the COMPLETE new "
                "content for every file you change or create (never fragments); "
                "change as little as possible; keep the consent gate, safety "
                "rails, and fail-soft error handling intact; never weaken "
                "DISCLAIMER.md or SECURITY.md protections; match the existing "
                "code style; the owner will review your diff before it applies. "
                "FOUNDATION FILES ARE READ-ONLY and any change to them will be "
                "rejected by the apply layer: agent.py, self_edit.py, "
                "llm_policy.py, DISCLAIMER.md, SECURITY.md, install.sh, LICENSE "
                "— treat them as fixed context and design around them. "
                "If the request is unsafe or impossible, return zero files and "
                "say why in the explanation."),
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content":
                       f"OWNER'S REQUEST:\n{request}\n\nCURRENT SOURCE TREE:\n"
                       f"{corpus}"}],
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return False, "The model declined that request."
        text = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
    except Exception as exc:
        return False, f"proposal failed: {str(exc)[:200]}"

    changes, warnings = [], []
    for f in result.get("files", []):
        rel = os.path.normpath(str(f.get("path", "")))
        if (rel.startswith("..") or os.path.isabs(rel)
                or rel.split(os.sep)[0] in (".git", ".venv")
                or any(part.startswith(".") for part in rel.split(os.sep))):
            return False, f"proposal rejected: illegal path {rel!r}"
        old = files.get(rel, "")
        new = str(f.get("content", ""))
        if old == new:
            continue
        if rel in FOUNDATION:
            return False, (f"proposal rejected: {rel} is a FOUNDATION file — "
                           "the agent cannot rewrite its own consent gate, "
                           "safety rails, terms, or this self-edit governor. "
                           "(You can still edit it manually.)")
        diff = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        if any(m in diff for m in _GUARDED_MARKERS):
            warnings.append(f"⚠️ references consent-gate internals: {rel}")
        changes.append({"path": rel, "content": new, "diff": diff})

    if not changes:
        return False, ("No changes proposed. Model says: "
                       + str(result.get("explanation", ""))[:400])

    proposal = {
        "request": request,
        "explanation": str(result.get("explanation", ""))[:2000],
        "warnings": warnings,
        "changes": changes,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    os.makedirs(_home(), exist_ok=True)
    with open(_proposal_path(), "w") as f:
        json.dump(proposal, f, indent=1)
    n = len(changes)
    return True, (f"Proposal ready: {n} file(s) changed. Review the diff in "
                  "the dashboard panel, then Apply or Reject."
                  + (" " + " ".join(warnings) if warnings else ""))


def apply_and_restart(restart=True):
    """Apply the pending proposal: backup -> write -> compile-check ->
    rollback on failure -> restart. Returns (ok, message)."""
    prop = current()
    if not prop:
        return False, "no pending proposal"

    # Re-validate paths at apply time — never trust the stored proposal file.
    repo_real = os.path.realpath(_repo())
    for ch in prop.get("changes", []):
        rel = os.path.normpath(str(ch.get("path", "")))
        dst_real = os.path.realpath(os.path.join(repo_real, rel))
        if (rel.startswith("..") or os.path.isabs(rel)
                or not dst_real.startswith(repo_real + os.sep)
                or any(part.startswith(".") for part in rel.split(os.sep))):
            reject()
            return False, f"apply refused: illegal path {rel!r} (proposal discarded)"
        if rel in FOUNDATION:
            reject()
            return False, (f"apply refused: {rel} is a FOUNDATION file "
                           "(proposal discarded)")

    backup_dir = os.path.join(_home(), "backups",
                              time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    os.makedirs(backup_dir, exist_ok=True)
    written = []
    try:
        for ch in prop["changes"]:
            dst = os.path.join(_repo(), ch["path"])
            os.makedirs(os.path.dirname(dst) or _repo(), exist_ok=True)
            if os.path.exists(dst):
                bak = os.path.join(backup_dir, ch["path"])
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                shutil.copy2(dst, bak)
            with open(dst, "w") as f:
                f.write(ch["content"])
            written.append(ch["path"])
        # compile-check every changed python file
        for ch in prop["changes"]:
            if ch["path"].endswith(".py"):
                py_compile.compile(os.path.join(_repo(), ch["path"]),
                                   doraise=True)
    except Exception as exc:
        # rollback
        for rel in written:
            bak = os.path.join(backup_dir, rel)
            dst = os.path.join(_repo(), rel)
            if os.path.exists(bak):
                shutil.copy2(bak, dst)
            elif os.path.exists(dst):
                os.unlink(dst)
        return False, f"apply FAILED and was rolled back: {str(exc)[:250]}"

    reject()  # consume the proposal
    if restart:
        def _restart():
            time.sleep(1.5)  # let the HTTP response flush
            os.execv(sys.executable, [sys.executable,
                                      os.path.join(_repo(), "agent.py"), "run"])
        threading.Thread(target=_restart, daemon=True).start()
        return True, (f"Applied {len(written)} file(s) (backup: {backup_dir}). "
                      "Restarting with the new code — this page will reconnect "
                      "in a few seconds.")
    return True, (f"Applied {len(written)} file(s) (backup: {backup_dir}). "
                  "Restart the agent to load the new code.")
