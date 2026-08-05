#Requires -Version 5.1
<#
.SYNOPSIS
    Install a non-full APK and synchronize its matching external resource root.
.DESCRIPTION
    The APK intentionally contains only bootstrap-critical files. After an
    overwrite install, the game is launched once before synchronization so its
    StreamingMd5 handshake can finish and discard stale resources. The game is
    then stopped, every external bundle/media file is synchronized, and an
    optional second launch verifies that all manifest-declared external
    resources remain present. Existing app data is preserved by default.
.PARAMETER Device
    Exact ADB serial for the localized MuMu instance.
.PARAMETER ApkPath
    Signed non-full APK. Defaults to apk_output\soultide-local.apk.
.PARAMETER ClearAppData
    Clear the app data before installation. This removes the local account and
    is deliberately opt-in.
.PARAMETER Launch
    Leave the game running after the second-launch resource verification. By
    default the verified game is stopped. For later starts without reinstalling,
    use tools\launch_nonfull.ps1.
.PARAMETER DeviceLocalSnapshotRoot
    Optional partial device-local snapshot. Existing app resources are copied
    here before installation, then only manifest-valid files are merged back;
    host synchronization repairs the remaining files.
#>

[CmdletBinding()]
param(
    [string]$Device = "",
    [string]$Package = "com.glkj.lhcx.aligames",
    [string]$ApkPath = "",
    [string]$ResourceRoot = "",
    [string]$ManifestPath = "",
    [string]$DeviceMirrorRoot = "",
    [string]$DeviceLocalSnapshotRoot = "",
    [string]$AdbPath = "",
    [string]$PythonPath = "",
    [string]$OfficialDevice = "",
    [string]$OfficialPackage = "com.glkj.lhcx.bilibili",
    [int]$ExpectedResourceVersion = 0,
    [int]$HandshakeTimeoutSeconds = 90,
    [int]$LaunchVerificationDelaySeconds = 45,
    [int]$MirrorTimeoutSeconds = 300,
    [switch]$ClearAppData,
    [switch]$Launch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$serverRoot = Split-Path -Parent $PSScriptRoot

if ($DeviceMirrorRoot -and $DeviceLocalSnapshotRoot) {
    throw "Specify only one of -DeviceMirrorRoot or -DeviceLocalSnapshotRoot."
}

if (-not $ApkPath) { $ApkPath = Join-Path $serverRoot "apk_output\soultide-local.apk" }
if (-not $ResourceRoot) { $ResourceRoot = Join-Path $serverRoot "offline_cdn\Android" }
if (-not $ManifestPath) {
    # The active CDN manifest may be a newer hot-update target than the
    # installed APK. Default to the manifest embedded by the current fix12
    # package so the first-launch StreamingMd5 handshake uses the APK base.
    $apkBaseline = Join-Path $serverRoot "apk_build\manifest-voice-fix12.json"
    $builtManifest = Join-Path $serverRoot "apk_build\version-local-nonfull-built.json"
    $ManifestPath = if (Test-Path -LiteralPath $apkBaseline) {
        $apkBaseline
    } elseif (Test-Path -LiteralPath $builtManifest) {
        $builtManifest
    } else {
        throw "No APK baseline manifest was found. Pass -ManifestPath explicitly; refusing to use the active CDN manifest before the first-launch handshake."
    }
}

foreach ($path in @($ApkPath, $ResourceRoot, $ManifestPath, $AdbPath, $PythonPath)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path does not exist: $path" }
}
if (-not (Test-Path -LiteralPath $ResourceRoot -PathType Container)) {
    throw "Resource root is not a directory: $ResourceRoot"
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
    $resolverText = (& $PythonPath @resolverArguments 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "Unable to resolve target device: $resolverText" }
    $resolved = $resolverText | ConvertFrom-Json
    Write-Host "ADB aliases: $($resolved.aliases -join ', ')"
    return [string]$resolved.selected
}

$Device = Resolve-TargetDevice

function Invoke-Adb {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    # PowerShell 5 promotes adb's progress stderr (notably for large APK
    # pushes) to a NativeCommandError when Stop is active, even with exit 0.
    # Capture both streams while keeping the real adb exit code authoritative.
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $AdbPath -s $Device @Arguments 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "ADB failed: adb -s $Device $($Arguments -join ' ')`n$output"
    }
    return $output.Trim()
}

