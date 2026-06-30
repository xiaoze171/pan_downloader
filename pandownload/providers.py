"""统一的网盘识别与会话工厂。

设计原则：**不修改原始项目代码**，仅复用它们已经导出的能力：

* ``quark_pan.quark_pan_direct_downloader.create_gui_session``
* ``aliyun_drive.aliyun_drive_direct_downloader.create_gui_session``
* ``baidu_pan.baidu_pan_downloader`` 的若干解析/取链函数（百度是单体脚本，
  没有现成的 ``create_gui_session``，这里做一个轻量适配器把它接入通用框架）。

三者最终都被规范成 ``common_pan.tk_gui.ProviderDownloadSession``，从而复用
``common_pan`` 中成熟的下载、断点续传、进度回调逻辑。
"""

import os
import re
import sys
import threading
from typing import Callable, List, Optional

# ---------------------------------------------------------------------------
# 确保仓库根目录在 sys.path 上，这样可以直接 import 三个原始项目（命名空间包）。
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common_pan.core import CloudFile  # noqa: E402
from common_pan.tk_gui import ProviderDownloadSession, ProviderGuiConfig  # noqa: E402


# ---------------------------------------------------------------------------
# 各网盘的链接识别规则
# ---------------------------------------------------------------------------
_PROVIDER_PATTERNS = {
    "baidu": re.compile(r"pan\.baidu\.com/s/", re.IGNORECASE),
    "quark": re.compile(r"(?:pan\.quark|drive\.uc)\.cn/s/", re.IGNORECASE),
    "aliyun": re.compile(r"(?:aliyundrive|alipan)\.com/s/", re.IGNORECASE),
}

PROVIDERS = {
    "baidu": {"name": "百度网盘", "color": "#06a7ff", "accent": "#0a84ff"},
    "quark": {"name": "夸克网盘", "color": "#5b6cff", "accent": "#4f46e5"},
    "aliyun": {"name": "阿里云盘", "color": "#1677ff", "accent": "#2563eb"},
}


def detect_provider(text: str) -> Optional[str]:
    """根据分享文本识别所属网盘，返回 ``baidu`` / ``quark`` / ``aliyun`` 或 ``None``。"""
    if not text:
        return None
    for key, pattern in _PROVIDER_PATTERNS.items():
        if pattern.search(text):
            return key
    return None


def provider_display_name(key: Optional[str]) -> str:
    info = PROVIDERS.get(key or "")
    return info["name"] if info else "未识别"


# ---------------------------------------------------------------------------
# 夸克 / 阿里云盘：直接复用原项目的 create_gui_session
# ---------------------------------------------------------------------------
def _create_quark_session(share_text: str) -> ProviderDownloadSession:
    from quark_pan import quark_pan_direct_downloader as quark

    return quark.create_gui_session(share_text)


def _create_aliyun_session(share_text: str) -> ProviderDownloadSession:
    from aliyun_drive import aliyun_drive_direct_downloader as aliyun

    return aliyun.create_gui_session(share_text)


# ---------------------------------------------------------------------------
# 百度网盘：单体脚本，做一个适配器接入通用 ProviderDownloadSession
# ---------------------------------------------------------------------------
def _create_baidu_session(share_text: str) -> ProviderDownloadSession:
    from baidu_pan import baidu_pan_downloader as baidu

    # 未登录时复用百度自带的浏览器登录流程（会弹出 Edge 登录百度网盘）。
    if not baidu.BDUSS:
        print("百度网盘未检测到登录态，正在打开浏览器登录...")
        baidu.ensure_login_credentials()

    surl, pwd = baidu.parse_share_link(share_text)
    yun_data, session = baidu.get_yun_data(
        surl,
        pwd,
        password_provider=baidu.require_password_from_link,
    )

    all_entries = baidu.get_file_list_recursive(yun_data, session)
    folder_count = sum(1 for item in all_entries if item.get("isdir"))

    files: List[CloudFile] = []
    for item in all_entries:
        if item.get("isdir"):
            continue
        cloud_path = str(item.get("path") or item.get("name") or "").lstrip("/")
        files.append(
            CloudFile(
                file_id=str(item.get("fs_id") or ""),
                name=str(item.get("name") or cloud_path.rsplit("/", 1)[-1]),
                path=cloud_path,
                size=int(item.get("size") or 0),
                is_folder=False,
                raw=item,
            )
        )

    # 百度取链涉及共享 session / yun_data，并可能转存到自己网盘，必须串行。
    resolve_lock = threading.Lock()

    def resolve(file: CloudFile) -> str:
        item = file.raw
        with resolve_lock:
            dlink, _restore_info = baidu.get_download_link(
                session, yun_data, item, prefer_embedded=True
            )
        return dlink

    # 百度直链下载需要专用 UA + Referer + 登录 Cookie。
    cookie_header = "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in session.cookies if cookie.value
    )

    return ProviderDownloadSession(
        files=files,
        resolve_url=resolve,
        user_agent=baidu.DOWNLOAD_USER_AGENT,
        referer="https://pan.baidu.com/disk/home",
        extra_headers={"Cookie": cookie_header} if cookie_header else {},
        folder_count=folder_count,
        serialize_resolve=True,
    )


