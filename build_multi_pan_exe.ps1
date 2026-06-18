$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Assert-UnderProjectRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $FullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $FullPath.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside project root: $FullPath"
    }
    return $FullPath
}

function Read-SavedCredentials {
    param(
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [Parameter(Mandatory = $true)][string[]]$Keys
    )
    foreach ($CredentialsPath in $Paths) {
        if (Test-Path -LiteralPath $CredentialsPath) {
            return [System.IO.File]::ReadAllText($CredentialsPath).TrimStart([char]0xFEFF)
        }
    }
    $EmptyCredentials = [ordered]@{}
    foreach ($Key in $Keys) {
        $EmptyCredentials[$Key] = ""
    }
    return ($EmptyCredentials | ConvertTo-Json)
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text.TrimStart([char]0xFEFF).TrimEnd() + [Environment]::NewLine, $Utf8NoBom)
}

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
        throw "Failed to install multi-pan build dependencies"
    }
}

$env:PYTHONPATH = "$BuildDeps;$ProjectRoot;$env:PYTHONPATH"
$env:PYTHONNOUSERSITE = "1"

$ExcludeModules = @(
    "IPython",
    "PIL",
    "astroid",
    "black",
    "dask",
    "docutils",
    "flet",
    "flet_desktop",
    "jedi",
    "jinja2",
    "jsonschema",
    "matplotlib",
    "nbformat",
    "numpy",
    "pandas",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "pygments",
    "pytest",
    "scipy",
    "sphinx",
    "streamlit",
    "tornado"
)

$Providers = @(
    @{
        Name = "AliyunDriveDownloader"
        Folder = "aliyun_drive"
        Source = "aliyun_drive\aliyun_drive_direct_downloader.py"
        Keys = @("ALIYUN_ACCESS_TOKEN", "ALIYUN_REFRESH_TOKEN", "ALIYUN_DEFAULT_DRIVE_ID")
    },
    @{
        Name = "XunleiPanDownloader"
        Folder = "xunlei_pan"
        Source = "xunlei_pan\xunlei_pan_direct_downloader.py"
        Keys = @("XUNLEI_COOKIE", "XUNLEI_AUTHORIZATION", "XUNLEI_CAPTCHA_TOKEN", "XUNLEI_CLIENT_ID", "XUNLEI_DEVICE_ID")
    },
    @{
        Name = "QuarkPanDownloader"
        Folder = "quark_pan"
        Source = "quark_pan\quark_pan_direct_downloader.py"
        Keys = @("QUARK_COOKIE", "QUARK_TO_PDIR_FID")
    }
)

foreach ($Provider in $Providers) {
    $BuildPath = Join-Path $ProjectRoot ".build_deps\$($Provider.Name)"
    $SpecPath = Join-Path $BuildPath "spec"
    $DistPath = Join-Path $ProjectRoot "dist\$($Provider.Folder)"
    $DistExePath = Join-Path $DistPath "$($Provider.Name).exe"
    $DistCredentialsPath = Join-Path $DistPath "credentials.local.json"
    $SourceCredentialsPath = Join-Path $ProjectRoot "$($Provider.Folder)\credentials.local.json"
    $ExampleCredentialsPath = Join-Path $ProjectRoot "$($Provider.Folder)\credentials.local.example.json"

    $SavedCredentialsJson = Read-SavedCredentials `
        -Paths @($DistCredentialsPath, $SourceCredentialsPath, $ExampleCredentialsPath) `
        -Keys $Provider.Keys

    foreach ($Path in @($BuildPath, $DistPath)) {
        if (Test-Path -LiteralPath $Path) {
            $null = Assert-UnderProjectRoot $Path
            Remove-Item -LiteralPath $Path -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Force -Path $BuildPath | Out-Null
    New-Item -ItemType Directory -Force -Path $SpecPath | Out-Null
    New-Item -ItemType Directory -Force -Path $DistPath | Out-Null

    $env:PYTHONUSERBASE = Join-Path $BuildPath "pythonuserbase"
    $env:MPLCONFIGDIR = Join-Path $BuildPath "mplconfig"

    $PyInstallerArgs = @(
        "-s",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        $Provider.Name,
        "--paths",
        $ProjectRoot,
        "--distpath",
        $DistPath,
        "--workpath",
        (Join-Path $BuildPath "work"),
        "--specpath",
        $SpecPath
    )
    foreach ($Module in $ExcludeModules) {
        $PyInstallerArgs += @("--exclude-module", $Module)
    }
    $PyInstallerArgs += (Join-Path $ProjectRoot $Provider.Source)

    & .\bin\py.cmd @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed for $($Provider.Name) with exit code $LASTEXITCODE"
    }

    Write-Utf8NoBom -Path $DistCredentialsPath -Text $SavedCredentialsJson
    Write-Output "Built: $DistExePath"
    Write-Output "Config: $DistCredentialsPath"
}
