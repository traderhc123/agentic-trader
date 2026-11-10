"""Code updates for installs WITHOUT git (the Windows zip install, or git
missing from PATH): the dashboard's Update button works there too.

Same rules as the git path (webui.check_code_updates / pull_code_updates):
  - code only ever comes from the official repository on GitHub;
  - the user sees the commit list first, and the pull installs EXACTLY the
    commit they reviewed (the zip is fetched by that commit's SHA);
  - that commit must be the one THIS agent's own last check found at the
    head of the official branch. A commit id is never taken on the browser's
    word: GitHub serves any commit of any fork under the official repo's
    download address, so "whatever SHA the page sends" would be "any code";
  - nothing is installed automatically.

The install is all-or-nothing. The whole snapshot is first written to
`.update-staging/`; only then are files swapped in, and if any swap fails
(disk full, a file locked by antivirus) the ones already swapped are put
back, so the agent is never left on a half-old, half-new mix.

Before a file is overwritten, a copy of it is kept under
`.update-backup/<timestamp>/` if its content differs (self-edits or local
changes are never silently lost). Files removed upstream are left in place,
like the installer does. The installed commit is recorded in
`.installed_commit` so the next check can list only what's new.
"""

import io
import os
import re
import shutil
import time
import zipfile

REPO = "traderhc123/agentic-trader"
API = f"https://api.github.com/repos/{REPO}"
ZIP_URL = "https://codeload.github.com/" + REPO + "/zip/{sha}"
VERSION_FILE = ".installed_commit"
BACKUP_DIR = ".update-backup"
STAGING_DIR = ".update-staging"
_SKIP = {".venv", ".git", "__pycache__", BACKUP_DIR, STAGING_DIR}
# install folder -> the commit this process's last check() saw at the head of
# the official branch. In memory on purpose: nothing on disk (or sent by the
# page) can name a commit to install; after a restart, check again.
_CHECKED = {}
_MAX_ZIP_BYTES = 50 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}$")


def installed_commit(repo_dir):
    try:
        with open(os.path.join(repo_dir, VERSION_FILE), encoding="utf-8") as f:
            sha = f.read().strip().lower()
        return sha if _SHA.match(sha) else ""
    except OSError:
        return ""


def _key(repo_dir):
    return os.path.normcase(os.path.abspath(repo_dir))


def _get(url, **kw):
    import requests
    return requests.get(url, headers={"Accept": "application/vnd.github+json",
                                      "User-Agent": "agentic-trader-updater"},
                        timeout=kw.pop("timeout", 30), **kw)


def check(repo_dir, branch="main"):
    """Same shape as webui.check_code_updates(). Raises on network errors
    (the caller turns those into a diagnosis)."""
    r = _get(f"{API}/commits", params={"sha": branch, "per_page": 30})
    if r.status_code == 403 and "rate limit" in r.text.lower():
        return {"ok": False, "error": "GitHub's hourly limit for update checks "
                "was reached from this network — try again in an hour"}
    r.raise_for_status()
    commits = r.json()
    if not commits:
        return {"ok": False, "error": "GitHub returned no commits"}
    shas = [c["sha"] for c in commits]
    latest = shas[0]
    _CHECKED[_key(repo_dir)] = str(latest).strip().lower()
    cur = installed_commit(repo_dir)
    if cur == latest:
        return {"ok": True, "behind": 0, "upstream": f"github/{branch}",
                "summary": "", "target": latest, "mode": "snapshot"}
    known = cur in shas
    new = commits[:shas.index(cur)] if known else commits[:20]
    summary = "\n".join(f"{c['sha'][:8]} {c['commit']['message'].splitlines()[0]}"
                        for c in new)
    if not known:
        summary = ("(this install's version isn't recorded — showing the latest "
                   "changes; updating installs the newest code)\n" + summary)
    return {"ok": True, "behind": len(new), "upstream": f"github/{branch}",
            "summary": summary, "target": latest, "mode": "snapshot",
            "version_unknown": not known}


