# Synchronize the complete local mirror for a non-full APK.
# The final manifest is pushed last so an official raw manifest cannot restore
# APK paths for resources that are actually stored on external storage.

[CmdletBinding()]
param(
    [string]$Device = "",
    [string]$Package = "com.glkj.lhcx.aligames",
    [string]$ResourceRoot = "",
    [string]$ManifestPath = "",
    [string]$AdbPath = "",
    [string]$PythonPath = "",
    [switch]$QuickVerify
)

$ErrorActionPreference = "Stop"
$serverRoot = Split-Path -Parent $PSScriptRoot
if (-not $ResourceRoot) {
    $ResourceRoot = Join-Path $serverRoot "offline_cdn\Android"
}
if (-not $ManifestPath) {
    $builtManifest = Join-Path $serverRoot "apk_build\version-local-nonfull-built.json"
    $ManifestPath = if (Test-Path -LiteralPath $builtManifest -PathType Leaf) {
        $builtManifest
    } else {
        Join-Path $ResourceRoot "version-local-nonfull.json"
    }
}

if (-not (Test-Path -LiteralPath $ResourceRoot -PathType Container)) {
    throw "Resource root does not exist: $ResourceRoot"
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Mixed-root manifest does not exist: $ManifestPath"
}
if (-not (Test-Path -LiteralPath $AdbPath -PathType Leaf)) {
    throw "ADB executable does not exist: $AdbPath"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable does not exist: $PythonPath"
}

$resolver = Join-Path $PSScriptRoot "resolve_mumu_device.py"
$resolverArguments = @(
    $resolver,
    "--adb", $AdbPath,
    "--package", $Package,
    "--json"
)
if ($Device) { $resolverArguments += @("--serial", $Device) }
$resolverText = (& $PythonPath @resolverArguments 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) { throw "Unable to resolve target device: $resolverText" }
$resolved = $resolverText | ConvertFrom-Json
$Device = [string]$resolved.selected
Write-Host "ADB aliases: $($resolved.aliases -join ', ')"

$syncTool = Join-Path $PSScriptRoot "sync_nonfull_resources.py"
$arguments = @(
    $syncTool,
    "--device", $Device,
    "--package", $Package,
    "--adb", $AdbPath,
    "--resource-root", $ResourceRoot,
    "--manifest", $ManifestPath
)
if ($QuickVerify) { $arguments += "--quick-verify" }
& $PythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw "Non-full resource verification or sync failed" }