Write-Host "=== Soul Tide non-full installer ===" -ForegroundColor Cyan
Write-Host "Device  : $Device"
Write-Host "APK     : $ApkPath"
Write-Host "Resources: $ResourceRoot"

Invoke-Adb -Arguments @("start-server") | Out-Null
$state = Invoke-Adb -Arguments @("get-state")
if ($state -notmatch "device") { throw "ADB device is not ready: $state" }

# Refuse a stale/official APK whose embedded manifest does not match the
# selected external manifest. Mixing resource generations creates the exact
# jar:file/media failures this workflow is designed to prevent.
$manifestObject = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$manifestResourceVersion = [int]$manifestObject.InternalResourceVersion
if ($manifestResourceVersion -le 0) {
    throw "Manifest has no valid InternalResourceVersion: $ManifestPath"
}
if ($ExpectedResourceVersion -ne 0 -and $ExpectedResourceVersion -ne $manifestResourceVersion) {
    throw "Expected resource version $ExpectedResourceVersion does not match manifest version $manifestResourceVersion."
}
$ExpectedResourceVersion = $manifestResourceVersion

$apkInspector = Join-Path $PSScriptRoot "inspect_nonfull_apk.py"
$apkInfo = & $PythonPath $apkInspector $ApkPath
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect APK manifest: $ApkPath" }
$apkInfoObject = $apkInfo | ConvertFrom-Json
if ([int]$apkInfoObject.resourceVersion -ne $ExpectedResourceVersion) {
    throw "APK resource version $($apkInfoObject.resourceVersion) does not match manifest version $ExpectedResourceVersion. Build the non-full APK first."
}
$selectedManifestMd5 = (Get-FileHash -LiteralPath $ManifestPath -Algorithm MD5).Hash.ToUpperInvariant()
$embeddedManifestMd5 = [string]$apkInfoObject.manifestMd5
if (-not $embeddedManifestMd5) {
    throw "APK inspector did not return the embedded version.json MD5; refusing to synchronize resources."
}
if ($embeddedManifestMd5.ToUpperInvariant() -ne $selectedManifestMd5) {
    throw "Selected manifest MD5 $selectedManifestMd5 does not match the APK embedded manifest MD5 $embeddedManifestMd5. Do not use the active hot-update manifest as the APK baseline."
}

$externalRows = @($manifestObject.AssetBundleList | Where-Object { [int]$_.storeRootPathId -eq 2 })
$mediaRows = @($externalRows | Where-Object { $_.RelativePath -match '(?i)\.(mp4|webm|mov)$' })
if ($externalRows.Count -eq 0) { throw "The external manifest contains no storeRootPathId=2 entries." }
Write-Host ("Manifest : version {0}, {1} entries, {2} external, {3} media" -f $ExpectedResourceVersion, $manifestObject.AssetBundleList.Count, $externalRows.Count, $mediaRows.Count)

if ($OfficialDevice) {
    Write-Host "Checking local resources; official emulator $OfficialDevice will be used only for missing or invalid files..." -ForegroundColor Cyan
    $officialSync = Join-Path $PSScriptRoot "sync_from_official_device.py"
    & $PythonPath $officialSync `
        --device $OfficialDevice `
        --package $OfficialPackage `
        --adb $AdbPath `
        --resource-root $ResourceRoot `
        --manifest $ManifestPath `
        --scope all-external
    if ($LASTEXITCODE -ne 0) { throw "Official emulator resource synchronization failed." }
}

Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
if ($ClearAppData) {
    Write-Host "Clearing app data by explicit request..." -ForegroundColor Yellow
    Invoke-Adb -Arguments @("shell", "pm", "clear", $Package) -AllowFailure | Out-Null
}

