#Requires -Version 5.1
<#
.SYNOPSIS
    Ensure network routes on the localized MuMu instance for Soul Tide offline play.
.DESCRIPTION
    Resolves duplicate ADB aliases by package, Android ID, and boot ID, then checks
    and repairs hosts/DNAT rules only on the localized instance. The official MuMu
    instance is never selected unless it also has the localized package and is
    explicitly selected.
.PARAMETER ServerIP
    Host machine IP for the local server (default: auto-detect).
.PARAMETER MumuAdb
    Path to adb.exe (default: project platform-tools, then MuMu locations).
.PARAMETER AdbSerial
    Optional exact ADB serial. If omitted, the target is resolved safely.
.PARAMETER Package
    Localized package used to identify the correct guest.
.PARAMETER Strict
    Enable strict offline mode (reject all non-local traffic, block DNS).
.PARAMETER Check
    Check only; do not repair missing routes.
.PARAMETER Revert
    Remove Soul Tide routing rules from the selected localized guest.
#>

param(
    [string]$ServerIP = "",
    [string]$MumuAdb = "",
    [string]$AdbSerial = "",
    [string]$Package = "com.glkj.lhcx.aligames",
    [switch]$Strict,
    [switch]$Check,
    [switch]$Revert
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configuredPython = [Environment]::GetEnvironmentVariable("SOULTIDE_PYTHON", "Process")
$python = if ($configuredPython -and (Test-Path -LiteralPath $configuredPython -PathType Leaf)) {
    $configuredPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

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

if (-not $MumuAdb) {
    $commonPaths = @(
        (Join-Path $root "dependencies\android-sdk\platform-tools\adb.exe"),
        "${env:ProgramFiles}\Netease\MuMu\nx_device\12.0\shell\adb.exe",
        "${env:ProgramFiles}\Nemu\vmonitor\bin\adb_server.exe",
        "${env:ProgramFiles(x86)}\Nemu\vmonitor\bin\adb_server.exe",
        "${env:LocalAppData}\Nemu\vmonitor\bin\adb_server.exe"
    )
    $MumuAdb = $commonPaths | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf -ErrorAction SilentlyContinue
    } | Select-Object -First 1
}
if (-not $MumuAdb) { throw "No supported adb.exe was found. Pass -MumuAdb explicitly." }

$routeTool = Join-Path $root "tools\ensure_mumu_routes.py"
$mode = if ($Revert) { "revert" } elseif ($Check) { "check" } else { "ensure" }
$arguments = @(
    $routeTool,
    "--adb", $MumuAdb,
    "--package", $Package,
    "--server-ip", $ServerIP,
    "--mode", $mode,
    "--json"
)
if ($AdbSerial) { $arguments += @("--serial", $AdbSerial) }
if ($Strict) { $arguments += "--strict" }

Write-Host "=== MuMu Route $mode ===" -ForegroundColor Cyan
Write-Host "Server IP : $ServerIP"
Write-Host "ADB path  : $MumuAdb"
Write-Host "Package   : $Package"
if ($AdbSerial) { Write-Host "ADB serial: $AdbSerial" }

$resultText = (& $python @arguments 2>&1) -join "`n"
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    throw "MuMu route $mode failed ($exitCode): $resultText"
}
$result = $resultText | ConvertFrom-Json
if ($mode -eq "revert") {
    Write-Host "  [OK] Routes reverted on $($result.serial)" -ForegroundColor Green
    return
}

Write-Host "  [OK] Device: $($result.serial)" -ForegroundColor Green
Write-Host "  [INFO] Aliases: $($result.aliases -join ', ')"
Write-Host "  [INFO] Android ID: $($result.android_id)"
Write-Host "  [INFO] Boot ID: $($result.boot_id)"
Write-Host "  [OK] Hosts: $($result.hosts_ok); HTTP DNAT: $($result.dnat_http_ok); SDK DNAT: $($result.dnat_sdk_ok); OUTPUT jump: $($result.output_jump_ok)" -ForegroundColor Green
if ($Strict) {
    Write-Host "  [OK] Strict offline rule: $($result.strict_ok)" -ForegroundColor Green
}
