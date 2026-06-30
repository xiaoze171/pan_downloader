"""PanDownload 的界面层。

复用 ``common_pan.flet_gui.CommonPanFletApp`` 这套成熟的 Flet 界面（"ui 界面还是
用这个"），通过子类化做视觉美化 + 网盘自动识别提示，**不修改原始界面代码**。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common_pan.flet_gui import CommonPanFletApp  # noqa: E402
from common_pan.tk_gui import ProviderGuiConfig  # noqa: E402

from .providers import PROVIDERS, detect_provider, provider_display_name  # noqa: E402


class PanDownloadFletApp(CommonPanFletApp):
    """在通用界面之上叠加：主题美化、顶部网盘识别徽标、输入即时识别。"""

    def __init__(self, page, ft_module, config: ProviderGuiConfig):
        super().__init__(page, ft_module, config)
        self._install_provider_badge()
        self._wire_live_detection()
        self._refresh_provider_badge(self.share_url_field.value or "")
        self.safe_update()

    # ------------------------------------------------------------------
    # 主题美化
    # ------------------------------------------------------------------
    def configure_page(self) -> None:
        super().configure_page()
        ft = self.ft
        self.page.title = self.config.title
        self.page.bgcolor = "#eef2f9"
        try:
            self.page.theme = ft.Theme(
                color_scheme_seed="#2563eb",
                font_family="Microsoft YaHei UI",
                visual_density=ft.VisualDensity.COMFORTABLE,
            )
        except Exception:
            pass
        try:
            self.page.window.width = 1440
            self.page.window.height = 820
            self.page.window.min_width = 1180
            self.page.window.min_height = 640
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 顶部网盘识别横幅（独立 banner，使用公开的 page.controls 树，避免依赖 .parent）
    # ------------------------------------------------------------------
    def _provider_chip(self, key: str):
        ft = self.ft
        info = PROVIDERS[key]
        return ft.Container(
            bgcolor="#ffffff",
            border_radius=999,
            padding=ft.Padding(left=10, top=5, right=12, bottom=5),
            border=ft.Border(
                left=ft.BorderSide(width=1, color="#e2e8f0"),
                top=ft.BorderSide(width=1, color="#e2e8f0"),
                right=ft.BorderSide(width=1, color="#e2e8f0"),
                bottom=ft.BorderSide(width=1, color="#e2e8f0"),
            ),
            content=ft.Row(
                [
                    ft.Container(width=8, height=8, border_radius=999, bgcolor=info["color"]),
                    ft.Text(info["name"], size=12, color="#475569"),
                ],
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _install_provider_badge(self) -> None:
        ft = self.ft
        self.provider_badge_text = ft.Text(
            "等待粘贴链接", size=13, weight=ft.FontWeight.BOLD, color="#94a3b8"
        )
        self.provider_badge_dot = ft.Container(
            width=10, height=10, border_radius=999, bgcolor="#cbd5e1"
        )
        self.provider_badge = ft.Container(
            bgcolor="#f1f5f9",
            border_radius=999,
            padding=ft.Padding(left=14, top=8, right=16, bottom=8),
            content=ft.Row(
                [self.provider_badge_dot, self.provider_badge_text],
                spacing=8,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        banner = ft.Container(
            bgcolor="#ffffff",
            border=ft.Border(bottom=ft.BorderSide(width=1, color="#e2e8f0")),
            padding=ft.Padding(left=26, top=10, right=26, bottom=10),
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.CLOUD_DOWNLOAD, color="#2563eb", size=20),
                    ft.Text("PanDownload", size=15, weight=ft.FontWeight.BOLD, color="#0f172a"),
                    ft.Text("三合一直链下载", size=12, color="#94a3b8"),
                    ft.Container(expand=True),
                    self._provider_chip("baidu"),
                    self._provider_chip("quark"),
                    self._provider_chip("aliyun"),
                    ft.Container(width=8),
                    self.provider_badge,
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        # 插入到根 Column（[header, body, footer]）的最顶部。
        try:
            root_column = self.page.controls[0]
            root_column.controls.insert(0, banner)
        except Exception:
            # 控件树异常时退化为不显示横幅，不影响主流程。
            pass

    def _wire_live_detection(self) -> None:
        previous = getattr(self.share_url_field, "on_change", None)

        def _handler(event):
            self._refresh_provider_badge(self.share_url_field.value or "")
            if callable(previous):
                previous(event)
            self.safe_update()

        self.share_url_field.on_change = _handler

    def _refresh_provider_badge(self, text: str) -> None:
        provider = detect_provider(text)
        name = provider_display_name(provider)
        if provider:
            info = PROVIDERS[provider]
            self.provider_badge_text.value = name
            self.provider_badge_text.color = info["accent"]
            self.provider_badge_dot.bgcolor = info["color"]
            self.provider_badge.bgcolor = "#eef2ff"
        else:
            self.provider_badge_text.value = "未识别" if text.strip() else "等待粘贴链接"
            self.provider_badge_text.color = "#94a3b8"
            self.provider_badge_dot.bgcolor = "#cbd5e1"
            self.provider_badge.bgcolor = "#f1f5f9"


def launch_pandownload_gui(config: ProviderGuiConfig) -> None:
    import flet as ft

    def target(page):
        PanDownloadFletApp(page, ft, config)

    if hasattr(ft, "run"):
        ft.run(target, view=ft.AppView.FLET_APP)
    else:
        ft.app(target=target)