if ($DeviceLocalSnapshotRoot) {
    Write-Host "Snapshotting existing emulator resources before APK install..." -ForegroundColor Cyan
    $mirrorScript = Join-Path $PSScriptRoot "mirror_nonfull_resources.py"
    & $PythonPath $mirrorScript snapshot `
        --device $Device `
        --package $Package `
        --adb $AdbPath `
        --manifest $ManifestPath `
        --mirror-root $DeviceLocalSnapshotRoot `
        --replace `
        --timeout-seconds $MirrorTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "Device-local resource snapshot failed." }
}

Write-Host "Installing APK while preserving account data..." -ForegroundColor Cyan
# MuMu's adb install --no-streaming path handling can turn a large APK source
# into /data/local/tmp/./. when the Windows path contains non-ASCII segments.
# Push to an explicit ASCII staging name, then ask package manager to install it.
$remoteApk = "/data/local/tmp/soultide-local.apk"
Invoke-Adb -Arguments @("shell", "rm", "-f", $remoteApk) -AllowFailure | Out-Null
try {
    Invoke-Adb -Arguments @("push", $ApkPath, $remoteApk) | Write-Host
    Invoke-Adb -Arguments @("shell", "pm", "install", "-r", "-d", "-g", $remoteApk) | Write-Host
} finally {
    Invoke-Adb -Arguments @("shell", "rm", "-f", $remoteApk) -AllowFailure | Out-Null
}

# An overwrite install can change the embedded manifest MD5 even when only the
# patched LuaJIT bundles changed. The first launch lets ResourceChecker commit
# the new StreamingMd5 before we publish the complete external mirror. Pushing
# resources before this handshake is unsafe: the first launch can delete all
# 629 ordinary Tags=[0] external resources as stale files.
$expectedStreamingMd5 = (Get-FileHash -LiteralPath $ManifestPath -Algorithm MD5).Hash.ToUpperInvariant()
$playerPrefs = "/data/user/0/$Package/shared_prefs/$Package.v2.playerprefs.xml"
Write-Host "Running first-launch manifest handshake ($expectedStreamingMd5)..." -ForegroundColor Cyan
Invoke-Adb -Arguments @("shell", "monkey", "-p", $Package, "-c", "android.intent.category.LAUNCHER", "1") | Write-Host
$handshakeDeadline = [DateTime]::UtcNow.AddSeconds($HandshakeTimeoutSeconds)
$handshakeComplete = $false
$remoteRoot = "/storage/emulated/0/Android/data/$Package/files/Android"
while ([DateTime]::UtcNow -lt $handshakeDeadline) {
    Start-Sleep -Seconds 3
    $prefsCommand = "su -c 'cat $playerPrefs'"
    $prefs = Invoke-Adb -Arguments @("shell", $prefsCommand) -AllowFailure
    if ($prefs -match "(?i)<string name=`"StreamingMd5`">$expectedStreamingMd5</string>") {
        $handshakeComplete = $true
        break
    }
}
Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
if (-not $handshakeComplete) {
    # Config-only APK rebuilds can keep the same InternalResourceVersion. In
    # that case ResourceChecker may not rewrite PlayerPrefs even though the
    # embedded LuaJIT bundle and selected manifest changed. With an explicit
    # device mirror, adopt the selected manifest after the guarded first launch
    # and restore the external root before the next launch.
    if ($DeviceMirrorRoot) {
        $mirrorExists = Invoke-Adb -Arguments @(
            "shell",
            "su -c 'if [ -d $DeviceMirrorRoot ]; then echo yes; else echo no; fi'"
        ) -AllowFailure
        if ($mirrorExists.Trim() -eq "yes") {
            $remoteManifest = "/data/local/tmp/soultide-selected-version.json"
            Invoke-Adb -Arguments @("push", $ManifestPath, $remoteManifest) | Write-Host
            $adoptManifestCommand = "su -c 'cat $remoteManifest > $remoteRoot/version.json.tmp && mv $remoteRoot/version.json.tmp $remoteRoot/version.json && cat $remoteManifest > $remoteRoot/version-remote.json.tmp && mv $remoteRoot/version-remote.json.tmp $remoteRoot/version-remote.json && cat $remoteManifest > $remoteRoot/version.json.bak.tmp && mv $remoteRoot/version.json.bak.tmp $remoteRoot/version.json.bak && rm -f $remoteManifest'"
            Invoke-Adb -Arguments @("shell", $adoptManifestCommand) | Out-Null
            $adoptPrefsCommand = 'su -c "sed -i ''/StreamingMd5/s/>[A-Fa-f0-9]*</>EXPECTED_MD5</'' /data/user/0/PACKAGE/shared_prefs/PACKAGE.v2.playerprefs.xml"'
            $adoptPrefsCommand = $adoptPrefsCommand.Replace("EXPECTED_MD5", $expectedStreamingMd5).Replace("PACKAGE", $Package)
            Invoke-Adb -Arguments @("shell", $adoptPrefsCommand) | Out-Null
            $handshakeComplete = $true
            Write-Warning "StreamingMd5 was not rewritten by the same-version first launch; adopted the selected manifest from the verified device mirror."
        }
    }
}
if (-not $handshakeComplete) {
    throw "StreamingMd5 did not reach $expectedStreamingMd5 within $HandshakeTimeoutSeconds seconds. External resources were not published."
}
Write-Host "StreamingMd5 handshake complete; synchronizing external bundles and media..." -ForegroundColor Cyan

