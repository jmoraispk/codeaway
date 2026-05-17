# Restart CodeAway (full process recycle).
#
# Use this when you've changed press_ui.py / press_engine.py / etc. and
# need the desktop process to pick up the new code. For bridge-only
# changes, the soft-reload endpoint (POST /api/admin/reload, or the
# Reload button in the phone settings drawer) is faster and keeps the
# desktop UI alive.
#
# What this script does:
#   1. Finds python.exe processes whose command line contains
#      `main.py` (the bridge is on by default now, so we don't grep
#      for the flag).
#   2. Stop-Process -Force on each (Qt subprocess dies with the parent).
#   3. Waits for the bridge port to free (up to 10 s).
#   4. Re-launches detached using the project's .venv\Scripts\python.exe
#      so we don't depend on `uv` being on PATH for the parent shell.
#   5. Probes /api/health until it responds (up to 25 s) and prints the
#      uptime so you can see it actually came up.
#
# Usage:
#   .\scripts\restart.ps1
#   .\scripts\restart.ps1 -Port 8000
#   .\scripts\restart.ps1 -RepoDir "C:\path\to\codeaway"

[CmdletBinding()]
param(
    [string]$RepoDir,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

# Default to the script's parent (the repo root) so this works without
# any -RepoDir override when the script lives at scripts\restart.ps1.
if (-not $RepoDir) {
    $RepoDir = Split-Path -Parent $PSScriptRoot
}

function Step($msg) { Write-Host ">> $msg" -ForegroundColor Cyan }
function Note($msg) { Write-Host "   $msg" -ForegroundColor DarkGray }

# Resolve python from the project's .venv. We use python.exe (not the
# windowless pythonw.exe) because pythonw maps stdout/stderr to NUL,
# which uvicorn / Qt / our own logging trip over during startup. The
# console window gets hidden via Start-Process -WindowStyle Hidden so
# the user-visible result is the same: the Qt window appears, no
# terminal in sight.
$python = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "python not found at $python — run 'uv sync' from $RepoDir first."
}

# 1. Find running CodeAway processes (parent + Qt subprocess share the
#    same command line, so this returns both).
Step "Looking for running CodeAway"
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*main.py*"
    }

if ($procs) {
    foreach ($p in $procs) {
        Note "PID $($p.ProcessId) (parent $($p.ParentProcessId))"
    }
    Step "Stopping"
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
            Note "Stop-Process for PID $($p.ProcessId) failed: $($_.Exception.Message)"
        }
    }

    # 2. Wait for the port to free up. Without this, Start-Process can race
    #    the kernel's TCP cleanup and the new uvicorn fails with a bind
    #    error that's invisible until you check the console window.
    Step "Waiting for port $Port to free"
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        if (-not $busy) { break }
        Start-Sleep -Milliseconds 200
    }
} else {
    Note "No running instance — going straight to launch."
}

# 3. Re-launch detached. Calling .venv\Scripts\python.exe directly skips
#    uv's wrapper (no stray cmd window for `uv run`). -WindowStyle Hidden
#    keeps the python console off-screen so the user only sees the Qt
#    window come up.
# Redirect stdout / stderr to a rotating log so a startup crash
# leaves breadcrumbs. The previous Hidden-console launch dropped
# both, which made "Bridge port never came up" a dead end. Keep one
# previous log around (.1) so a wedge-then-recovery sequence still
# has the wedge's output for inspection.
$logsDir = Join-Path $RepoDir "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }
$logOut = Join-Path $logsDir "service.out.log"
$logErr = Join-Path $logsDir "service.err.log"
foreach ($f in @($logOut, $logErr)) {
    if (Test-Path $f) {
        $bak = "$f.1"
        if (Test-Path $bak) { Remove-Item $bak -Force -ErrorAction SilentlyContinue }
        Move-Item $f $bak -Force -ErrorAction SilentlyContinue
    }
}

Step "Launching $python main.py (hidden console; logs -> logs\service.{out,err}.log)"
Start-Process `
    -FilePath $python `
    -ArgumentList "main.py" `
    -WorkingDirectory $RepoDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError $logErr | Out-Null

# 4. Two-stage readiness check. PySide6's cold import + Qt window
#    construction can take 30 s on first launch, and uvicorn binds the
#    port well before /api/health responds. Check the port first (cheap)
#    and only escalate to HTTP once the kernel has accepted the bind.
Step "Waiting for bridge on :$Port (up to 60s)"
$deadline = (Get-Date).AddSeconds(60)
$bound = $false
while ((Get-Date) -lt $deadline) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        $bound = $true
        break
    }
    Start-Sleep -Milliseconds 500
}
if (-not $bound) {
    Write-Host "Bridge port never came up — check logs\service.err.log." -ForegroundColor Yellow
    if (Test-Path $logErr) {
        $tail = Get-Content $logErr -Tail 30 -ErrorAction SilentlyContinue
        if ($tail) {
            Write-Host "--- tail of service.err.log ---" -ForegroundColor DarkYellow
            $tail | ForEach-Object { Write-Host $_ }
        }
    }
    exit 1
}
Note "Port bound, probing /api/health"

$ok = $false
$body = $null
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$Port/api/health" -UseBasicParsing -TimeoutSec 4
        if ($r.StatusCode -eq 200) {
            $ok = $true
            $body = $r.Content
            break
        }
    } catch {
        # First few requests can hang while uvicorn is still settling
        # event-loop callbacks — keep polling until the deadline.
    }
    Start-Sleep -Milliseconds 500
}

if ($ok) {
    Write-Host "OK" -ForegroundColor Green
    Note $body
} else {
    Write-Host "Port is bound but /api/health didn't respond — service may be wedged." -ForegroundColor Yellow
    exit 1
}
