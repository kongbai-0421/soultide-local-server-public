#Requires -Version 5.1
<#
.SYNOPSIS
    Stop all Soul Tide local server services.
.DESCRIPTION
    Stops SDK, HTTP login, TCP game, and DNS servers by reading their PID files.
    Also cleans up background logging jobs.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$services = @("mumu_route_watchdog", "sdk_server", "login_server", "http_compat_proxy", "tcp_server", "dns_server")

Write-Host "=== Stopping Soul Tide Stack ===" -ForegroundColor Cyan

$anyStopped = $false
foreach ($name in $services) {
    $pidFile = Join-Path $root "${name}.pid"

    # Clean up monitoring/logging jobs
    foreach ($suffix in @(".out.job", ".err.job")) {
        $jobFile = Join-Path $root "${name}$suffix"
        if (Test-Path $jobFile) {
            $jobId = Get-Content $jobFile -Raw -ErrorAction SilentlyContinue
            if ($jobId) {
                try { Stop-Job -Id $jobId -ErrorAction SilentlyContinue | Out-Null } catch {}
                try { Remove-Job -Id $jobId -ErrorAction SilentlyContinue | Out-Null } catch {}
            }
            Remove-Item $jobFile -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-Path $pidFile)) {
        Write-Host "  [SKIP] $name (no PID file)" -ForegroundColor Yellow
        continue
    }

    $servicePid = Get-Content $pidFile -Raw -ErrorAction SilentlyContinue
    if (-not $servicePid) {
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        continue
    }

    $servicePid = $servicePid.Trim()
    try {
        $proc = Get-Process -Id $servicePid -ErrorAction Stop
        # Try graceful shutdown first
        if ($name -eq "tcp_server") {
            # TCP server has no signal handler, just kill
            $proc.Kill()
        } else {
            # For HTTP servers, try Ctrl+C equivalent
            $proc.CloseMainWindow() | Out-Null
            if (-not $proc.HasExited) {
                Start-Sleep -Milliseconds 500
                if (-not $proc.HasExited) { $proc.Kill() }
            }
        }
        Write-Host "  [STOP] $name (PID $servicePid)" -ForegroundColor Green
        $anyStopped = $true
    } catch {
        Write-Host "  [WARN] $name (PID $servicePid not found)" -ForegroundColor Yellow
    }

    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

# Also kill any orphan python processes running our scripts
$scriptNames = @("mumu_route_watchdog.py", "sdk_server.py", "login_server.py", "http_compat_proxy.py", "tcp_server.py", "dns_server.py")
foreach ($script in $scriptNames) {
    try {
        $procs = Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%$script%'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            if ($p.ProcessId -ne $PID -and $p.ProcessId -ne 0) {
                try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
                Write-Host "  [CLEANUP] python $script (PID $($p.ProcessId))" -ForegroundColor Yellow
                $anyStopped = $true
            }
        }
    } catch {}
}

if (-not $anyStopped) {
    Write-Host "  No running services found." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "All services stopped." -ForegroundColor Green
}
