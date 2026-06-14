# baidu_pan_downloader

一个用于下载百度网盘分享文件的 Windows 命令行脚本。脚本会解析分享链接、验证提取码、递归读取分享内的文件列表，并把文件保存到本机指定目录。

当前默认下载目录：

```text
D:\Downloads\baidu_download
```

脚本不会自动解压下载结果。如果分享里是 ZIP 文件，下载完成后会保留 ZIP 原文件。

## 功能

- 支持带 `pwd` 提取码的百度网盘分享链接。
- 支持递归列出分享目录中的子文件夹和文件。
- 下载时保留分享目录结构，便于按原路径归档。
- 支持断点续传和分段 Range 下载。
- 支持 `tqdm` 进度条显示。
- 当分享直链被百度限制时，可从已经保存到自己网盘的同名同大小文件继续获取下载链接。
- 对部分 ZIP 受限文件，会在自己的网盘中临时把后缀改为 `.txt`，下载链接获取完成后再恢复原文件名。

## 使用前准备

项目需要 Python 和以下依赖：

```text
requests
tqdm
```

如果使用项目内的便携 Python 初始化脚本，在 PowerShell 中运行：

```powershell
.\setup_portable_python.ps1
```

也可以使用系统 Python：

```powershell
py -m pip install -r requirements.txt
```

## 配置登录凭据

脚本需要百度网盘登录态，用于访问分享页面、保存后的个人网盘文件和下载接口。

复制示例文件：

```powershell
Copy-Item .\credentials.local.example.json .\credentials.local.json
```

然后编辑 `credentials.local.json`：

```json
{
  "BDUSS": "your_bduss_here",
  "STOKEN": "your_stoken_here"
}
```

也可以运行脚本交互式保存：

```powershell
.\save_credentials.ps1
```

`credentials.local.json` 包含账号登录凭据，已被 `.gitignore` 忽略，不应该提交到 GitHub。

## 运行

使用系统 Python：

```powershell
py baidu_pan_downloader.py
```

使用项目内便携 Python：

```powershell
.\python\python.exe .\baidu_pan_downloader.py
```

运行后输入百度网盘分享链接，例如：

```text
https://pan.baidu.com/s/xxxxxxxxxxxxxxxxxxxxxx?pwd=abcd
```

脚本会显示解析出的文件列表，确认后开始下载。下载完成后文件会保存到：

```text
D:\Downloads\baidu_download
```

## 受限文件处理方式

百度网盘有时不会给分享文件返回可直接下载的浏览器直链，而是返回客户端加密下载任务。遇到这种情况时，脚本会尝试下面的备用流程：

1. 先在自己的百度网盘中搜索同名、同大小的文件。
2. 如果找到的是 ZIP 文件，会临时把自己网盘里的文件名从 `.zip` 改为 `.txt`。
3. 使用个人网盘下载接口获取下载链接。
4. 下载到本机默认目录。
5. 下载结束后把自己网盘里的文件名恢复为原始名称。

因此，如果分享直链失败，需要先手动把分享文件保存到自己的百度网盘。脚本不会自动保存分享文件，只会查找已经存在于自己网盘中的同名同大小文件。

## 项目文件

```text
baidu_pan_downloader.py          主下载脚本
requirements.txt                 Python 依赖
credentials.local.example.json   凭据配置示例
save_credentials.ps1             交互式保存本地凭据
setup_portable_python.ps1        初始化便携 Python 环境
run_local.ps1                    使用临时环境变量运行脚本
bin/py.cmd                       本机便携 Python 启动包装
```

## 注意事项

- 本项目仅用于下载你有权限访问的百度网盘文件。
- 不要把真实的 `credentials.local.json`、`BDUSS`、`STOKEN` 上传到公开仓库。
- 如果下载中断，重新运行脚本会根据已下载文件大小继续续传。
- 默认并发数是 `5`，如遇到频率限制，可以在 `baidu_pan_downloader.py` 中调低 `MAX_WORKERS`。
- 默认下载目录可以在 `baidu_pan_downloader.py` 中修改 `DOWNLOAD_ROOT`。
