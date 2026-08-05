#Requires -Version 5.1
<#
.SYNOPSIS
    Verify and repair the non-full APK resource root before launching.
.DESCRIPTION
    A non-full APK cannot be launched safely just by tapping its emulator
    icon. Android may preserve only part of the app-specific external files
    after an update, app-data clear, emulator restore, or interrupted push.
    This wrapper validates the host mirror and target device by manifest
    length/MD5, repairs only missing or invalid files, publishes version.json
    last, and launches only after the complete check succeeds.
    It never clears app data.
#>

[CmdletBinding()]
param(
    [string]$Device = "",
    [string]$Package = "com.glkj.lhcx.aligames",
    [string]$ResourceRoot = "",
    [string]$ManifestPath = "",
    [string]$AdbPath = "",
    [string]$PythonPath = "",
    [string]$OfficialDevice = "",
    [string]$OfficialPackage = "com.glkj.lhcx.bilibili"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$serverRoot = Split-Path -Parent $PSScriptRoot

trap {
    Write-Error ("Non-full launch failed: " + $_.Exception.Message)
    exit 1
}

if (-not $AdbPath) {
    $projectAdb = Join-Path $serverRoot "dependencies\android-sdk\platform-tools\adb.exe"
    $mumuAdb = ""
    $AdbPath = if (Test-Path -LiteralPath $projectAdb -PathType Leaf) {
        $projectAdb
    } elseif (Test-Path -LiteralPath $mumuAdb -PathType Leaf) {
        $mumuAdb
    } else {
        throw "No supported adb.exe was found. Pass -AdbPath explicitly."
    }
}
if (-not $PythonPath) {
    $PythonPath = (Get-Command python -ErrorAction Stop).Source
}
$python = $PythonPath

if (-not $ResourceRoot) {
    $ResourceRoot = Join-Path $serverRoot "offline_cdn\Android"
}
if (-not $ManifestPath) {
    # Validate against the installed APK baseline. The active CDN manifest can
    # be newer because it is a hot-update target and must not drive cleanup.
    $apkBaseline = Join-Path $serverRoot "apk_build\manifest-voice-fix12.json"
    $builtManifest = Join-Path $serverRoot "apk_build\version-local-nonfull-built.json"
    $ManifestPath = if (Test-Path -LiteralPath $apkBaseline) {
        $apkBaseline
    } elseif (Test-Path -LiteralPath $builtManifest) {
        $builtManifest
    } else {
        throw "No APK baseline manifest was found. Pass -ManifestPath explicitly; refusing to use the active CDN manifest for preflight."
    }
}

foreach ($path in @($ResourceRoot, $ManifestPath, $AdbPath, $python)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required path does not exist: $path"
    }
}
if (-not (Test-Path -LiteralPath $ResourceRoot -PathType Container)) {
    throw "Resource root is not a directory: $ResourceRoot"
}
$manifestObject = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json

function Get-OnlineDevices {
    $lines = & $AdbPath devices 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to list ADB devices:`n$($lines | Out-String)"
    }
    return @(
        $lines |
            Select-Object -Skip 1 |
            ForEach-Object {
                if ($_ -match '^([^\s]+)\s+device(?:\s|$)') { $Matches[1] }
            }
    )
}

function Resolve-TargetDevice {
    $resolver = Join-Path $PSScriptRoot "resolve_mumu_device.py"
    $resolverArguments = @(
        $resolver,
        "--adb", $AdbPath,
        "--package", $Package,
        "--json"
    )
    if ($Device) { $resolverArguments += @("--serial", $Device) }
    $resolverText = (& $python @resolverArguments 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "Unable to resolve target device: $resolverText" }
    $resolved = $resolverText | ConvertFrom-Json
    Write-Host "ADB aliases: $($resolved.aliases -join ', ')"
    return [string]$resolved.selected
}

& $AdbPath start-server 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to start the ADB server: $AdbPath"
}
$Device = Resolve-TargetDevice

function Invoke-Adb {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    $output = & $AdbPath -s $Device @Arguments 2>&1 | Out-String
    if (-not $AllowFailure -and $LASTEXITCODE -ne 0) {
        throw "ADB failed: adb -s $Device $($Arguments -join ' ')`n$output"
    }
    return $output.Trim()
}

Write-Host "=== Soul Tide non-full launch preflight ===" -ForegroundColor Cyan
Write-Host "Device  : $Device"
Write-Host "Package : $Package"

$state = Invoke-Adb -Arguments @("get-state")
if ($state -notmatch "device") {
    throw "ADB device is not ready: $Device ($state)"
}
$packagePath = Invoke-Adb -Arguments @("shell", "pm", "path", $Package) -AllowFailure
if ($LASTEXITCODE -ne 0 -or $packagePath -notmatch "package:") {
    throw "Package is not installed on ${Device}: $Package"
}

if ($OfficialDevice) {
    Write-Host "Checking host mirror; official emulator is used only for missing or invalid files..." -ForegroundColor Cyan
    $officialSync = Join-Path $PSScriptRoot "sync_from_official_device.py"
    & $python $officialSync `
        --device $OfficialDevice `
        --package $OfficialPackage `
        --adb $AdbPath `
        --resource-root $ResourceRoot `
        --manifest $ManifestPath `
        --scope all-external
    if ($LASTEXITCODE -ne 0) {
        throw "Official emulator resource synchronization failed."
    }
}

Write-Host "Verifying and repairing target resources before launch..." -ForegroundColor Cyan
$syncScript = Join-Path $PSScriptRoot "sync_nonfull_resources.py"
& $python $syncScript `
    --device $Device `
    --package $Package `
    --resource-root $ResourceRoot `
    --manifest $ManifestPath `
    --adb $AdbPath
$syncExitCode = $LASTEXITCODE
if ($syncExitCode -ne 0) {
    throw "Target resource verification failed with exit code $syncExitCode; launch was cancelled."
}

$remoteRoot = "/storage/emulated/0/Android/data/$Package/files/Android"
foreach ($relative in @(
    "16_luaab/luajit/luajit_base.ab.x64",
    "16_luaab/luajit/luajit_base.ab.x86"
)) {
    $luaRow = @($manifestObject.AssetBundleList | Where-Object { $_.RelativePath -eq $relative }) | Select-Object -First 1
    if ($luaRow -and [int]$luaRow.storeRootPathId -eq 1) {
        Invoke-Adb -Arguments @("shell", "rm", "-f", "$remoteRoot/$relative") -AllowFailure | Out-Null
    }
}

Write-Host "Resource preflight passed. Launching..." -ForegroundColor Cyan
Invoke-Adb -Arguments @("shell", "monkey", "-p", $Package, "1") | Write-Host
Write-Host "Waiting for the client resource updater..." -ForegroundColor Cyan
Start-Sleep -Seconds 15
& $python $syncScript `
    --device $Device `
    --package $Package `
    --resource-root $ResourceRoot `
    --manifest $ManifestPath `
    --adb $AdbPath `
    --quick-verify
$postLaunchExitCode = $LASTEXITCODE
if ($postLaunchExitCode -ne 0) {
    Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
    throw "Post-launch resource verification failed with exit code $postLaunchExitCode; the client was stopped."
}
Write-Host "Launch complete and post-launch resources remain valid." -ForegroundColor Green
