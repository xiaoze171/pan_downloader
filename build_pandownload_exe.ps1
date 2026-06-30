$ErrorActionPreference = "Stop"

# ============================================================================
# Build PanDownload (Baidu / Quark / Aliyun combined) into a single-file exe.
# Reuses the dependency / flet-client / exclude logic from build_multi_pan_exe.ps1.
# Usage:  .\build_pandownload_exe.ps1
# Output: dist\pandownload\PanDownload.exe
#         dist\pandownload\credentials.local.json  (merged 3-provider credentials)
# ============================================================================

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

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text.TrimStart([char]0xFEFF).TrimEnd() + [Environment]::NewLine, $Utf8NoBom)
}

# Merge the three providers' credentials into one JSON.
# Each module's load_credentials only reads the keys it cares about.
function Build-MergedCredentials {
    $Merged = [ordered]@{
        "BDUSS"                   = ""
        "STOKEN"                  = ""
        "QUARK_COOKIE"            = ""
        "QUARK_TO_PDIR_FID"       = "0"
        "ALIYUN_ACCESS_TOKEN"     = ""
        "ALIYUN_REFRESH_TOKEN"    = ""
        "ALIYUN_DEFAULT_DRIVE_ID" = ""
    }
    $Sources = @(
        (Join-Path $ProjectRoot "baidu_pan\credentials.local.json"),
        (Join-Path $ProjectRoot "quark_pan\credentials.local.json"),
        (Join-Path $ProjectRoot "aliyun_drive\credentials.local.json")
    )
    foreach ($Src in $Sources) {
        if (-not (Test-Path -LiteralPath $Src)) { continue }
        try {
            $Text = [System.IO.File]::ReadAllText($Src).TrimStart([char]0xFEFF)
            $Obj = $Text | ConvertFrom-Json
        }
        catch {
            Write-Warning "Skip unparseable credentials file: $Src"
            continue
        }
        foreach ($Prop in $Obj.PSObject.Properties) {
            $Value = "$($Prop.Value)".Trim()
            if ($Value -and $Value -notin @("your_bduss_here", "your_stoken_here", "your_value_here")) {
                $Merged[$Prop.Name] = $Value
            }
        }
    }
    return ($Merged | ConvertTo-Json)
}

function Ensure-FletDesktopClient {
    param([Parameter(Mandatory = $true)][string]$BuildDeps)

    $FletDesktopPath = Join-Path $BuildDeps "flet_desktop"
    $FletDesktopAppPath = Join-Path $FletDesktopPath "app"
    $FletClientArchive = Join-Path $FletDesktopAppPath "flet-windows.zip"
    $FletVersionPath = Join-Path $FletDesktopPath "version.py"
    $FletVersion = "0.85.3"
    if (Test-Path -LiteralPath $FletVersionPath) {
        $VersionText = [System.IO.File]::ReadAllText($FletVersionPath)
        if ($VersionText -match 'version\s*=\s*"([^"]+)"') {
            $FletVersion = $Matches[1]
        }
    }
    if (-not (Test-Path -LiteralPath $FletClientArchive)) {
        $FletClientCache = Join-Path $HOME ".flet\client\flet-desktop-full-$FletVersion"
        if (-not (Test-Path -LiteralPath (Join-Path $FletClientCache "flet\flet.exe"))) {
            throw "Flet desktop client cache not found: $FletClientCache. Run the GUI once on a networked machine, or place flet-windows.zip at $FletClientArchive before building."
        }
        Ensure-Directory -Path $FletDesktopAppPath
        $FletClientTempArchive = Join-Path $env:TEMP "flet-windows-$FletVersion.zip"
        if (Test-Path -LiteralPath $FletClientTempArchive) {
            Remove-Item -LiteralPath $FletClientTempArchive -Force
        }
        Compress-Archive -LiteralPath (Join-Path $FletClientCache "flet") -DestinationPath $FletClientTempArchive -Force
        Move-Item -LiteralPath $FletClientTempArchive -Destination $FletClientArchive -Force
    }
    return $FletClientArchive
}

