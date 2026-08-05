#Requires -Version 5.1
<#
.SYNOPSIS
    Start all Soul Tide local server services.
.DESCRIPTION
    Starts SDK (8000), HTTP login (8081), port-80 compatibility proxy, TCP game (51121), and DNS (53) servers.
    Each service runs in its own background Python process with PID tracking.
.PARAMETER ServerIP
    Local server IP for DNS and emulator routing (default: auto-detect).
.PARAMETER NoWait
    Skip health checks after starting services.
#>

param(
    [string]$ServerIP = "",
    [string]$AdbSerial = "",
    [string]$MumuAdb = "",
    [string]$Package = "com.glkj.lhcx.aligames",
    [switch]$SkipMumuRoutes,
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
# WorkBuddy can expose both Path and PATH in the inherited Windows process
# environment. Windows PowerShell 5.1 Start-Process treats them as duplicate
# case-insensitive dictionary keys. Normalize only this launcher process; do
# not modify user- or machine-level environment variables.
$processPath = [Environment]::GetEnvironmentVariable("Path", "Process")
[Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[Environment]::SetEnvironmentVariable("Path", $processPath, "Process")
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "logs"
$pidDir = $root
$null = New-Item -ItemType Directory -Path $logDir -Force

$configuredPython = [Environment]::GetEnvironmentVariable("SOULTIDE_PYTHON", "Process")
$python = if ($configuredPython -and (Test-Path -LiteralPath $configuredPython -PathType Leaf)) {
    $configuredPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

# ── Detect local IP if not specified ──
if (-not $ServerIP) {
    try {
        $ServerIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
            $_.InterfaceAlias -notmatch 'Loopback|Virtual|Bluetooth|vEthernet' -and
            $_.IPAddress -notmatch '^169\.|^127\.|^0\.'
        } | Select-Object -First 1).IPAddress
    } catch { $ServerIP = "192.168.1.136" }
}
$env:SOULTIDE_SERVER_IP = $ServerIP

if (-not $SkipMumuRoutes) {
    $routeInstaller = Join-Path $root "install_mumu_routes.ps1"
    $routeArguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $routeInstaller,
        "-ServerIP", $ServerIP,
        "-Package", $Package
    )
    if ($AdbSerial) { $routeArguments += @("-AdbSerial", $AdbSerial) }
    if ($MumuAdb) { $routeArguments += @("-MumuAdb", $MumuAdb) }
    $routeArguments += "-Strict"
    Write-Host "Checking MuMu routes before starting services..." -ForegroundColor Cyan
    $routeShell = if ($PSVersionTable.PSEdition -eq "Core") { Join-Path $PSHOME "pwsh.exe" } else { Join-Path $PSHOME "powershell.exe" }
    & $routeShell @routeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "MuMu route recovery failed; local services were not started."
    }
}

Write-Host "=== Soul Tide Local Stack ===" -ForegroundColor Cyan
Write-Host "Server IP: $ServerIP"
Write-Host "Root dir : $root"
Write-Host ""

function Start-ServiceProcess {
    param([string]$Name, [string]$Script, [string]$PortLabel, [int]$Port)
    $pidFile = Join-Path $pidDir "${Name}.pid"
    $logFile = Join-Path $logDir "${Name}.log"
    $errFile = Join-Path $logDir "${Name}.err.log"

    # Check if already running
    if (Test-Path $pidFile) {
        $oldPid = Get-Content $pidFile -Raw -ErrorAction SilentlyContinue
        if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
            Write-Host "  [SKIP] $Name (PID $oldPid already running on $PortLabel)" -ForegroundColor Yellow
            return $oldPid
        }
    }

    # Start process
    $serviceEnv = @{ "PYTHONUNBUFFERED" = "1" }
    # The stack is an offline product; diagnostics must be opt-in outside this launcher.
    $serviceEnv["SOULTIDE_ALLOW_UPSTREAM"] = "0"
    $envVars = @(
        "SOULTIDE_SERVER_IP", "SOULTIDE_TCP_PORT", "SOULTIDE_HTTP_PORT",
        "SOULTIDE_SDK_PORT", "SOULTIDE_DNS_PORT", "SOULTIDE_DB_PATH",
        "SOULTIDE_ASSET_ROOT", "SOULTIDE_RESPONSE_FIXTURE_PATH",
        "SOULTIDE_CDN_UPSTREAM_FALLBACK", "SOULTIDE_CDN_UPSTREAM_BASE",
        "SOULTIDE_UPSTREAM_PROXY", "SOULTIDE_UPDATE_MODE", "SOULTIDE_VERSION_UPSTREAM_URL",
        "SOULTIDE_LOCAL_MANIFEST", "SOULTIDE_WATCHDOG_ADB",
        "SOULTIDE_WATCHDOG_PACKAGE", "SOULTIDE_WATCHDOG_SERIAL",
        "SOULTIDE_ROUTE_INTERVAL", "SOULTIDE_ROUTE_STRICT"
    )
    foreach ($ev in $envVars) {
        $envValue = [Environment]::GetEnvironmentVariable($ev, "Process")
        if ($null -ne $envValue) { $serviceEnv[$ev] = $envValue }
    }
    if ($Name -eq "dns_server") {
        $serviceEnv["SOULTIDE_DNS_PORT"] = [string]$Port
    }

    $previousEnv = @{}
    foreach ($key in $serviceEnv.Keys) {
        $previousEnv[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, [string]$serviceEnv[$key], "Process")
    }
    try {
        # Windows PowerShell 5.1 Start-Process may fail before launch when the
        # host process contains case-variant environment keys (Path/PATH/path).
        # The Process API avoids that environment dictionary normalization;
        # cmd performs only the two file redirections for the Python process.
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $env:ComSpec
        $startInfo.Arguments = "/d /s /c `"`"$python`" -u `"$Script`" 1>`"$logFile`" 2>`"$errFile`"`""
        $startInfo.WorkingDirectory = $root
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($startInfo)
    } finally {
        foreach ($key in $previousEnv.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previousEnv[$key], "Process")
        }
    }

    # Write PID file
    $p.Id | Out-File -FilePath $pidFile -Encoding ascii -Force

    Write-Host "  [START] $Name (PID $($p.Id) on $PortLabel)" -ForegroundColor Green
    return $p.Id
}

