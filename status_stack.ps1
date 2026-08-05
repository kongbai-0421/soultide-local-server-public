#Requires -Version 5.1
<#
.SYNOPSIS
    Check the status of all Soul Tide local server services.
.DESCRIPTION
    Checks SDK (8000), HTTP login (8081), port-80 compatibility proxy, TCP game (51121), and DNS (53) servers.
    Verifies ports, PID files, and process existence.
#>

param(
    [string]$ServerIP = "",
    [string]$AdbSerial = "",
    [string]$MumuAdb = "",
    [string]$Package = "com.glkj.lhcx.aligames",
    [switch]$SkipMumuRoutes
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ServerIP) {
    try {
        $ServerIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
            $_.InterfaceAlias -notmatch "Loopback|Virtual|Bluetooth|vEthernet" -and
            $_.IPAddress -notmatch "^169\.|^127\.|^0\."
        } | Select-Object -First 1).IPAddress
    } catch {
        $ServerIP = "192.168.1.136"
    }
}
if (-not $ServerIP) { $ServerIP = "192.168.1.136" }

function Get-ServiceProcessIds {
    param([int]$RootPid)
    $pending = [System.Collections.Generic.Queue[int]]::new()
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $pending.Enqueue($RootPid)
    while ($pending.Count -gt 0) {
        $currentPid = $pending.Dequeue()
        if (-not $seen.Add($currentPid)) { continue }
        foreach ($child in Get-CimInstance Win32_Process -Filter "ParentProcessId=$currentPid" -ErrorAction SilentlyContinue) {
            $pending.Enqueue([int]$child.ProcessId)
        }
    }
    return @($seen)
}

$dnsPort = 53
$dnsPortFile = Join-Path $root "dns_port.txt"
if (Test-Path $dnsPortFile) {
    $parsedDnsPort = 0
    if ([int]::TryParse((Get-Content $dnsPortFile -Raw).Trim(), [ref]$parsedDnsPort) -and $parsedDnsPort -gt 0) {
        $dnsPort = $parsedDnsPort
    }
}

$services = @(
    @{Name="mumu_route_watchdog"; Port=0; File="mumu_route_watchdog.py"; NoPort=$true}
    @{Name="sdk_server";    Port=8000;  File="sdk_server.py"}
    @{Name="login_server";  Port=8081;  File="login_server.py"}
    @{Name="http_compat_proxy"; Port=80; File="http_compat_proxy.py"}
    @{Name="tcp_server";   Port=51121; File="tcp_server.py"}
    @{Name="dns_server";   Port=$dnsPort; File="dns_server.py"; Udp=$true}
)

Write-Host "=== Soul Tide Stack Status ===" -ForegroundColor Cyan
Write-Host ""

$allOk = $true
foreach ($svc in $services) {
    $pidFile = Join-Path $root "$($svc.Name).pid"
    $running = $false
    $servicePid = $null
    $portListening = $false

    # Check PID file
    if (Test-Path $pidFile) {
        $servicePid = (Get-Content $pidFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($servicePid -and (Get-Process -Id $servicePid -ErrorAction SilentlyContinue)) {
            $running = $true
        }
    }

    # PID files track the cmd launcher used for output redirection. Accept the
    # launcher and all descendants so port ownership resolves to python.exe.
    $serviceProcessIds = if ($running) {
        Get-ServiceProcessIds -RootPid ([int]$servicePid)
    } else {
        @()
    }

    # The route watchdog has no listening port; DNS is UDP; other services are TCP.
    if ($svc.NoPort) {
        $portListening = $running
    } elseif ($svc.Udp) {
        $portListening = [bool](Get-NetUDPEndpoint -LocalPort $svc.Port -ErrorAction SilentlyContinue | Where-Object OwningProcess -In $serviceProcessIds)
    } elseif ($svc.Port -eq 80) {
        # The compatibility proxy binds the detected LAN address, not loopback,
        # because the emulator reaches the host over its LAN interface.
        $portListening = [bool](Get-NetTCPConnection -State Listen -LocalPort $svc.Port -ErrorAction SilentlyContinue | Where-Object OwningProcess -In $serviceProcessIds)
    } else {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect("127.0.0.1", $svc.Port)
            $tcp.Close()
            $portListening = $true
        } catch {
            $portListening = $false
        }
    }

    # Determine status icons
    if ($running -and $portListening) {
        $endpoint = if ($svc.NoPort) { "route guard" } else { ":$($svc.Port)" }
        Write-Host "  [RUNNING] $($svc.Name) (PID $servicePid, $endpoint)" -ForegroundColor Green
    } elseif ($running -and -not $portListening) {
        Write-Host "  [WARN] $($svc.Name) (PID $servicePid, port :$($svc.Port) not listening)" -ForegroundColor Yellow
        $allOk = $false
    } elseif ($portListening -and -not $running) {
        Write-Host "  [WARN] $($svc.Name) (port :$($svc.Port) in use by unknown process)" -ForegroundColor Yellow
        $allOk = $false
    } else {
        Write-Host "  [STOPPED] $($svc.Name) (:$( $svc.Port))" -ForegroundColor Red
        $allOk = $false
    }
}

Write-Host ""

if (-not $SkipMumuRoutes) {
    try {
        $routeInstaller = Join-Path $root "install_mumu_routes.ps1"
        $routeArguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $routeInstaller,
            "-ServerIP", $ServerIP,
            "-Package", $Package,
            "-Check"
        )
        if ($AdbSerial) { $routeArguments += @("-AdbSerial", $AdbSerial) }
        if ($MumuAdb) { $routeArguments += @("-MumuAdb", $MumuAdb) }
        $routeShell = if ($PSVersionTable.PSEdition -eq "Core") { Join-Path $PSHOME "pwsh.exe" } else { Join-Path $PSHOME "powershell.exe" }
        & $routeShell @routeArguments
        if ($LASTEXITCODE -ne 0) { throw "route check exit code $LASTEXITCODE" }
    } catch {
        Write-Host "  [WARN] MuMu routes are missing or invalid: $($_.Exception.Message)" -ForegroundColor Yellow
        $allOk = $false
    }
    Write-Host ""
}

# Database summary
$dbPath = Join-Path $root "soultide.db"
if (Test-Path $dbPath) {
    try {
        $python = (Get-Command python).Source
        $result = & $python -c "
import sys, json
sys.path.insert(0, '$root')
import storage
print(json.dumps(storage.database_summary()))
" 2>$null
        if ($result) {
            $summary = $result | ConvertFrom-Json
            Write-Host "Database: $dbPath" -ForegroundColor Cyan
            Write-Host "  Accounts: $($summary.accounts) | Players: $($summary.players) | Items: $($summary.items) | Souls: $($summary.souls)"
        }
    } catch {
        Write-Host "Database: $dbPath (read error: $($_.Exception.Message))" -ForegroundColor Yellow
    }
} else {
    Write-Host "Database: not found" -ForegroundColor Yellow
}

Write-Host ""
if ($allOk) {
    Write-Host "All services OK." -ForegroundColor Green
} else {
    Write-Host "Some services are not running. Use .\start_stack.ps1 to start." -ForegroundColor Yellow
}
