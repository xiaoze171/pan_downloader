# baidu_pan_downloader

一个用于下载百度网盘分享文件的 Windows 桌面工具。程序会解析分享链接、验证提取码、递归读取分享内的文件列表，并把文件保存到本机指定目录。

当前默认下载目录：

```text
D:\下载\baidu_download
```

脚本不会自动解压下载结果。如果分享里是 ZIP 文件，下载完成后会保留 ZIP 原文件。

## 功能

- 自动从分享链接或整段分享文本中截取提取码。
- 默认打开桌面窗口，集中完成登录、解析、选择目录和下载。
- 支持递归列出分享目录中的子文件夹和文件。
- 下载时保留分享目录结构，便于按原路径归档。
- 支持断点续传和单文件多线程分段 Range 下载。
- 支持按文件选择下载，解析后不会默认下载全部文件。
- 窗口文件列表显示每个文件的进度百分比、速度和状态。
- 支持 `tqdm` 进度条显示。
- 首次运行可自动打开百度网页登录页，登录后自动保存本地登录态。
- 当分享直链被百度限制时，可从已经保存到自己网盘的同名同大小文件继续获取下载链接。
- 对部分 ZIP 受限文件，会在自己的网盘中临时把后缀改为 `.txt`，下载链接获取完成后再恢复原文件名。

## 使用前准备

项目需要 Python 和以下依赖：

```text
requests
tqdm
flet
flet-desktop
```

如果使用项目内的便携 Python 初始化脚本，在 PowerShell 中运行：

```powershell
.\setup_portable_python.ps1
```

也可以使用系统 Python：

```powershell
py -m pip install -r requirements.txt
```

## 登录

脚本需要百度网盘登录态，用于访问分享页面、自动转存文件和调用下载接口。

首次运行如果没有 `credentials.local.json`，脚本会直接调用本机 Microsoft Edge 打开百度网盘登录窗口。你在窗口里完成登录后，脚本会通过浏览器调试接口读取登录 Cookie，保存到 `credentials.local.json`，然后继续解析和下载分享资源。

后续运行会直接复用本地登录态。如果 Cookie 失效，脚本会重新打开网页登录页刷新登录态。

也可以继续手动配置 Cookie 作为备用方式。复制示例文件：

```powershell
Copy-Item .\baidu_pan\credentials.local.example.json .\baidu_pan\credentials.local.json
```

然后编辑 `baidu_pan\credentials.local.json`：

```json
{
  "BDUSS": "your_bduss_here",
  "STOKEN": "your_stoken_here"
}
```

### 使用油猴脚本导出 Cookie

如果网页登录自动读取失败，可以用项目里的油猴脚本导出 `BDUSS` 和 `STOKEN`：

1. 安装 Tampermonkey / 篡改猴扩展。
2. 新建用户脚本，把 `baidu_pan\baidu_pan_cookie_exporter.user.js` 的内容复制进去并保存。
3. 打开 `https://pan.baidu.com/`，确认当前浏览器已经登录百度网盘。
4. 页面右下角会出现 `Baidu Pan Cookie Exporter` 面板，点击 `Refresh`。
5. 如果自动读取成功，点击 `Copy JSON` 复制配置内容。
6. 如果自动读取失败，按面板里的提示从浏览器开发者工具的 Cookies 页面手动复制 `BDUSS` 和 `STOKEN`，再点击导出。
7. 把复制得到的 JSON 写入程序同目录的 `credentials.local.json`；运行 EXE 时写入 `dist\baidu_pan\credentials.local.json`。

油猴脚本只在本地页面读取 Cookie 并生成 JSON，不会上传数据。`BDUSS` 和 `STOKEN` 等同账号登录凭据，不要发给别人，也不要提交到仓库。

或者运行脚本交互式保存：

```powershell
.\save_credentials.ps1
```

`credentials.local.json` 包含账号登录凭据，已被 `.gitignore` 忽略，不应该提交到 GitHub。自动网页登录需要本机安装 Microsoft Edge；如果浏览器安装在非标准路径，可通过 `BAIDU_EDGE_PATH` 指定 `msedge.exe` 路径。

## 运行窗口

直接运行打包好的 EXE：

```powershell
.\dist\baidu_pan\BaiduPanDownloader.exe
```

窗口版默认优先尝试 Flet 界面。打包脚本会把 Flet 桌面 client 一起打进 EXE，复制到没有外网的新电脑后不需要再在线下载 Flet 资源。如果 Flet 仍启动失败，程序会自动切换到 Tkinter 界面。也可以强制使用 Tkinter：

```powershell
$env:BAIDU_GUI="tkinter"
.\dist\baidu_pan\BaiduPanDownloader.exe
```

使用系统 Python：

```powershell
py .\baidu_pan\baidu_pan_downloader.py
```

使用本仓库的 Python 启动包装（优先使用 `.\python\python.exe`，否则使用本机 `D:\app\conda\python.exe`）：

```powershell
.\bin\py.cmd .\baidu_pan\baidu_pan_downloader.py
```

使用项目内便携 Python：

```powershell
.\python\python.exe .\baidu_pan\baidu_pan_downloader.py
```

运行后在窗口中粘贴百度网盘分享链接或复制出来的整段分享文本，例如：

```text
https://pan.baidu.com/s/xxxxxxxxxxxxxxxxxxxxxx?pwd=abcd
```

也支持：

```text
链接: https://pan.baidu.com/s/xxxxxxxxxxxxxxxxxxxxxx 提取码: abcd
```