def _members(zf):
    """(member, relative path) pairs inside the zip's single top folder,
    refusing anything that would land outside the install."""
    out, top = [], None
    for m in zf.infolist():
        if m.is_dir():
            continue
        parts = m.filename.replace("\\", "/").split("/")
        # every entry must sit under ONE "agentic-trader-<sha>/" folder
        if top is None:
            top = parts[0]
        if (parts[0] != top or not top.startswith("agentic-trader-")
                or len(parts) < 2):
            raise ValueError(f"unsafe path in update archive: {m.filename!r}")
        rel = parts[1:]
        if any(p in ("", ".", "..") for p in rel) or ":" in m.filename:
            raise ValueError(f"unsafe path in update archive: {m.filename!r}")
        if rel[0] in _SKIP or "__pycache__" in rel:
            continue
        out.append((m, os.path.join(*rel)))
    return out


def pull(repo_dir, expect):
    """Install exactly commit `expect`, which must be the commit this
    agent's last check() reported. All-or-nothing. Returns (ok, message)."""
    expect = str(expect or "").strip().lower()
    if not _SHA.match(expect):
        return False, "check for updates first, review the commit list, then pull"
    if _CHECKED.get(_key(repo_dir)) != expect:
        return False, ("that isn't the version this agent last checked (the "
                       "agent restarted, or a newer update came out since) — "
                       "press 'Check for code updates' again, review the "
                       "list, then install. Nothing was changed.")
    r = _get(ZIP_URL.format(sha=expect), timeout=120, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(64 * 1024):
        buf.write(chunk)
        if buf.tell() > _MAX_ZIP_BYTES:
            return False, "update archive is unexpectedly large — not installed"
    try:
        zf = zipfile.ZipFile(buf)
        members = _members(zf)
    except (zipfile.BadZipFile, ValueError) as exc:
        return False, f"update archive rejected: {exc}"
    if not any(rel == "agent.py" for _m, rel in members):
        return False, "update archive doesn't look like agentic-trader — not installed"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(repo_dir, BACKUP_DIR, stamp)
    staging = os.path.join(repo_dir, STAGING_DIR)
    # 1) Stage every file that differs. Nothing the agent runs is touched yet.
    shutil.rmtree(staging, ignore_errors=True)  # left by an interrupted run
    todo = []  # (rel, staged copy, destination, had a previous version)
    try:
        for m, rel in members:
            data = zf.read(m)
            dest = os.path.join(repo_dir, rel)
            try:
                with open(dest, "rb") as f:
                    old = f.read()
            except OSError:
                old = None
            if old == data:
                continue
            staged = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            with open(staged, "wb") as f:
                f.write(data)
            todo.append((rel, staged, dest, old is not None))
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return False, (f"the update could not be prepared ({str(exc)[:150]}) "
                       "— nothing was changed")
    # 2) Swap them in; on any failure put back what was already swapped.
    done = []
    try:
        for rel, staged, dest, had_old in todo:
            if had_old:
                os.makedirs(os.path.dirname(os.path.join(backup, rel)), exist_ok=True)
                shutil.copy2(dest, os.path.join(backup, rel))
            os.makedirs(os.path.dirname(dest) or repo_dir, exist_ok=True)
            os.replace(staged, dest)
            # only now: a file whose swap failed (e.g. held open by antivirus
            # on Windows) was never changed and needs no putting back
            done.append((rel, dest, had_old))
        version_tmp = os.path.join(staging, VERSION_FILE)
        os.makedirs(staging, exist_ok=True)
        with open(version_tmp, "w", encoding="utf-8") as f:
            f.write(expect + "\n")
        os.replace(version_tmp, os.path.join(repo_dir, VERSION_FILE))
    except Exception as exc:
        stuck = []
        for rel, dest, had_old in reversed(done):
            try:
                if had_old:
                    shutil.copy2(os.path.join(backup, rel), dest)
                elif os.path.exists(dest):
                    os.unlink(dest)
            except OSError:
                stuck.append(rel)
        shutil.rmtree(staging, ignore_errors=True)
        if stuck:
            return False, (
                f"the update failed part-way ({str(exc)[:150]}) and these "
                f"files could not be put back: {', '.join(stuck[:10])}. Their "
                f"previous versions are in {BACKUP_DIR}/{stamp} — copy them "
                "back before restarting the agent.")
        return False, (f"the update could not be installed ({str(exc)[:150]}) "
                       "and was undone — the agent is still on the version "
                       "it had")
    shutil.rmtree(staging, ignore_errors=True)
    changed = len(todo)
    backed = sum(1 for _rel, _staged, _dest, had_old in todo if had_old)
    note = f"updated to {expect[:12]} ({changed} file(s) changed"
    if backed:
        note += f"; previous versions saved in {BACKUP_DIR}/{stamp}"
    return True, note + ")"