# ── Start services in order ──
if (-not $SkipMumuRoutes) {
    $adbPath = if ($MumuAdb) { $MumuAdb } else { Join-Path $root "dependencies\android-sdk\platform-tools\adb.exe" }
    $watchdogScript = Join-Path $root "tools\mumu_route_watchdog.py"
    $oldWatchdogAdb = $env:SOULTIDE_WATCHDOG_ADB
    $oldWatchdogPackage = $env:SOULTIDE_WATCHDOG_PACKAGE
    $oldWatchdogSerial = $env:SOULTIDE_WATCHDOG_SERIAL
    $oldRouteInterval = $env:SOULTIDE_ROUTE_INTERVAL
    $oldRouteStrict = $env:SOULTIDE_ROUTE_STRICT
    $env:SOULTIDE_WATCHDOG_ADB = $adbPath
    $env:SOULTIDE_WATCHDOG_PACKAGE = $Package
    $env:SOULTIDE_WATCHDOG_SERIAL = $AdbSerial
    $env:SOULTIDE_ROUTE_INTERVAL = "5"
    $env:SOULTIDE_ROUTE_STRICT = "1"
    try {
        $routeWatchdogPid = Start-ServiceProcess -Name "mumu_route_watchdog" -Script $watchdogScript -PortLabel "ADB route guard" -Port 0
    } finally {
        $env:SOULTIDE_WATCHDOG_ADB = $oldWatchdogAdb
        $env:SOULTIDE_WATCHDOG_PACKAGE = $oldWatchdogPackage
        $env:SOULTIDE_WATCHDOG_SERIAL = $oldWatchdogSerial
        $env:SOULTIDE_ROUTE_INTERVAL = $oldRouteInterval
        $env:SOULTIDE_ROUTE_STRICT = $oldRouteStrict
    }
}

# SDK server (port 8000)
$sdkPid = Start-ServiceProcess -Name "sdk_server" -Script (Join-Path $root "sdk_server.py") -PortLabel ":8000" -Port 8000

# HTTP login server (port 8081)
$httpPid = Start-ServiceProcess -Name "login_server" -Script (Join-Path $root "login_server.py") -PortLabel ":8081" -Port 8081

# The client requests CDN/login URLs without an explicit port. MuMu's guest
# iptables is not writable on all versions, so keep a host-side port-80 entry.
$compatPid = Start-ServiceProcess -Name "http_compat_proxy" -Script (Join-Path $root "http_compat_proxy.py") -PortLabel ":80" -Port 80

# TCP game server (port 51121)
$tcpPid = Start-ServiceProcess -Name "tcp_server" -Script (Join-Path $root "tcp_server.py") -PortLabel ":51121" -Port 51121

# DNS falls back when a local proxy owns UDP :53. Emulator hosts/DNAT rules
# still route every game endpoint directly to the local stack.
$dnsPort = 53
if (Get-NetUDPEndpoint -LocalPort 53 -ErrorAction SilentlyContinue) {
    $dnsPort = 10053
    Write-Host "  [WARN] UDP :53 is occupied; using DNS fallback :10053" -ForegroundColor Yellow
}
$dnsPort | Out-File -FilePath (Join-Path $root "dns_port.txt") -Encoding ascii -Force
$dnsPid = Start-ServiceProcess -Name "dns_server" -Script (Join-Path $root "dns_server.py") -PortLabel ":$dnsPort" -Port $dnsPort

Write-Host ""

# ── Health check ──
if (-not $NoWait) {
    Write-Host "Waiting for services to be ready..." -ForegroundColor Cyan
    Start-Sleep -Seconds 2

    $checkPorts = @(
        @{Host="127.0.0.1"; Port=8000; Name="SDK server"}
        @{Host="127.0.0.1"; Port=8081; Name="HTTP server"}
        @{Host=$ServerIP; Port=80; Name="HTTP compatibility proxy"}
        @{Host="127.0.0.1"; Port=51121; Name="TCP server"}
    )

    $allOk = $true
    foreach ($cp in $checkPorts) {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect($cp.Host, $cp.Port)
            $tcp.Close()
            Write-Host "  [OK] $($cp.Name) :$($cp.Port)" -ForegroundColor Green
        } catch {
            Write-Host "  [FAIL] $($cp.Name) :$($cp.Port)" -ForegroundColor Red
            $allOk = $false
        }
    }

    if ($allOk) {
        Write-Host ""
        Write-Host "All services are running!" -ForegroundColor Green
        Write-Host "  SDK  : http://localhost:8000"
        Write-Host "  HTTP : http://localhost:8081"
        Write-Host "  HTTP80: http://localhost:80 -> :8081"
        Write-Host "  TCP  : localhost:51121"
        Write-Host "  DNS  : localhost:$dnsPort"
        Write-Host ""
        Write-Host "To stop: .\stop_stack.ps1"
    } else {
        Write-Host ""
        Write-Host "Some services failed to start. Check logs in: $logDir" -ForegroundColor Red
    }
}
