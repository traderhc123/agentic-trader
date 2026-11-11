"""Self-modification — the agent changes its own production code.

Flow (human merge gate, deliberately):
  1. Owner asks in the dashboard chat:  code: add a source that reads RSS
  2. The agent's LLM (owner's API key) writes the change against its own
     source tree and files a PROPOSAL: explanation + full new file contents
     + a unified diff.
  3. The dashboard shows the diff. The owner clicks Apply — the agent backs
     up the old files, writes the new ones, compile-checks every changed
     .py, makes sure the new code still imports in a throwaway process
     (automatic rollback if either check fails), and restarts itself.

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
    "install.ps1",       # ...and the Windows twin piped into PowerShell
    "LICENSE",
    "webui.py",          # dashboard: dry-run toggle, updates, consent page
    "keep_running.py",   # one-copy lock (a second copy would double every order)
    "broker_events.py",  # how dropped trades reach the owner (can't be muted)
    "rules.py",          # the owner's built-in entry rules
    "dry_run_preview.py",  # what the dry-run preview claims the rules would do
    "view_model.py",     # what the dashboard says the agent is doing and will do
    "transfer.py",       # exports the broker login + wallet keys (move file)
    "platform_compat.py",  # process launch/restart plumbing
    "llm_client.py",     # carries the AI key; the policy brain's only door
    "setup_plan.py",     # enforces what an assistant may never set (go live…)
    "requirements.txt",  # what the Update button pip-installs
})

# Whole folders under the same rule: the dashboard + wizard app (its source
# and the built files the server actually runs). A rewrite there could flip
# the go-live confirmation or the consent screen just like webui.py could.
FOUNDATION_DIRS = ("webui_app", "webui_dist")

# Order-placement and signal code: EXISTING modules here are locked (a
# proposal could otherwise change how orders are placed or strip the auto-pay
# cap), but the agent may still ADD new adapters — a new file can't alter an
# existing order path. The registries (__init__.py) stay editable so a new
# adapter can be wired in; changes to them are flagged in the review panel.
LOCKED_DIRS = ("brokers", "sources")
_REGISTRY = "__init__.py"

# The agent never sells to open (brokers/order_guard.py): every order it
# sends goes through order_guard.check_order, inside the function that
# sends it. A broker file the agent writes (a new adapter, the registry)
# that looks able to send an order but never calls the check is refused.
_ORDER_MARKERS = ("place_order", "submit_order", "create_order", "/orders",
                  "place_option_order", "call_tool(", "requests.post", ".post(")


def _order_path_problem(rel, content):
    """Why a proposed file must not be applied because it could send broker
    orders around the never-sell-to-open check ("" = fine)."""
    rel = str(rel).replace(os.sep, "/").lower()
    if not (rel.startswith("brokers/") and rel.endswith(".py")):
        return ""
    text = str(content or "")
    if "order_guard.check_order(" in text:
        return ""
    hit = next((m for m in _ORDER_MARKERS if m in text), None)
    if hit is None:
        return ""
    return (f"{rel} looks able to send broker orders ({hit!r}) but never calls "
            "order_guard.check_order() — every order must go through that "
            "check, inside the function that sends it, so the agent can never "
            "sell to open")


def _is_foundation(rel):
    """True if `rel` names a FOUNDATION file on THIS filesystem. Windows and
    stock macOS are case-insensitive, and Windows also silently drops trailing
    dots/spaces from names — so a plain `rel in FOUNDATION` would let
    `Agent.py` or `agent.py.` overwrite the real consent gate."""
    norm = os.path.normcase(rel.rstrip(". "))
    top = norm.replace("\\", "/").split("/")[0].rstrip(". ").casefold()
    if any(top == d.casefold() for d in FOUNDATION_DIRS):
        return True
    return any(norm.casefold() == os.path.normcase(f).casefold()
               for f in FOUNDATION)


def _locked_module(rel, repo):
    """True for an EXISTING file under brokers/ or sources/ (except the
    registries). Matches case-insensitively for Windows/macOS."""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0].casefold() not in LOCKED_DIRS:
        return False
    if parts[-1].casefold() == _REGISTRY:
        return False
    # Walk the tree case-insensitively: `Brokers/Alpaca.py` IS brokers/alpaca.py
    # on Windows/macOS, so test against the real names, not os.path.exists.
    cur = repo
    for part in parts:
        try:
            match = next((e for e in os.listdir(cur)
                          if e.casefold() == part.casefold()), None)
        except OSError:
            return False
        if match is None:
            return False
        cur = os.path.join(cur, match)
    return True


def _illegal_name(rel):
    """Path parts Windows would reinterpret: NTFS alternate data streams
    (`agent.py:x`), drive-relative paths (`C:foo`) and trailing dots/spaces."""
    parts = rel.replace("\\", "/").split("/")
    return any(":" in p or (p != p.rstrip(". ") and p not in (".", ".."))
               for p in parts)


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
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("__pycache__", ".venv", "node_modules")]
        for name in files:
            if name.startswith(".") or not name.endswith((".py", ".md", ".txt",
                                                          ".yaml", ".service")):
                continue
            path = os.path.join(root, name)
            # forward slashes on every OS: these names go to the model and
            # into proposals, and must look the same as on Linux/macOS
            rel = os.path.relpath(path, _repo()).replace(os.sep, "/")
            try:
                if os.path.getsize(path) <= MAX_FILE_BYTES:
                    with open(path, encoding="utf-8") as f:
                        out[rel] = f.read()
            except (OSError, UnicodeDecodeError):
                continue
    return out


def enabled(cfg):
    return bool(cfg.get("self_edit_enabled"))


def current():
    try:
        with open(_proposal_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


SHOWN_DIFF_MAX = 200000  # characters of diff the review shows


def shown_diff(p):
    """The diff of a proposal as the dashboard shows it for review (GET
    /api/proposal): cut at SHOWN_DIFF_MAX characters (diff_truncated)."""
    return "\n".join(c["diff"] for c in p.get("changes", []))[:SHOWN_DIFF_MAX]


def _full_diff(p):
    return "\n".join(str(c.get("diff", "")) if isinstance(c, dict) else ""
                     for c in p.get("changes", []))


def diff_truncated(p):
    """True when the review shows only part of the diff: such a change was
    never all shown, so /v2 never applies it."""
    return len(_full_diff(p)) > SHOWN_DIFF_MAX


def diff_sha256(p):
    """sha256 of the WHOLE diff: what /v2 binds its Apply confirmation to.
    When the review shows all of it (not diff_truncated), that is the sha256
    of the text shown; a cut one never matches what a page shows. With
    base_problem() it also binds the files' new contents: each diff must be
    the one its content makes against the file as it is."""
    import hashlib
    return hashlib.sha256(_full_diff(p).encode("utf-8")).hexdigest()


def _diff_text(rel, old, new):
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def base_problem(p):
    """"" when every change's diff is exactly what its new content makes
    against the file as it is NOW; else why not. Apply writes whole files,
    so a change made against older code (an update installed since, an edit
    by hand) would quietly undo that: it must be asked for again."""
    repo_real = os.path.realpath(_repo())
    for ch in p.get("changes", []):
        if not (isinstance(ch, dict) and all(isinstance(ch.get(k), str)
                                             for k in ("path", "content", "diff"))):
            return "the proposal file is damaged"
        rel = ch["path"]
        dst = os.path.realpath(os.path.join(repo_real, os.path.normpath(rel)))
        if not dst.startswith(repo_real + os.sep):
            return f"illegal path {rel!r}"
        try:
            if os.path.exists(dst):
                with open(dst, encoding="utf-8") as f:
                    now = f.read()
            else:
                now = ""
        except (OSError, UnicodeDecodeError):
            return f"{rel} can't be read to check it"
        if _diff_text(rel, now, ch["content"]) != ch["diff"]:
            return (f"{rel} changed since this was proposed (an update, or an "
                    "edit by hand), and applying it would undo that")
    return ""


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
    import llm_client
    if not llm_client.configured(cfg):
        return False, "Connect an AI model first (Setup → Connect your AI)."

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
    system = (
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
        "llm_policy.py, llm_client.py, lightning_wallet.py, webui.py, "
        "platform_compat.py, keep_running.py, broker_events.py, rules.py, transfer.py, requirements.txt, DISCLAIMER.md, "
        "SECURITY.md, install.sh, install.ps1, LICENSE, and every "
        "EXISTING file in brokers/ and sources/ (you may ADD new "
        "adapter files there and register them in __init__.py) "
        "— treat them as fixed context and design around them. "
        "If the request is unsafe or impossible, return zero files and "
        "say why in the explanation.")
    try:
        result = llm_client.complete_json(
            cfg, system,
            f"OWNER'S REQUEST:\n{request}\n\nCURRENT SOURCE TREE:\n{corpus}",
            schema, max_tokens=64000, long=True, timeout=900)
    except llm_client.LLMError as exc:
        if exc.reason == "refused":
            return False, "The model declined that request."
        return False, f"proposal failed: {str(exc)[:200]}"
    except Exception as exc:
        return False, f"proposal failed: {str(exc)[:200]}"

    files_out = result.get("files", [])
    if not isinstance(files_out, list) or not all(isinstance(f, dict) for f in files_out):
        return False, "proposal failed: the model's reply had no usable file list"
    changes, warnings = [], []
    for f in files_out:
        raw = str(f.get("path", ""))
        rel = os.path.normpath(raw)
        if (rel.startswith("..") or os.path.isabs(rel) or _illegal_name(raw)
                or rel.split(os.sep)[0] in (".git", ".venv")
                or any(part.startswith(".") for part in rel.split(os.sep))):
            return False, f"proposal rejected: illegal path {rel!r}"
        # _repo_files() keys use forward slashes on every OS; normpath gives
        # backslashes on Windows. Without this, every file in a folder was
        # compared against "" there and shown to the owner as wholly new.
        rel = rel.replace(os.sep, "/")
        old = files.get(rel, "")
        new = str(f.get("content", ""))
        if old == new:
            continue
        if _is_foundation(rel):
            return False, (f"proposal rejected: {rel} is a FOUNDATION file — "
                           "the agent cannot rewrite its own consent gate, "
                           "safety rails, terms, or this self-edit governor. "
                           "(You can still edit it manually.)")
        problem = _order_path_problem(rel, new)
        if problem:
            return False, f"proposal rejected: {problem}"
        diff = _diff_text(rel, old, new)
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
    with open(_proposal_path(), "w", encoding="utf-8") as f:
        json.dump(proposal, f, indent=1)
    n = len(changes)
    return True, (f"Proposal ready: {n} file(s) changed. Review the diff in "
                  "the dashboard panel, then Apply or Reject."
                  + (" " + " ".join(warnings) if warnings else ""))


def _same_as_foundation(dst_real, repo_real):
    """Filesystem-level backstop for _is_foundation: does `dst_real` resolve
    to the same file as any FOUNDATION file (hard links, 8.3 short names),
    or land inside a FOUNDATION folder (e.g. through a symlinked directory)?"""
    for d in FOUNDATION_DIRS:
        dr = os.path.normcase(os.path.realpath(os.path.join(repo_real, d)))
        if os.path.normcase(dst_real).startswith(dr + os.sep):
            return True
    if not os.path.exists(dst_real):
        return False
    for f in FOUNDATION:
        fp = os.path.join(repo_real, f)
        try:
            if os.path.exists(fp) and os.path.samefile(dst_real, fp):
                return True
        except OSError:
            continue
    return False


_IMPORT_CHECK_TIMEOUT_S = 120
_IMPORT_CHECK = (
    "import importlib, os, sys\n"
    "sys.path.insert(0, os.getcwd())\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        importlib.import_module(name)\n"
    "    except BaseException as exc:\n"
    "        print('%s: %s: %s' % (name, type(exc).__name__, exc))\n"
    "        raise SystemExit(1)\n")


def _module_name(rel):
    """Dotted module name for a changed file the agent could import, else
    None (not Python, not a valid module path, or a test file — those need
    pytest, which an owner's install doesn't have)."""
    parts = rel.replace("\\", "/").split("/")
    if not parts[-1].endswith(".py"):
        return None
    parts[-1] = parts[-1][:-3]
    if (parts[0] == "tests" or parts[-1].startswith("test_")
            or parts[-1] == "conftest"):
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def _app_modules():
    """Names of this install's own modules that the running agent can load:
    whatever agent.py and webui.py import, directly or through each other,
    including imports written inside functions. Found by READING the source —
    nothing is run."""
    import ast
    repo = _repo()

    def source_of(name):
        base = os.path.join(repo, *name.split("."))
        for path in (base + ".py", os.path.join(base, "__init__.py")):
            if os.path.isfile(path):
                return path
        return None

    loaded, todo = set(), ["agent", "webui"]
    while todo:
        name = todo.pop()
        path = None if name in loaded else source_of(name)
        if path is None:
            continue  # seen already, or not this install's code
        loaded.add(name)
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, ValueError, SyntaxError):
            continue
        package = name.split(".")
        if os.path.basename(path) != "__init__.py":
            package = package[:-1]
        for node in ast.walk(tree):
            found = []
            if isinstance(node, ast.Import):
                found = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = package[:len(package) - node.level + 1] if node.level else []
                base = ".".join(base + (node.module.split(".") if node.module else []))
                found = [base] + [f"{base}.{a.name}" if base else a.name
                                  for a in node.names]
            elif (isinstance(node, ast.Call) and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)
                  and getattr(node.func, "id", getattr(node.func, "attr", ""))
                  in ("__import__", "import_module")):
                found = [node.args[0].value]
            for mod in found:
                parts = [p for p in mod.split(".") if p]
                todo.extend(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return loaded


def _import_problem(paths):
    """Import the changed modules that the agent itself loads, then the agent
    and its dashboard, in a separate Python process. None if all of it loads;
    otherwise what failed.

    A compile check only proves the syntax: a missing package or a renamed
    function still compiles, and then the restarted agent dies on import —
    over and over, with no dashboard left to undo the change from. If the
    check itself cannot run, that counts as a failure too (the change is
    rolled back rather than trusted).

    A changed file that nothing in the agent imports (a script the owner
    asked for, to run by hand) is NOT imported here: importing runs it, with
    the owner's live broker login. Approving a change saves such a file; it
    never runs it."""
    loads = _app_modules()
    names = [m for m in map(_module_name, paths) if m and m in loads]
    for top in ("agent", "webui"):
        if top not in names and os.path.exists(os.path.join(_repo(), top + ".py")):
            names.append(top)
    if not names:
        return None
    try:
        r = subprocess.run(
            [sys.executable, "-B", "-c", _IMPORT_CHECK, *names], cwd=_repo(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_IMPORT_CHECK_TIMEOUT_S,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return ("the new code did not finish loading within "
                f"{_IMPORT_CHECK_TIMEOUT_S}s")
    except OSError as exc:
        return f"could not check that the new code loads ({exc})"
    if r.returncode == 0:
        return None
    lines = ((r.stdout or "").strip() or (r.stderr or "").strip()).splitlines()
    return ("the new code does not load — "
            + (lines[-1] if lines else f"exit code {r.returncode}"))


def _app_port():
    try:
        import webui
        return webui.APP_PORT
    except Exception:
        return 8721


def apply_and_restart(restart=True, expect_sha256=None):
    """Apply the pending proposal: backup -> write -> compile-check ->
    import-check -> rollback on failure -> restart. Returns (ok, message).
    expect_sha256: apply only the proposal whose whole diff has this sha256
    (diff_sha256), the one the owner reviewed, and only if all of it was
    shown and it was made against the files as they are now (base_problem);
    any other is left as it is."""
    prop = current()
    if not prop:
        return False, "no pending proposal"
    if expect_sha256 is not None:
        if diff_truncated(prop):
            return False, ("this change is too large to review in the "
                           "dashboard — nothing was applied; reject it and ask "
                           "for a smaller change")
        if diff_sha256(prop) != expect_sha256:
            return False, ("the proposal waiting is not the one you reviewed — "
                           "nothing was applied; review it again")
        problem = base_problem(prop)
        if problem:
            return False, (f"{problem} — nothing was applied; reject it and "
                           "ask again")

    # Re-validate paths at apply time — never trust the stored proposal file.
    repo_real = os.path.realpath(_repo())
    for ch in prop.get("changes", []):
        raw = str(ch.get("path", ""))
        rel = os.path.normpath(raw)
        dst_real = os.path.realpath(os.path.join(repo_real, rel))
        if (rel.startswith("..") or os.path.isabs(rel) or _illegal_name(raw)
                or not dst_real.startswith(repo_real + os.sep)
                or any(part.startswith(".") for part in rel.split(os.sep))):
            reject()
            return False, f"apply refused: illegal path {rel!r} (proposal discarded)"
        if _is_foundation(rel) or _same_as_foundation(dst_real, repo_real):
            reject()
            return False, (f"apply refused: {rel} is a FOUNDATION file "
                           "(proposal discarded)")
        if _locked_module(rel, repo_real):
            reject()
            return False, (f"apply refused: {rel} is an existing broker/source "
                           "module (proposal discarded)")
        problem = _order_path_problem(rel, ch.get("content"))
        if problem:
            reject()
            return False, f"apply refused: {problem} (proposal discarded)"

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
            with open(dst, "w", encoding="utf-8") as f:
                f.write(ch["content"])
            written.append(ch["path"])
        # compile-check every changed python file
        for ch in prop["changes"]:
            if ch["path"].endswith(".py"):
                py_compile.compile(os.path.join(_repo(), ch["path"]),
                                   doraise=True)
        # ...and prove the result still loads before restarting onto it
        problem = _import_problem([ch["path"] for ch in prop["changes"]])
        if problem:
            raise RuntimeError(problem)
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
            argv = [os.path.join(_repo(), "agent.py"), "run"]
            try:
                # never restarts in the middle of an order; tells the owner
                # if it had to give up
                import webui
                tell = webui.restart_after_self_edit
            except Exception:
                import platform_compat
                platform_compat.restart_self(argv, _app_port())
                return
            tell(argv, _app_port())
        threading.Thread(target=_restart, daemon=True).start()
        return True, (f"Applied {len(written)} file(s) (backup: {backup_dir}). "
                      "Restarting with the new code — this page will reconnect "
                      "in a few seconds.")
    return True, (f"Applied {len(written)} file(s) (backup: {backup_dir}). "
                  "Restart the agent to load the new code.")
