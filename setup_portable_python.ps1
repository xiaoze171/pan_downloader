$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonVersion = "3.12.4"
$PythonDir = Join-Path $ProjectRoot "python"
$PythonZip = Join-Path $ProjectRoot "python-$PythonVersion-embed-amd64.zip"
$GetPip = Join-Path $ProjectRoot "get-pip.py"
$PythonExe = Join-Path $PythonDir "python.exe"
$PthFile = Join-Path $PythonDir "python312._pth"
$Requirements = Join-Path $ProjectRoot "requirements.txt"

if (-not (Test-Path $PythonExe)) {
    if (-not (Test-Path $PythonZip)) {
        $PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonZip
    }

    if (-not (Test-Path $PythonDir)) {
        New-Item -ItemType Directory -Path $PythonDir | Out-Null
    }

    Expand-Archive -LiteralPath $PythonZip -DestinationPath $PythonDir -Force
}

if (Test-Path $PthFile) {
    $PthContent = Get-Content -LiteralPath $PthFile
    $PthContent = $PthContent -replace "^#import site$", "import site"
    Set-Content -LiteralPath $PthFile -Value $PthContent -Encoding ASCII
}

& $PythonExe -c "import sys; print(sys.version)"

if (-not (Test-Path (Join-Path $PythonDir "Scripts\pip.exe"))) {
    if (-not (Test-Path $GetPip)) {
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip
    }
    & $PythonExe $GetPip --no-warn-script-location
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r $Requirements
& $PythonExe -c "import requests, tqdm; print('dependencies ok')"
