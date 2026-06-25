$ErrorActionPreference = "Stop"

function Convert-SecureStringToPlainText {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.SecureString] $SecureString
    )

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PortablePythonExe = Join-Path $ProjectRoot "python\python.exe"
$LocalPythonExe = "D:\app\conda\python.exe"
$ScriptPath = Join-Path $ProjectRoot "baidu_pan\baidu_pan_downloader.py"

if (Test-Path $PortablePythonExe) {
    $PythonExe = $PortablePythonExe
}
elseif (Test-Path $LocalPythonExe) {
    $PythonExe = $LocalPythonExe
}
else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "Python was not found. Install Python or run setup_portable_python.ps1 first."
    }
    $PythonExe = $PythonCommand.Source
}

$bdussSecure = Read-Host "BAIDU_BDUSS" -AsSecureString
$stokenSecure = Read-Host "BAIDU_STOKEN" -AsSecureString

$env:BAIDU_BDUSS = Convert-SecureStringToPlainText $bdussSecure
$env:BAIDU_STOKEN = Convert-SecureStringToPlainText $stokenSecure

try {
    & $PythonExe $ScriptPath
}
finally {
    Remove-Item Env:\BAIDU_BDUSS -ErrorAction SilentlyContinue
    Remove-Item Env:\BAIDU_STOKEN -ErrorAction SilentlyContinue
}
