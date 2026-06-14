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
$CredentialsPath = Join-Path $ProjectRoot "credentials.local.json"

$bdussSecure = Read-Host "BAIDU_BDUSS" -AsSecureString
$stokenSecure = Read-Host "BAIDU_STOKEN" -AsSecureString

$credentials = [ordered]@{
    BDUSS = Convert-SecureStringToPlainText $bdussSecure
    STOKEN = Convert-SecureStringToPlainText $stokenSecure
}

$credentials | ConvertTo-Json | Set-Content -LiteralPath $CredentialsPath -Encoding UTF8
Write-Output "已保存到 $CredentialsPath"