窗口会显示解析出的文件列表。默认不会下载全部文件，请在“选择”列勾选需要下载的文件，也可以使用“全选 / 全不选 / 反选”批量调整。下载完成后文件会保存到：

```text
D:\下载\baidu_download
```

窗口中的“文件并发”控制同时下载几个文件，“单文件线程”控制一个大文件内部同时拉取几个分片。默认是文件并发 `1`、单文件线程 `4`，通常比单线程更容易跑满带宽，也比同时下载多个大文件更稳。

下载中关闭窗口会取消当前任务并退出程序；未完成的 `.parts` 分片会保留，下次重新选择同一文件可继续续传。

仍然可以使用旧的命令行模式：

```powershell
.\bin\py.cmd .\baidu_pan\baidu_pan_downloader.py --cli
```

## 打包 EXE

重新打包所有网盘程序：

```powershell
.\build_multi_pan_exe.ps1
```

构建脚本会把本机缓存的 Flet 桌面 client 打包进 EXE，来源路径类似：

```text
%USERPROFILE%\.flet\client\flet-desktop-full-0.85.3
```

如果构建机没有这个缓存，先在有外网的机器上运行一次程序让 Flet 完成初始化，或者手动把 `flet-windows.zip` 放到：

```text
.build_deps\flet_desktop\app\flet-windows.zip
```

输出文件：

```text
dist\baidu_pan\BaiduPanDownloader.exe
dist\aliyun_drive\AliyunDriveDownloader.exe
dist\quark_pan\QuarkPanDownloader.exe
```

EXE 会把 `credentials.local.json` 保存到对应 EXE 所在目录。打包依赖会安装到项目本地 `.build_deps` 目录，构建产物位于 `build`、`.build_deps\*\work` 和 `dist`，这些目录不会提交到 Git。

打包完成后 `dist` 目录会包含：

```text
dist\baidu_pan\BaiduPanDownloader.exe
dist\baidu_pan\credentials.local.json
dist\aliyun_drive\AliyunDriveDownloader.exe
dist\aliyun_drive\credentials.local.json
dist\quark_pan\QuarkPanDownloader.exe
dist\quark_pan\credentials.local.json
```

`credentials.local.json` 默认是空配置，程序网页登录成功后会自动写入登录态。

## 受限文件处理方式

百度网盘有时不会给分享文件返回可直接下载的浏览器直链，而是返回客户端加密下载任务。遇到这种情况时，脚本会尝试下面的备用流程：

1. 先在自己的百度网盘中搜索同名、同大小的文件。
2. 如果没有找到，自动把分享文件保存到自己网盘的 `/baidu_pan_downloader/{shareid}` 目录。
3. 如果需要，会临时把自己网盘里的文件名改为 `.txt` 后再请求下载链接。
4. 使用个人网盘下载接口获取下载链接。
5. 下载到本机默认目录。
6. 下载结束后把自己网盘里的文件名恢复为原始名称。

因此，如果分享直链失败，脚本会优先复用已经保存到自己网盘的同名同大小文件；没有找到时会自动转存后再下载。自动转存仍然受百度网盘空间、账号状态和分享文件限制影响。

## 项目文件

```text
baidu_pan/baidu_pan_downloader.py 主下载脚本
requirements.txt                 Python 依赖
baidu_pan/credentials.local.example.json 凭据配置示例
save_credentials.ps1             交互式保存本地凭据
baidu_pan/baidu_pan_cookie_exporter.user.js 油猴脚本，用于导出 BDUSS/STOKEN
setup_portable_python.ps1        初始化便携 Python 环境
run_local.ps1                    使用临时环境变量运行脚本
bin/py.cmd                       本机便携 Python 启动包装
build_multi_pan_exe.ps1          打包所有网盘 EXE
```

## 辅助脚本说明

```text
setup_portable_python.ps1        初始化项目内便携 Python 环境，下载嵌入式 Python、安装 pip 和 requirements.txt 依赖。
save_credentials.ps1             手动写入百度登录凭据，会把 BDUSS/STOKEN 保存到 baidu_pan/credentials.local.json。
run_local.ps1                    使用临时环境变量运行百度下载器的旧快捷脚本，适合手动输入 BDUSS/STOKEN 后临时启动。
test_multi_pan_downloads.py      多网盘测试 runner，从 share_links.local.txt 读取分享链接并调用对应网盘脚本做列目录、解析直链或下载测试。
```

## 注意事项

- 本项目仅用于下载你有权限访问的百度网盘文件。
- 不要把真实的 `credentials.local.json`、`BDUSS`、`STOKEN` 上传到公开仓库。
- 如果下载中断，重新运行脚本会根据已下载文件大小继续续传。
- 多线程下载会在目标文件旁边临时生成 `.parts` 分片缓存，全部分片完成后再合并成最终文件。
- 下载中关闭窗口会取消当前任务并退出程序；未完成的 `.parts` 分片会保留，下次重新选择同一文件可继续续传。
- 如果百度下载 CDN 连接超时，程序会先在分片内重连；需要刷新下载链接时，只要本轮有新增下载进度，就会继续续传而不是直接判失败。
- 大文件续传刷新链接时，如果分享下载接口提示验证码过期，程序会改用已转存到自己网盘的文件，并刷新自己网盘下载签名后继续。
- 默认文件并发数是 `1`，用于降低多个大文件同时下载时的限流风险；需要更高带宽时优先调高“单文件线程”。
- 百度接口和文件直链下载默认不使用系统代理，避免解析或大文件下载时代理连接被重置。
- 默认下载目录可以在 `baidu_pan\baidu_pan_downloader.py` 中修改 `DOWNLOAD_ROOT`。
