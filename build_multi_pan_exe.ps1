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

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
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
            throw "Flet desktop client cache not found: $FletClientCache. Run the app once on a networked build machine, or place flet-windows.zip at $FletClientArchive before building."
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

$BuildDeps = Join-Path $ProjectRoot ".build_deps"
if (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "PyInstaller"))) {
    & .\bin\py.cmd -m pip install --target $BuildDeps pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller"
    }
}
if (
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "requests"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "tqdm"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "flet"))) -or
    (-not (Test-Path -LiteralPath (Join-Path $BuildDeps "flet_desktop")))
) {
    & .\bin\py.cmd -m pip install --target $BuildDeps -r .\requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install multi-pan build dependencies"
    }
}

$FletClientArchive = Ensure-FletDesktopClient -BuildDeps $BuildDeps

$env:PYTHONPATH = "$BuildDeps;$ProjectRoot;$env:PYTHONPATH"
$env:PYTHONNOUSERSITE = "1"

$ExcludeModules = @(
    "IPython",
    "PIL",
    "astroid",
    "black",
    "dask",
    "docutils",
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

$CliExcludeModules = $ExcludeModules + @("flet", "flet_desktop")
$FletExcludeModules = $ExcludeModules + @("tkinter", "_tkinter")

$Providers = @(
    @{
        Name = "BaiduPanDownloader"
        Folder = "baidu_pan"
        Source = "baidu_pan\baidu_pan_downloader.py"
        Keys = @("BDUSS", "STOKEN")
        CredentialPaths = @(
            (Join-Path $ProjectRoot "dist\baidu_pan\credentials.local.json"),
            (Join-Path $ProjectRoot "baidu_pan\credentials.local.json"),
            (Join-Path $ProjectRoot "baidu_pan\credentials.local.example.json"),
            (Join-Path $ProjectRoot "dist\credentials.local.json"),
            (Join-Path $ProjectRoot "credentials.local.json")
        )
        Windowed = $true
        Flet = $true
    },
    @{
        Name = "AliyunDriveDownloader"
        Folder = "aliyun_drive"
        Source = "aliyun_drive\aliyun_drive_direct_downloader.py"
        Keys = @("ALIYUN_ACCESS_TOKEN", "ALIYUN_REFRESH_TOKEN", "ALIYUN_DEFAULT_DRIVE_ID")
        Windowed = $true
        Flet = $true
    },
    @{
        Name = "XunleiPanDownloader"
        Folder = "xunlei_pan"
        Source = "xunlei_pan\xunlei_pan_direct_downloader.py"
        Keys = @("XUNLEI_COOKIE", "XUNLEI_AUTHORIZATION", "XUNLEI_CAPTCHA_TOKEN", "XUNLEI_CLIENT_ID", "XUNLEI_DEVICE_ID")
        Windowed = $false
    },
    @{
        Name = "QuarkPanDownloader"
        Folder = "quark_pan"
        Source = "quark_pan\quark_pan_direct_downloader.py"
        Keys = @("QUARK_COOKIE", "QUARK_TO_PDIR_FID")
        Windowed = $true
        Flet = $true
    }
)

foreach ($Provider in $Providers) {
    $SourcePath = Join-Path $ProjectRoot $Provider.Source
    if (-not (Test-Path -LiteralPath $SourcePath)) {
        Write-Warning "Skipping missing provider source: $SourcePath"
        continue
    }

    $BuildPath = Join-Path $ProjectRoot ".build_deps\$($Provider.Name)"
    $SpecPath = Join-Path $BuildPath "spec"
    $DistPath = if ([string]::IsNullOrWhiteSpace($Provider.Folder)) {
        Join-Path $ProjectRoot "dist"
    } else {
        Join-Path $ProjectRoot "dist\$($Provider.Folder)"
    }
    $DistExePath = Join-Path $DistPath "$($Provider.Name).exe"
    $DistCredentialsPath = Join-Path $DistPath "credentials.local.json"
    $SourceCredentialsPath = Join-Path $ProjectRoot "$($Provider.Folder)\credentials.local.json"
    $ExampleCredentialsPath = Join-Path $ProjectRoot "$($Provider.Folder)\credentials.local.example.json"
    $CredentialPaths = if ($Provider.ContainsKey("CredentialPaths")) {
        $Provider.CredentialPaths
    } else {
        @($DistCredentialsPath, $SourceCredentialsPath, $ExampleCredentialsPath)
    }

    $SavedCredentialsJson = Read-SavedCredentials `
        -Paths $CredentialPaths `
        -Keys $Provider.Keys

    $CleanPaths = @($BuildPath, $DistPath)
    foreach ($Path in $CleanPaths) {
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

    $PyInstallerArgs = @(
        "-s",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
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
    if ($Provider.Windowed) {
        $PyInstallerArgs += "--windowed"
    } else {
        $PyInstallerArgs += "--console"
    }
    if ($Provider.Flet) {
        $PyInstallerArgs += @("--collect-all", "flet")
        $PyInstallerArgs += @("--collect-all", "flet_desktop")
        $PyInstallerArgs += @("--add-data", "$FletClientArchive;flet_desktop/app")
        $PyInstallerArgs += @("--hidden-import", "flet_desktop")
        $ProviderExcludeModules = $FletExcludeModules
    } else {
        $ProviderExcludeModules = $CliExcludeModules
    }
    foreach ($Module in $ProviderExcludeModules) {
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
