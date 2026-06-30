"""PanDownload —— 百度网盘 / 夸克网盘 / 阿里云盘 三合一直链下载。

本包不修改任何原始项目代码，仅复用 ``baidu_pan`` / ``quark_pan`` /
``aliyun_drive`` / ``common_pan`` 中已有的能力，提供统一的 URL 自动识别与界面。
"""

from .providers import (
    PROVIDERS,
    detect_provider,
    build_pandownload_config,
    unified_create_session,
)

__all__ = [
    "PROVIDERS",
    "detect_provider",
    "build_pandownload_config",
    "unified_create_session",
]

__version__ = "1.0.0"
