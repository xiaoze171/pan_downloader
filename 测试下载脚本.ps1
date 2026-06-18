param(
    [ValidateSet("list", "links", "download")]
    [string]$Mode = "links",

    [ValidateSet("all", "aliyun", "xunlei", "quark")]
    [string]$Provider = "all",

    [string]$Select = "1",
    [string]$Out = "D:\pan_downloader_test_download",
    [int]$Retries = 3,
    [int]$FileWorkers = 1,
    [switch]$PrintLinks,
    [switch]$Overwrite,
    [switch]$SaveFirst,
    [switch]$QuarkSaveFirst,
    [switch]$KeepGoing
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonLauncher = Join-Path $ProjectRoot "bin\py.cmd"
$TestScript = Join-Path $ProjectRoot "test_multi_pan_downloads.py"

$argsList = @(
    $TestScript,
    "--mode", $Mode,
    "--provider", $Provider,
    "--select", $Select,
    "--out", $Out,
    "--retries", [string]$Retries,
    "--file-workers", [string]$FileWorkers
)

if ($PrintLinks) { $argsList += "--print-links" }
if ($Overwrite) { $argsList += "--overwrite" }
if ($SaveFirst) { $argsList += "--save-first" }
if ($QuarkSaveFirst) { $argsList += "--quark-save-first" }
if ($KeepGoing) { $argsList += "--keep-going" }

& $PythonLauncher @argsList
exit $LASTEXITCODE
