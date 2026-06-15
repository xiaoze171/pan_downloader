$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$BuildPath = Join-Path $ProjectRoot "build"
$DistPath = Join-Path $ProjectRoot "dist"
$SpecPath = Join-Path $ProjectRoot "BaiduPanDownloader.spec"
foreach ($Path in @($BuildPath, $DistPath, $SpecPath)) {
    if ((Test-Path -LiteralPath $Path) -and ((Resolve-Path -LiteralPath $Path).Path.StartsWith($ProjectRoot))) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

$BuildDeps = Join-Path $ProjectRoot ".build_deps"
if (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "PyInstaller"))) {
    & .\bin\py.cmd -m pip install --target $BuildDeps pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller"
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

Write-Output "Built: $(Join-Path $ProjectRoot 'dist\BaiduPanDownloader.exe')"
