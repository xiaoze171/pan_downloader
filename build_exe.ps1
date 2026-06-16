$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$BuildPath = Join-Path $ProjectRoot "build"
$DistPath = Join-Path $ProjectRoot "dist"
$SpecPath = Join-Path $ProjectRoot "BaiduPanDownloader.spec"
$DistCredentialsPath = Join-Path $DistPath "credentials.local.json"
$RootCredentialsPath = Join-Path $ProjectRoot "credentials.local.json"
$SavedCredentialsJson = $null
foreach ($CredentialsPath in @($DistCredentialsPath, $RootCredentialsPath)) {
    if (Test-Path -LiteralPath $CredentialsPath) {
        $SavedCredentialsJson = [System.IO.File]::ReadAllText($CredentialsPath).TrimStart([char]0xFEFF)
        break
    }
}

foreach ($Path in @($BuildPath, $SpecPath)) {
    if ((Test-Path -LiteralPath $Path) -and ((Resolve-Path -LiteralPath $Path).Path.StartsWith($ProjectRoot))) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}
if (Test-Path -LiteralPath $DistPath) {
    if ((Resolve-Path -LiteralPath $DistPath).Path.StartsWith($ProjectRoot)) {
        Get-ChildItem -LiteralPath $DistPath -Force | Remove-Item -Recurse -Force
    }
}

$BuildDeps = Join-Path $ProjectRoot ".build_deps"
if (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "PyInstaller"))) {
    & .\bin\py.cmd -m pip install --target $BuildDeps pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller"
    }
}
if (
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "flet"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "flet_desktop")))
) {
    & .\bin\py.cmd -m pip install --target $BuildDeps -r .\requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install build dependencies"
    }
}
if (Test-Path -LiteralPath $BuildDeps) {
    $env:PYTHONPATH = "$BuildDeps;$env:PYTHONPATH"
}
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUSERBASE = Join-Path $ProjectRoot "build\pythonuserbase"
$env:MPLCONFIGDIR = Join-Path $ProjectRoot "build\mplconfig"

& .\bin\py.cmd -s -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name BaiduPanDownloader `
    --collect-all flet `
    --collect-all flet_desktop `
    --hidden-import flet_desktop `
    --exclude-module IPython `
    --exclude-module PIL `
    --exclude-module astroid `
    --exclude-module black `
    --exclude-module dask `
    --exclude-module docutils `
    --exclude-module jedi `
    --exclude-module jinja2 `
    --exclude-module jsonschema `
    --exclude-module matplotlib `
    --exclude-module nbformat `
    --exclude-module numpy `
    --exclude-module pandas `
    --exclude-module PyQt5 `
    --exclude-module PyQt6 `
    --exclude-module PySide2 `
    --exclude-module PySide6 `
    --exclude-module pygments `
    --exclude-module pytest `
    --exclude-module scipy `
    --exclude-module sphinx `
    --exclude-module streamlit `
    --exclude-module tornado `
    .\baidu_pan_downloader.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if ([string]::IsNullOrWhiteSpace($SavedCredentialsJson)) {
    $EmptyCredentials = [ordered]@{
        BDUSS = ""
        STOKEN = ""
    }
    $SavedCredentialsJson = $EmptyCredentials | ConvertTo-Json
}
$CredentialsJson = $SavedCredentialsJson.TrimStart([char]0xFEFF).TrimEnd() + [Environment]::NewLine
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($DistCredentialsPath, $CredentialsJson, $Utf8NoBom)

Write-Output "Built: $(Join-Path $ProjectRoot 'dist\BaiduPanDownloader.exe')"
Write-Output "Config: $DistCredentialsPath"