$syncScript = Join-Path $PSScriptRoot "sync_nonfull_resources.ps1"
if ($DeviceMirrorRoot) {
    $mirrorScript = Join-Path $PSScriptRoot "mirror_nonfull_resources.py"
    & $PythonPath $mirrorScript restore `
        --device $Device `
        --package $Package `
        --adb $AdbPath `
        --manifest $ManifestPath `
        --mirror-root $DeviceMirrorRoot `
        --timeout-seconds $MirrorTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "Device-local resource mirror restore failed." }

    # The mirror contains the resource snapshot's old manifests. Restore the
    # selected APK manifest only after copying resources from the device.
    $remoteManifest = "/data/local/tmp/soultide-selected-version.json"
    Invoke-Adb -Arguments @("push", $ManifestPath, $remoteManifest) | Write-Host
    $manifestCommand = "su -c 'cat $remoteManifest > $remoteRoot/version.json.tmp && mv $remoteRoot/version.json.tmp $remoteRoot/version.json && cat $remoteManifest > $remoteRoot/version-remote.json.tmp && mv $remoteRoot/version-remote.json.tmp $remoteRoot/version-remote.json && cat $remoteManifest > $remoteRoot/version.json.bak.tmp && mv $remoteRoot/version.json.bak.tmp $remoteRoot/version.json.bak && rm -f $remoteManifest'"
    Invoke-Adb -Arguments @("shell", $manifestCommand) | Out-Null
} elseif ($DeviceLocalSnapshotRoot) {
    $mirrorScript = Join-Path $PSScriptRoot "mirror_nonfull_resources.py"
    & $PythonPath $mirrorScript merge `
        --device $Device `
        --package $Package `
        --adb $AdbPath `
        --manifest $ManifestPath `
        --mirror-root $DeviceLocalSnapshotRoot `
        --timeout-seconds $MirrorTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "Device-local resource merge failed." }
    Write-Host "Merging valid device-local resources; host will repair only missing or mismatched files..." -ForegroundColor Cyan
    & $syncScript -Device $Device -Package $Package -ResourceRoot $ResourceRoot -ManifestPath $ManifestPath -AdbPath $AdbPath -PythonPath $PythonPath
    if ($LASTEXITCODE -ne 0) { throw "External resource synchronization failed after device-local merge." }
} else {
    & $syncScript -Device $Device -Package $Package -ResourceRoot $ResourceRoot -ManifestPath $ManifestPath -AdbPath $AdbPath -PythonPath $PythonPath
    if ($LASTEXITCODE -ne 0) { throw "External resource synchronization failed." }
}

