# Multi Pan Direct Download Scripts

This document covers the standalone scripts for Aliyun Drive, Xunlei Pan, and Quark Pan.
The Baidu downloader is not changed.

## Shared Behavior

The three new scripts keep separate provider-specific folders, but share the common code in `common_pan/`.
Common behavior includes:

- Recursive share file listing.
- File index selection with `--select all`, `--select 1`, or `--select 1,3-5`.
- Direct link resolving.
- Optional download with `--download`.
- Resume from `.part` files.
- Retry with fresh direct links.
- Existing-file skip unless `--overwrite` is passed.
- File-level concurrency with `--file-workers`.
- Consistent output directory handling and path sanitizing.

Provider-specific code should only handle login credentials, share parsing, file listing, and direct-link API calls.
Download bugs should normally be fixed in `common_pan/core.py`.

## Credentials

Each folder has a `credentials.local.example.json`.
Copy it to `credentials.local.json` in the same folder and fill the required values.
`credentials.local.json` is ignored by Git.

You can use the matching Tampermonkey userscript to export the JSON:

- `aliyun_drive/aliyun_drive_token_exporter.user.js`
- `xunlei_pan/xunlei_pan_token_exporter.user.js`
- `quark_pan/quark_pan_token_exporter.user.js`

Install the script in Tampermonkey, open the matching logged-in web disk page, click `Refresh`, then click `Copy JSON`.
Paste the copied JSON into that provider folder's `credentials.local.json`.
For Xunlei Pan, open `https://pan.xunlei.com/`, finish any captcha/verification shown by the page, then export again.
The script needs `XUNLEI_CAPTCHA_TOKEN`; without it the share API returns `captcha_token is empty`.

Aliyun Drive:

```json
{
  "ALIYUN_ACCESS_TOKEN": "",
  "ALIYUN_REFRESH_TOKEN": "",
  "ALIYUN_DEFAULT_DRIVE_ID": ""
}
```

Xunlei Pan:

```json
{
  "XUNLEI_COOKIE": "",
  "XUNLEI_AUTHORIZATION": "",
  "XUNLEI_CAPTCHA_TOKEN": "",
  "XUNLEI_CLIENT_ID": "",
  "XUNLEI_DEVICE_ID": ""
}
```

Quark Pan:

```json
{
  "QUARK_COOKIE": "",
  "QUARK_TO_PDIR_FID": "0"
}
```

## Verify Links First

List files only:

```powershell
.\bin\py.cmd .\aliyun_drive\aliyun_drive_direct_downloader.py "share text" --pwd abcd --list-only
.\bin\py.cmd .\xunlei_pan\xunlei_pan_direct_downloader.py "share text" --pwd abcd --list-only
.\bin\py.cmd .\quark_pan\quark_pan_direct_downloader.py "share text" --pwd abcd --list-only
```

Resolve and print direct links:

```powershell
.\bin\py.cmd .\aliyun_drive\aliyun_drive_direct_downloader.py "share text" --pwd abcd --select 1
.\bin\py.cmd .\xunlei_pan\xunlei_pan_direct_downloader.py "share text" --pwd abcd --select 1
.\bin\py.cmd .\quark_pan\quark_pan_direct_downloader.py "share text" --pwd abcd --select 1
```

Download selected files:

```powershell
.\bin\py.cmd .\aliyun_drive\aliyun_drive_direct_downloader.py "share text" --pwd abcd --select 1,3-5 --download
.\bin\py.cmd .\xunlei_pan\xunlei_pan_direct_downloader.py "share text" --pwd abcd --select 1,3-5 --download
.\bin\py.cmd .\quark_pan\quark_pan_direct_downloader.py "share text" --pwd abcd --select 1,3-5 --download
```

Download with file concurrency:

```powershell
.\bin\py.cmd .\aliyun_drive\aliyun_drive_direct_downloader.py "share text" --pwd abcd --download --file-workers 2
```

Quark saves shared files to your own cloud before downloading by default:

```powershell
.\bin\py.cmd .\quark_pan\quark_pan_direct_downloader.py "share text" --pwd abcd --download
```

Aliyun fallback when direct share download is blocked:

```powershell
.\bin\py.cmd .\aliyun_drive\aliyun_drive_direct_downloader.py "share text" --pwd abcd --download --save-first
```

## Test Script

The repository has a test runner that reads links from `测试数据以及对应配置`.

List files:

```powershell
.\测试下载脚本.ps1 -Mode list -KeepGoing
```

Download the first small file. Quark always saves to your own cloud first:

```powershell
.\测试下载脚本.ps1 -Mode download -Provider aliyun -Select 1 -SaveFirst -Overwrite
.\测试下载脚本.ps1 -Mode download -Provider quark -Select 1 -SaveFirst -Overwrite
```

Default test download output:

```text
D:\pan_downloader_test_download
```
