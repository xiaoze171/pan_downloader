# PanDownload · 网盘三合一直链下载

把 **百度网盘 / 夸克网盘 / 阿里云盘** 三种分享链接合并到一个界面里：
粘贴任意一种链接 → 自动识别网盘类型 → 解析文件列表 → 选择并下载。

界面复用了仓库里成熟的 Flet 图形界面，并在其上做了美化（顶部网盘横幅、
实时链接识别徽标、统一主题）。

> 本项目**不修改任何原始代码**，完全复用 `baidu_pan` / `quark_pan` /
> `aliyun_drive` / `common_pan` 中已有的解析、取链与下载能力。

## 目录结构

```
pandownload/
├── __init__.py        # 包导出
├── __main__.py        # 支持 python -m pandownload
├── main.py            # 启动入口（GUI / CLI）
├── providers.py       # 链接识别 + 统一会话工厂（核心）
└── ui.py              # 美化版 Flet 界面（子类化 CommonPanFletApp）
```

## 运行

在仓库根目录（`pan_downloader/`）下执行：

```powershell
# 图形界面（推荐）
python -m pandownload

# 或直接运行
python pandownload\main.py
```

命令行模式：

```powershell
# 自动识别链接并下载
python -m pandownload --cli "https://pan.quark.cn/s/xxxxxxxx"

# 仅列出文件
python -m pandownload --cli "链接" --list-only

# 指定文件与目录
python -m pandownload --cli "链接" --select 1,3-5 --out D:\下载\test
```

## 凭据 / 登录

各网盘沿用原项目各自的凭据文件，互不影响：

| 网盘   | 凭据文件                                   | 说明                                   |
| ------ | ------------------------------------------ | -------------------------------------- |
| 百度   | `baidu_pan/credentials.local.json`         | 未登录时解析会自动打开浏览器登录       |
| 夸克   | `quark_pan/credentials.local.json`         | 需要 `QUARK_COOKIE`                    |
| 阿里   | `aliyun_drive/credentials.local.json`      | 需要 `ALIYUN_ACCESS_TOKEN`/`REFRESH`   |

填写方式参考各目录下的 `credentials.local.example.json` 与油猴脚本导出工具。

## 依赖

与主项目一致（见根目录 `requirements.txt`）：

```powershell
pip install -r requirements.txt
```

## 工作原理

- `providers.detect_provider()` 用正则识别链接归属。
- 夸克 / 阿里直接调用各自的 `create_gui_session()`，返回
  `common_pan.tk_gui.ProviderDownloadSession`。
- 百度是单体脚本，`providers._create_baidu_session()` 复用其
  `parse_share_link` / `get_yun_data` / `get_file_list_recursive` /
  `get_download_link`，包装成同样的 `ProviderDownloadSession`。
- 三者统一交给 `common_pan` 的下载器（断点续传 / 进度回调 / 取消）执行。