foreach ($relative in @(
    "16_luaab/luajit/luajit_base.ab.x64",
    "16_luaab/luajit/luajit_base.ab.x86"
)) {
    $luaRow = @($manifestObject.AssetBundleList | Where-Object { $_.RelativePath -eq $relative }) | Select-Object -First 1
    if ($luaRow -and [int]$luaRow.storeRootPathId -eq 1) {
        Invoke-Adb -Arguments @("shell", "rm", "-f", "$remoteRoot/$relative") -AllowFailure | Out-Null
    }
}
$aurora = "$remoteRoot/21_Media/CG/Whisper/Aurora/Whisper01/Aurora_02.mp4"
$auroraCheck = Invoke-Adb -Arguments @("shell", "test", "-s", $aurora) -AllowFailure
if ($LASTEXITCODE -ne 0) { throw "Required Whisper media is not present after synchronization: $aurora" }
$versionCheck = Invoke-Adb -Arguments @("shell", "grep", "-q", "storeRootPathId.*2", "$remoteRoot/version.json") -AllowFailure
if ($LASTEXITCODE -ne 0) { throw "External-root marker is missing from the installed version.json." }

# A second launch is mandatory even when -Launch was not requested. It proves
# that ResourceChecker no longer removes the synchronized mirror. Only after a
# full manifest-sized quick verification may the install be considered successful.
Write-Host "Running second-launch resource stability verification..." -ForegroundColor Cyan
Invoke-Adb -Arguments @("logcat", "-c") -AllowFailure | Out-Null
Invoke-Adb -Arguments @("shell", "monkey", "-p", $Package, "-c", "android.intent.category.LAUNCHER", "1") | Write-Host
Start-Sleep -Seconds $LaunchVerificationDelaySeconds
$gamePid = Invoke-Adb -Arguments @("shell", "pidof", $Package) -AllowFailure
if (-not $gamePid) {
    Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
    throw "The game process is not running after the second launch."
}
& $syncScript -Device $Device -Package $Package -ResourceRoot $ResourceRoot -ManifestPath $ManifestPath -AdbPath $AdbPath -PythonPath $PythonPath -QuickVerify
if ($LASTEXITCODE -ne 0) {
    Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
    throw "External resources changed after the second launch."
}
$secondLaunchLog = Invoke-Adb -Arguments @("logcat", "-d", "-v", "threadtime") -AllowFailure
$criticalPattern = "ResourceS\.AssetLoadAgent\.Update|ResourceS\.ResourceLoader\.Update|ResourceS\.ResourcePoolLoader\.Update|NullReferenceException|FATAL EXCEPTION|SIGSEGV|UnsatisfiedLinkError"
if ($secondLaunchLog -match $criticalPattern) {
    Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
    $criticalLines = @($secondLaunchLog -split "`r?`n" | Where-Object { $_ -match $criticalPattern } | Select-Object -Last 30)
    throw "Critical resource or crash log detected after the second launch:`n$($criticalLines -join "`n")"
}

if (-not $Launch) {
    Invoke-Adb -Arguments @("shell", "am", "force-stop", $Package) -AllowFailure | Out-Null
}
Write-Host "Install complete: resourceVersion=$ExpectedResourceVersion, StreamingMd5=$expectedStreamingMd5, second-launch resources=$($externalRows.Count)/$($externalRows.Count), media=$($mediaRows.Count)/$($mediaRows.Count), account data preserved=$(-not $ClearAppData), running=$([bool]$Launch)." -ForegroundColor Green
