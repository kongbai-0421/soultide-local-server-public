#Requires -Version 5.1
<#
.SYNOPSIS
    Build the configurable local server launcher as a single EXE.
.DESCRIPTION
    All paths are resolved from this script's repository location or from the
    supplied Python executable. The resulting EXE stores user settings under
    the current Windows user's application-data directory at runtime.
#>

param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $toolRoot
$python = if ($PythonPath) {
    (Resolve-Path -LiteralPath $PythonPath).Path
} else {
    (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python executable does not exist: $python"
}

$source = Join-Path $toolRoot 'local_stack_launcher.py'
$outputRoot = Join-Path $projectRoot 'launcher_output'
$buildRoot = Join-Path $projectRoot '.tmp-launcher-build'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Launcher source does not exist: $source"
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name SoulTideLocalServerLauncher `
    --distpath $outputRoot `
    --workpath $buildRoot `
    --specpath $buildRoot `
    $source
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $outputRoot 'SoulTideLocalServerLauncher.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Expected launcher EXE was not created: $exe"
}
Write-Host "Launcher built: $exe" -ForegroundColor Green