_SESSION_FACTORIES: dict = {
    "baidu": _create_baidu_session,
    "quark": _create_quark_session,
    "aliyun": _create_aliyun_session,
}


def unified_create_session(share_text: str) -> ProviderDownloadSession:
    """根据链接自动识别网盘并创建下载会话。"""
    text = (share_text or "").strip()
    provider = detect_provider(text)
    if provider is None:
        raise ValueError(
            "无法识别该分享链接所属的网盘。\n"
            "支持：百度网盘 (pan.baidu.com)、夸克网盘 (pan.quark.cn / drive.uc.cn)、"
            "阿里云盘 (alipan.com / aliyundrive.com)。"
        )
    factory = _SESSION_FACTORIES[provider]
    print(f"已识别为 {provider_display_name(provider)}，开始解析分享...")
    return factory(text)


# ---------------------------------------------------------------------------
# 启动时的凭据状态汇总（聚合三家）
# ---------------------------------------------------------------------------
def unified_credential_messages() -> List[str]:
    messages: List[str] = ["支持自动识别：百度网盘 / 夸克网盘 / 阿里云盘，直接粘贴分享链接即可。"]

    def _safe(label: str, getter: Callable[[], List[str]]) -> None:
        try:
            for line in getter():
                messages.append(f"[{label}] {line}")
        except Exception as exc:  # 某个网盘模块异常不应阻塞其他网盘
            messages.append(f"[{label}] 状态读取失败: {exc}")

    try:
        from quark_pan import quark_pan_direct_downloader as quark

        _safe("夸克", quark.gui_credential_messages)
    except Exception as exc:
        messages.append(f"[夸克] 模块加载失败: {exc}")

    try:
        from aliyun_drive import aliyun_drive_direct_downloader as aliyun

        _safe("阿里", aliyun.gui_credential_messages)
    except Exception as exc:
        messages.append(f"[阿里] 模块加载失败: {exc}")

    try:
        from baidu_pan import baidu_pan_downloader as baidu

        if baidu.BDUSS:
            messages.append("[百度] 已读取登录态 (BDUSS)。")
        else:
            messages.append("[百度] 未登录，解析时会自动打开浏览器登录。")
    except Exception as exc:
        messages.append(f"[百度] 模块加载失败: {exc}")

    return messages


def _default_download_dir() -> str:
    return os.path.join("D:\\", "下载", "pandownload")


def build_pandownload_config() -> ProviderGuiConfig:
    """构建统一的 GUI 配置。"""
    return ProviderGuiConfig(
        title="PanDownload · 网盘三合一直链下载",
        provider_name="网盘",
        default_download_dir=_default_download_dir(),
        credentials_file="(百度 / 夸克 / 阿里 各自的 credentials.local.json)",
        create_session=unified_create_session,
        credential_messages=unified_credential_messages,
        share_hint="粘贴 百度 / 夸克 / 阿里云盘 任意分享链接或完整分享文本，自动识别",
        default_file_workers=2,
        max_file_workers=4,
        default_retries=5,
        max_retries=10,
    )
