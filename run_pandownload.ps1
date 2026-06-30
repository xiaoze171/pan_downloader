$ErrorActionPreference = "Stop"

# 在仓库根目录启动 PanDownload 图形界面。
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PortablePythonExe = Join-Path $ProjectRoot "python\python.exe"
$LocalPythonExe = "D:\app\conda\python.exe"

if (Test-Path $PortablePythonExe) {
    $PythonExe = $PortablePythonExe
}
elseif (Test-Path $LocalPythonExe) {
    $PythonExe = $LocalPythonExe
}
else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "未找到 Python。请先安装 Python 或运行 setup_portable_python.ps1。"
    }
    $PythonExe = $PythonCommand.Source
}

Push-Location $ProjectRoot
try {
    & $PythonExe -m pandownload @args
}
finally {
    Pop-Location
}