# --- Install build dependencies ------------------------------------------
$BuildDeps = Join-Path $ProjectRoot ".build_deps"
if (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "PyInstaller"))) {
    & .\bin\py.cmd -m pip install --target $BuildDeps pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller" }
}
if (
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "requests"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "tqdm"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "flet"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "flet_desktop")))
) {
    & .\bin\py.cmd -m pip install --target $BuildDeps -r .\requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies" }
}

$FletClientArchive = Ensure-FletDesktopClient -BuildDeps $BuildDeps

$env:PYTHONPATH = "$BuildDeps;$ProjectRoot;$env:PYTHONPATH"
$env:PYTHONNOUSERSITE = "1"

$ExcludeModules = @(
    "IPython", "PIL", "astroid", "black", "dask", "docutils", "jedi",
    "jinja2", "jsonschema", "matplotlib", "nbformat", "numpy", "pandas",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "pygments", "pytest", "scipy",
    "sphinx", "streamlit", "tornado", "tkinter", "_tkinter"
)

# --- Configuration --------------------------------------------------------
$Name = "PanDownload"
$Source = Join-Path $ProjectRoot "pandownload\main.py"
$BuildPath = Join-Path $ProjectRoot ".build_deps\$Name"
$SpecPath = Join-Path $BuildPath "spec"
$DistPath = Join-Path $ProjectRoot "dist\pandownload"
$DistExePath = Join-Path $DistPath "$Name.exe"
$DistCredentialsPath = Join-Path $DistPath "credentials.local.json"

if (-not (Test-Path -LiteralPath $Source)) { throw "Entry not found: $Source" }

$SavedCredentialsJson = Build-MergedCredentials

foreach ($Path in @($BuildPath, $DistPath)) {
    if (Test-Path -LiteralPath $Path) {
        $null = Assert-UnderProjectRoot $Path
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}
Ensure-Directory -Path $BuildPath
Ensure-Directory -Path $SpecPath
Ensure-Directory -Path $DistPath

$env:PYTHONUSERBASE = Join-Path $BuildPath "pythonuserbase"
$env:MPLCONFIGDIR = Join-Path $BuildPath "mplconfig"

# The 3 providers are namespace packages with lazy imports, so collect them explicitly.
$HiddenImports = @(
    "pandownload", "pandownload.providers", "pandownload.ui", "pandownload.main",
    "common_pan", "common_pan.core", "common_pan.tk_gui", "common_pan.flet_gui",
    "baidu_pan.baidu_pan_downloader",
    "quark_pan.quark_pan_direct_downloader", "quark_pan.flet_gui",
    "aliyun_drive.aliyun_drive_direct_downloader", "aliyun_drive.flet_gui",
    "flet_desktop"
)
# pandownload & common_pan have __init__.py -> collect-all (grabs flet_gui too).
# baidu/quark/aliyun are namespace packages (no __init__.py) -> collect-submodules.
$CollectAllPackages = @("pandownload", "common_pan")
$CollectSubmodules = @("baidu_pan", "quark_pan", "aliyun_drive")

$PyInstallerArgs = @(
    "-s", "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", $Name,
    "--paths", $ProjectRoot,
    "--distpath", $DistPath,
    "--workpath", (Join-Path $BuildPath "work"),
    "--specpath", $SpecPath,
    "--collect-all", "flet",
    "--collect-all", "flet_desktop",
    "--add-data", "$FletClientArchive;flet_desktop/app"
)
foreach ($Module in $HiddenImports) { $PyInstallerArgs += @("--hidden-import", $Module) }
foreach ($Pkg in $CollectAllPackages) { $PyInstallerArgs += @("--collect-all", $Pkg) }
foreach ($Pkg in $CollectSubmodules) { $PyInstallerArgs += @("--collect-submodules", $Pkg) }
foreach ($Module in $ExcludeModules) { $PyInstallerArgs += @("--exclude-module", $Module) }
$PyInstallerArgs += $Source

& .\bin\py.cmd @PyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Name (exit $LASTEXITCODE)" }

Write-Utf8NoBom -Path $DistCredentialsPath -Text $SavedCredentialsJson
Write-Output ""
Write-Output "Built : $DistExePath"
Write-Output "Config: $DistCredentialsPath  (merged Baidu / Quark / Aliyun credentials)"
Write-Output "Note  : Fill real credentials into that json, or let Baidu auto-open the browser login at runtime."
