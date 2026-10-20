# agentic-trader installer for Windows - clones the repo and installs dependencies.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.ps1 | iex"
#
# Windows twin of install.sh. Self-sufficient: if Python 3.10+ isn't on the
# machine, it installs a private Python via uv (no admin rights, no system
# changes beyond your user profile). This installer does NOT run the agent and
# does NOT accept any agreement for you - trading requires the interactive
# consent gate in `agent.py setup`, which you must complete yourself afterwards.
$ErrorActionPreference = "Stop"

$Repo = "https://github.com/traderhc123/agentic-trader"
$Dir = if ($env:AGENTIC_TRADER_DIR) { $env:AGENTIC_TRADER_DIR } else { Join-Path $HOME "agentic-trader" }

Write-Host "== agentic-trader installer (Windows) =="

function Test-Py($exe, $extra) {
    try {
        & $exe @extra -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

# The Microsoft Store "python.exe" alias is a stub that opens the Store -
# Test-Py rejects it because it exits non-zero without running the probe.
$Python = $null; $PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in "-3.13", "-3.12", "-3.11", "-3.10") {
        if (Test-Py "py" @($v)) { $Python = "py"; $PyArgs = @($v); break }
    }
}
if (-not $Python) {
    foreach ($c in "python3", "python") {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Py $cmd.Source @())) { $Python = $cmd.Source; break }
    }
}
if (-not $Python) {
    Write-Host "Python 3.10+ not found - installing a private Python 3.12 via uv (no admin)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = (Join-Path $HOME ".local\bin") + ";" + $env:Path
    uv python install 3.12
    $Python = (uv python find 3.12).Trim()
    if (-not (Test-Py $Python @())) { throw "automatic Python install failed" }
    Write-Host "Using $Python"
}

# git is preferred but NOT required - without it we download a snapshot zip
# (Expand-Archive ships with Windows). `.venv` and local edits are left in place.
function Get-Snapshot {
    Write-Host "git not found - downloading a snapshot of the code instead..."
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("agentic-trader-" + [Guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp "src.zip"
    Invoke-WebRequest "https://codeload.github.com/traderhc123/agentic-trader/zip/refs/heads/main" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $tmp
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $src = Get-ChildItem $tmp -Directory | Where-Object Name -like "agentic-trader-*" | Select-Object -First 1
    Copy-Item -Path (Join-Path $src.FullName "*") -Destination $Dir -Recurse -Force
    Remove-Item $tmp -Recurse -Force
}

if (Get-Command git -ErrorAction SilentlyContinue) {
    if (Test-Path (Join-Path $Dir ".git")) {
        Write-Host "Existing install found at $Dir - updating..."
        git -C $Dir pull --ff-only
    } elseif (Test-Path (Join-Path $Dir "agent.py")) {
        Get-Snapshot   # zip install being re-run on a box that now has git
    } else {
        git clone --depth 1 $Repo $Dir
    }
} else {
    Get-Snapshot
}

Set-Location $Dir
$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not ((Test-Path $VenvPy) -and (Test-Py $VenvPy @()))) {
    if (Test-Path ".venv") { Remove-Item ".venv" -Recurse -Force }
    & $Python @PyArgs -m venv .venv
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r requirements.txt

Write-Host ""
Write-Host "Installed to $Dir"
Write-Host ""

# One-paste experience: launch the browser setup wizard now. The wizard
# collects consent IN THE BROWSER (you still read DISCLAIMER.md and type the
# acceptance phrase yourself), so auto-launching does not bypass the consent
# gate. Opt out with AGENTIC_TRADER_NO_LAUNCH=1.
if (-not $env:AGENTIC_TRADER_NO_LAUNCH) {
    Write-Host "Launching the setup wizard in your browser..."
    Write-Host "(read DISCLAIMER.md in the page and accept it yourself; Ctrl-C here to stop)"
    Write-Host "Keep this window open until you click 'Start my agent now' - after that"
    Write-Host "you can close it: the agent keeps running in the background."
    Write-Host ""
    & $VenvPy agent.py setup --web
} else {
    Write-Host "Next steps (run these yourself - the agent will not act without your"
    Write-Host "explicit, in-browser consent to DISCLAIMER.md):"
    Write-Host ""
    Write-Host "  cd $Dir"
    Write-Host "  .\.venv\Scripts\python.exe agent.py setup --web   # browser wizard"
    Write-Host "  .\.venv\Scripts\python.exe agent.py doctor        # verify everything is ready"
    Write-Host "  .\.venv\Scripts\python.exe agent.py start         # run in the background + dashboard"
    Write-Host "  .\.venv\Scripts\python.exe agent.py autostart on  # optional: start at every login"
    Write-Host ""
    Write-Host "Not investment advice. You accept all liability - read DISCLAIMER.md."
}
