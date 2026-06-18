$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$BuildPath = Join-Path $ProjectRoot ".build_deps\AliyunDriveDownloader"
$SpecPath = Join-Path $BuildPath "spec"
$DistPath = Join-Path $ProjectRoot "dist\aliyun_drive"
$DistExePath = Join-Path $DistPath "AliyunDriveDownloader.exe"
$DistCredentialsPath = Join-Path $DistPath "credentials.local.json"
$SourceCredentialsPath = Join-Path $ProjectRoot "aliyun_drive\credentials.local.json"
$ExampleCredentialsPath = Join-Path $ProjectRoot "aliyun_drive\credentials.local.example.json"

$SavedCredentialsJson = $null
foreach ($CredentialsPath in @($DistCredentialsPath, $SourceCredentialsPath, $ExampleCredentialsPath)) {
    if (Test-Path -LiteralPath $CredentialsPath) {
        $SavedCredentialsJson = [System.IO.File]::ReadAllText($CredentialsPath).TrimStart([char]0xFEFF)
        break
    }
}

if ((Test-Path -LiteralPath $BuildPath) -and ((Resolve-Path -LiteralPath $BuildPath).Path.StartsWith($ProjectRoot))) {
    Remove-Item -LiteralPath $BuildPath -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildPath | Out-Null
New-Item -ItemType Directory -Force -Path $SpecPath | Out-Null
New-Item -ItemType Directory -Force -Path $DistPath | Out-Null

$BuildDeps = Join-Path $ProjectRoot ".build_deps"
if (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "PyInstaller"))) {
    & .\bin\py.cmd -m pip install --target $BuildDeps pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller"
    }
}
if (
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "requests"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "tqdm")))
) {
    & .\bin\py.cmd -m pip install --target $BuildDeps requests tqdm
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Aliyun build dependencies"
    }
}

$env:PYTHONPATH = "$BuildDeps;$ProjectRoot;$env:PYTHONPATH"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUSERBASE = Join-Path $BuildPath "pythonuserbase"
$env:MPLCONFIGDIR = Join-Path $BuildPath "mplconfig"

& .\bin\py.cmd -s -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name AliyunDriveDownloader `
    --paths "$ProjectRoot" `
    --distpath "$DistPath" `
    --workpath "$BuildPath\work" `
    --specpath "$SpecPath" `
    --exclude-module IPython `
    --exclude-module PIL `
    --exclude-module astroid `
    --exclude-module black `
    --exclude-module dask `
    --exclude-module docutils `
    --exclude-module flet `
    --exclude-module flet_desktop `
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
    .\aliyun_drive\aliyun_drive_direct_downloader.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if ([string]::IsNullOrWhiteSpace($SavedCredentialsJson)) {
    $EmptyCredentials = [ordered]@{
        ALIYUN_ACCESS_TOKEN = ""
        ALIYUN_REFRESH_TOKEN = ""
        ALIYUN_DEFAULT_DRIVE_ID = ""
    }
    $SavedCredentialsJson = $EmptyCredentials | ConvertTo-Json
}
$CredentialsJson = $SavedCredentialsJson.TrimStart([char]0xFEFF).TrimEnd() + [Environment]::NewLine
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($DistCredentialsPath, $CredentialsJson, $Utf8NoBom)

Write-Output "Built: $DistExePath"
Write-Output "Config: $DistCredentialsPath"
