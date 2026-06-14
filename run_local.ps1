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
$PythonExe = Join-Path $ProjectRoot "python\python.exe"
$ScriptPath = Join-Path $ProjectRoot "baidu_pan_downloader.py"

if (-not (Test-Path $PythonExe)) {
    throw "未找到便携 Python: $PythonExe。请先运行 setup_portable_python.ps1。"
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
