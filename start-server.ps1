<#
.SYNOPSIS
    Start the private Umamusume server.

.DESCRIPTION
    Wraps the uvicorn launch from server/README.md. Two reasons this exists:

      * `uvicorn` is NOT on PATH -- it lives inside the Windows Store Python
        3.12 install, so it must be invoked as `python -m uvicorn`.
      * That interpreter reports itself as `python3.12`, which is why
        `Get-Process python` never finds a running server. Checked here so a
        stale worker still holding port 443 is reported instead of showing up
        as a confusing bind error (that exact situation -- orphaned uvicorn
        workers racing for the socket -- has bitten this project before).

.EXAMPLE
    .\start-server.ps1              # normal run: use this while PLAYING
    .\start-server.ps1 -Reload      # auto-restart on code edits (DEV ONLY)
    .\start-server.ps1 -Force       # kill any existing server first
#>
param(
    [switch]$Reload,
    [switch]$Force,
    [switch]$IosCerts
)

$ErrorActionPreference = 'Stop'
$ServerDir = Join-Path $PSScriptRoot 'server'

# --- is one already running? ------------------------------------------------
$existing = @(Get-Process -ErrorAction SilentlyContinue |
              Where-Object { $_.ProcessName -like 'python*' })
if ($existing.Count -gt 0) {
    Write-Host "Found $($existing.Count) python process(es) already running:" -ForegroundColor Yellow
    $existing | Select-Object Id, ProcessName, StartTime | Format-Table -AutoSize | Out-String | Write-Host

    # STALE CODE CHECK. Python imports a module once, so a running server keeps
    # executing whatever the source said when it STARTED. This has already cost
    # two debugging sessions -- a fix looked broken when it simply was not
    # loaded. Compare the newest source file against the oldest server process.
    $newest = Get-ChildItem (Join-Path $ServerDir 'app') -Recurse -Filter *.py |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $oldestStart = ($existing | Sort-Object StartTime | Select-Object -First 1).StartTime
    if ($newest -and $oldestStart -and $newest.LastWriteTime -gt $oldestStart) {
        Write-Host ''
        Write-Host 'STALE: that server predates your newest source edit.' -ForegroundColor Red
        Write-Host ("  server started : {0}" -f $oldestStart) -ForegroundColor Red
        Write-Host ("  newest .py     : {0}  ({1})" -f $newest.LastWriteTime, $newest.Name) -ForegroundColor Red
        Write-Host '  It is running OLD code. Restart it (-Force) or use -Reload.' -ForegroundColor Red
        Write-Host ''
    }
    if ($Force) {
        Write-Host 'Stopping them (-Force)...' -ForegroundColor Yellow
        $existing | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 700
    } else {
        Write-Host 'If port 443 fails to bind, one of these is holding it.' -ForegroundColor Yellow
        Write-Host 'Re-run with -Force to stop them first.' -ForegroundColor Yellow
    }
}

# --- the DNS redirect must be ON or the client talks to the REAL server -----
# Read the hosts file DIRECTLY rather than shelling out to toggle-redirect.ps1:
# that script reports with Write-Host, which writes straight to the console and
# never reaches the pipeline, so capturing its output yields an empty string and
# this check fired a false "redirect is off" warning every single start. A
# warning that is always wrong is worse than no warning -- it trains you to
# ignore the real ones.
$hostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
$redirectOn = $false
if (Test-Path $hostsPath) {
    foreach ($line in (Get-Content $hostsPath -ErrorAction SilentlyContinue)) {
        if ($line -match 'api\.games\.umamusume\.com' -and $line -notmatch '^\s*#') {
            $redirectOn = $true
            break
        }
    }
}
if ($redirectOn) {
    Write-Host 'DNS redirect: on' -ForegroundColor Green
} else {
    Write-Host 'DNS redirect is NOT on -- the game will reach the REAL server.' -ForegroundColor Red
    Write-Host '  Run:  .\toggle-redirect.ps1 on' -ForegroundColor Red
}

# --- warn about live TESTING KNOBS -----------------------------------------
$cfgPath = Join-Path $ServerDir 'client_config.json'
if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    foreach ($knob in 'force_failure_rate', 'force_training_gain') {
        $val = $cfg.$knob
        if ($null -ne $val) {
            Write-Host "TESTING KNOB LIVE: $knob = $val  (set it to null for real play)" -ForegroundColor Magenta
        }
    }
}

# --- launch ----------------------------------------------------------------
Set-Location $ServerDir
# -IosCerts serves the CA-signed chain from tools\gen-ios-certs.ps1 instead of
# the original self-signed cert. iOS 13+ rejects that original one outright (no
# EKU serverAuth, CA:TRUE used as a leaf), so the phone needs this. Kept behind
# a switch rather than swapped in: the PC client trusts the ORIGINAL cert, and
# nothing on this box knows the new CA until you run gen-ios-certs.ps1
# -InstallToWindows. Serve the chain file, never the bare leaf -- iOS will not
# go looking for the issuer on its own.
if ($IosCerts) {
    $sslKey  = 'certs\ios\server.key.pem'
    $sslCert = 'certs\ios\server.chain.pem'
    if (-not (Test-Path (Join-Path $ServerDir $sslCert))) {
        throw "No iOS cert chain at $sslCert. Run: .\tools\gen-ios-certs.ps1"
    }
    Write-Host 'TLS: iOS chain (certs\ios\server.chain.pem)' -ForegroundColor Cyan
}
else {
    $sslKey  = 'certs\server.key.pem'
    $sslCert = 'certs\server.cert.pem'
}

$uvicornArgs = @(
    '-m', 'uvicorn', 'app.main:app',
    '--host', '0.0.0.0', '--port', '443',
    '--ssl-keyfile', $sslKey,
    '--ssl-certfile', $sslCert
)
if ($Reload) {
    # Watch ONLY the app package. Without --reload-dir uvicorn watches the whole
    # working directory, so writing a log, a fixture or a scratch file restarts
    # the server for no reason.
    $uvicornArgs += '--reload'
    $uvicornArgs += '--reload-dir'; $uvicornArgs += 'app'
    Write-Host ''
    Write-Host '--reload is ON. Read this once:' -ForegroundColor Yellow
    Write-Host '  * Saving any .py under server\app restarts the worker.' -ForegroundColor Yellow
    Write-Host '  * Career state lives in SQLite, so nothing is LOST -- but a' -ForegroundColor Yellow
    Write-Host '    request in flight at that moment fails, and the client can' -ForegroundColor Yellow
    Write-Host '    sit there stuck. Avoid it while actually playing.' -ForegroundColor Yellow
    Write-Host '  * The reloader runs a parent + a worker. If a worker is ever' -ForegroundColor Yellow
    Write-Host '    orphaned it keeps port 443 and the next start races it --' -ForegroundColor Yellow
    Write-Host '    that has bitten this project before. Recover with -Force.' -ForegroundColor Yellow
    Write-Host ''
}

Write-Host ''
# Prefer the project's own venv -- PATH's bare `python` can resolve to an
# unrelated install with no uvicorn (confirmed on this machine: PATH's
# python is a Windows Store/other install missing the package, while
# server\.venv has it). Fall back to PATH if the venv isn't there.
$venvPython = Join-Path $ServerDir '.venv\Scripts\python.exe'
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { 'python' }

Write-Host "Starting: $pythonExe " -NoNewline; Write-Host ($uvicornArgs -join ' ')
Write-Host 'Ctrl-C to stop.'
Write-Host ''
& $pythonExe @uvicornArgs
