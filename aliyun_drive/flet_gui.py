import asyncio
import contextlib
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from common_pan.core import CloudFile, DownloadCancelled, DownloadConfig, download_file_with_resolver, format_size
from common_pan.tk_gui import GuiDownloadStopped, ProviderDownloadSession, ProviderGuiConfig, QueueTextWriter, clamp_int


FILE_SELECTED_MARK = "✓"
FILE_UNSELECTED_MARK = ""


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    return f"{format_size(int(bytes_per_second))}/s"


class CommonPanFletApp:
    def __init__(self, page, ft_module, config: ProviderGuiConfig):
        self.page = page
        self.ft = ft_module
        self.config = config
        self.ui_queue = queue.Queue()
        self.provider_session: Optional[ProviderDownloadSession] = None
        self.files_to_download: List[CloudFile] = []
        self.file_item_map: Dict[str, object] = {}
        self.file_selected_map: Dict[str, bool] = {}
        self.file_row_tag_map: Dict[str, str] = {}
        self.file_progress_bytes: Dict[str, int] = {}
        self.file_total_bytes: Dict[str, int] = {}
        self.file_speed_bytes: Dict[str, float] = {}
        self.file_row_controls: Dict[str, Dict[str, object]] = {}
        self.selection_buttons = []
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.active_task: Optional[str] = None
        self.download_paused = False
        self.download_stop_reason = ""
        self.closing = False
        self.queue_running = True
        self.queue_task = None
        self.busy = False
        self.last_progress_update_time = 0.0

        self.configure_page()
        self.build_ui()
        self.append_startup_credential_status()
        self.start_queue_pump()

    def configure_page(self) -> None:
        ft = self.ft
        self.page.title = self.config.title
        self.page.padding = 0
        self.page.bgcolor = "#f5f7fb"
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.scroll = ft.ScrollMode.HIDDEN
        self.page.on_resize = lambda event: self.update_responsive_layout(getattr(event, "height", None))
        try:
            self.page.window.width = 1380
            self.page.window.height = 780
            self.page.window.min_width = 1180
            self.page.window.min_height = 640
            self.page.window.maximized = True
            self.page.window.prevent_close = False
            self.page.window.on_event = self.on_window_event
        except Exception:
            pass

    def build_ui(self) -> None:
        ft = self.ft
        self.status_text = ft.Text("就绪", size=13, weight=ft.FontWeight.BOLD, color="#1d4ed8")
        self.summary_text = ft.Text("等待解析分享", size=13, color="#64748b", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.total_speed_text = ft.Text("总速度 -", size=13, weight=ft.FontWeight.BOLD, color="#334155")
        self.overall_progress = ft.ProgressBar(value=0, height=8, color="#2563eb", bgcolor="#e2e8f0")

        self.share_url_field = ft.TextField(
            label="分享文本",
            hint_text=self.config.share_hint,
            expand=True,
            border_radius=10,
            prefix_icon=ft.Icons.LINK,
            filled=True,
            bgcolor="#ffffff",
        )
        self.download_root_field = ft.TextField(
            label="保存位置",
            value=self.config.default_download_dir,
            expand=True,
            border_radius=10,
            prefix_icon=ft.Icons.FOLDER_OPEN,
            filled=True,
            bgcolor="#ffffff",
        )
        self.file_workers_field = ft.TextField(
            label="文件并发",
            value=str(self.config.default_file_workers),
            width=96,
            border_radius=10,
            text_align=ft.TextAlign.CENTER,
            filled=True,
            bgcolor="#ffffff",
        )
        self.retries_field = ft.TextField(
            label="重试次数",
            value=str(self.config.default_retries),
            width=96,
            border_radius=10,
            text_align=ft.TextAlign.CENTER,
            filled=True,
            bgcolor="#ffffff",
        )

        PrimaryButton = ft.Button if hasattr(ft, "Button") else ft.ElevatedButton
        self.parse_button = PrimaryButton("解析文件", icon=ft.Icons.SEARCH, on_click=lambda _e: self.parse_share())
        self.download_button = PrimaryButton("开始下载", icon=ft.Icons.DOWNLOAD, on_click=lambda _e: self.download(), disabled=True)
        self.pause_button = ft.OutlinedButton("暂停", icon=ft.Icons.PAUSE, on_click=lambda _e: self.pause_download(), disabled=True)
        self.resume_button = PrimaryButton("继续", icon=ft.Icons.PLAY_ARROW, on_click=lambda _e: self.resume_download(), disabled=True)
        self.reselect_button = ft.OutlinedButton("重新选择", icon=ft.Icons.RESTART_ALT, on_click=lambda _e: self.reselect_download(), disabled=True)
        self.select_all_button = ft.TextButton("全选", icon=ft.Icons.SELECT_ALL, on_click=lambda _e: self.select_all_files(), disabled=True)
        self.clear_selection_button = ft.TextButton("全不选", icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK, on_click=lambda _e: self.clear_file_selection(), disabled=True)
        self.invert_selection_button = ft.TextButton("反选", icon=ft.Icons.SWAP_HORIZ, on_click=lambda _e: self.invert_file_selection(), disabled=True)
        self.selection_buttons = [self.select_all_button, self.clear_selection_button, self.invert_selection_button]

        header = ft.Container(
            padding=ft.Padding(left=26, top=20, right=26, bottom=20),
            bgcolor="#ffffff",
            border=ft.Border(bottom=ft.BorderSide(width=1, color="#e2e8f0")),
            content=ft.Row(
                [
                    ft.Column(
                        [ft.Text(self.config.title, size=24, weight=ft.FontWeight.BOLD, color="#111827"), self.summary_text],
                        spacing=4,
                        expand=True,
                    ),
                    ft.Container(
                        content=self.status_text,
                        bgcolor="#dbeafe",
                        border_radius=999,
                        padding=ft.Padding(left=14, top=8, right=14, bottom=8),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        controls_panel = ft.Container(
            bgcolor="#ffffff",
            border_radius=14,
            padding=18,
            border=ft.Border(
                left=ft.BorderSide(width=1, color="#e2e8f0"),
                top=ft.BorderSide(width=1, color="#e2e8f0"),
                right=ft.BorderSide(width=1, color="#e2e8f0"),
                bottom=ft.BorderSide(width=1, color="#e2e8f0"),
            ),
            content=ft.Column(
                [
                    ft.Row([self.share_url_field], spacing=12),
                    ft.Row(
                        [
                            self.download_root_field,
                            ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="选择保存目录", on_click=lambda _e: self.choose_download_dir()),
                            self.file_workers_field,
                            self.retries_field,
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [self.parse_button, self.download_button, self.pause_button, self.resume_button, self.reselect_button],
                        spacing=10,
                        wrap=True,
                    ),
                    ft.Text("暂停或重新选择会停止当前连接；未完成的 .part 文件会保留，继续下载会自动续传。", size=12, color="#64748b"),
                ],
                spacing=12,
            ),
        )

        content_area_height, file_list_height, log_list_height = self.calculate_content_heights()
        self.file_list = ft.ListView(spacing=0, padding=0, auto_scroll=False, scroll=ft.ScrollMode.ALWAYS, height=file_list_height)
        file_list_viewport = ft.Container(content=self.file_list, height=file_list_height, clip_behavior=ft.ClipBehavior.HARD_EDGE)
        files_header = ft.Container(
            bgcolor="#f8fafc",
            border=ft.Border(bottom=ft.BorderSide(width=1, color="#e2e8f0")),
            padding=ft.Padding(left=12, top=10, right=12, bottom=10),
            content=ft.Row(
                [
                    ft.Text("", width=34),
                    ft.Text("路径", expand=True, size=12, weight=ft.FontWeight.BOLD, color="#475569"),
                    ft.Text("大小", width=95, size=12, weight=ft.FontWeight.BOLD, color="#475569", text_align=ft.TextAlign.RIGHT),
                    ft.Text("进度", width=190, size=12, weight=ft.FontWeight.BOLD, color="#475569", text_align=ft.TextAlign.CENTER),
                    ft.Text("速度", width=105, size=12, weight=ft.FontWeight.BOLD, color="#475569", text_align=ft.TextAlign.RIGHT),
                    ft.Text("状态", width=92, size=12, weight=ft.FontWeight.BOLD, color="#475569", text_align=ft.TextAlign.CENTER),
                ],
                spacing=12,
            ),
        )
        files_panel = ft.Container(
            expand=7,
            height=content_area_height,
            bgcolor="#ffffff",
            border_radius=14,
            border=ft.Border(
                left=ft.BorderSide(width=1, color="#e2e8f0"),
                top=ft.BorderSide(width=1, color="#e2e8f0"),
                right=ft.BorderSide(width=1, color="#e2e8f0"),
                bottom=ft.BorderSide(width=1, color="#e2e8f0"),
            ),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Column(
                [
                    ft.Container(
                        padding=ft.Padding(left=16, top=14, right=12, bottom=10),
                        content=ft.Row(
                            [ft.Text("文件列表", size=16, weight=ft.FontWeight.BOLD, color="#111827", expand=True), self.select_all_button, self.clear_selection_button, self.invert_selection_button],
                            spacing=4,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    files_header,
                    file_list_viewport,
                ],
                spacing=0,
                expand=True,
            ),
        )

        self.log_list = ft.ListView(spacing=6, padding=12, auto_scroll=True, scroll=ft.ScrollMode.ALWAYS, height=log_list_height)
        log_list_viewport = ft.Container(content=self.log_list, height=log_list_height, clip_behavior=ft.ClipBehavior.HARD_EDGE)
        log_panel = ft.Container(
            width=400,
            height=content_area_height,
            bgcolor="#ffffff",
            border_radius=14,
            border=ft.Border(
                left=ft.BorderSide(width=1, color="#e2e8f0"),
                top=ft.BorderSide(width=1, color="#e2e8f0"),
                right=ft.BorderSide(width=1, color="#e2e8f0"),
                bottom=ft.BorderSide(width=1, color="#e2e8f0"),
            ),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Column(
                [
                    ft.Container(
                        bgcolor="#f8fafc",
                        border=ft.Border(bottom=ft.BorderSide(width=1, color="#e2e8f0")),
                        padding=ft.Padding(left=16, top=14, right=16, bottom=14),
                        content=ft.Row([ft.Icon(ft.Icons.TERMINAL, color="#2563eb", size=18), ft.Text("任务日志", size=16, weight=ft.FontWeight.BOLD, color="#111827")], spacing=8),
                    ),
                    log_list_viewport,
                ],
                spacing=0,
                expand=True,
            ),
        )
        footer = ft.Container(
            bgcolor="#ffffff",
            border=ft.Border(top=ft.BorderSide(width=1, color="#e2e8f0")),
            padding=ft.Padding(left=26, top=12, right=26, bottom=14),
            content=ft.Row(
                [ft.Text("总进度", size=13, weight=ft.FontWeight.BOLD, color="#334155"), ft.Container(content=self.overall_progress, expand=True), self.total_speed_text],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        content_area = ft.Container(height=content_area_height, content=ft.Row([files_panel, log_panel], expand=True, spacing=14, vertical_alignment=ft.CrossAxisAlignment.START))
        self.content_area_container = content_area
        self.files_panel = files_panel
        self.log_panel = log_panel
        self.file_list_viewport = file_list_viewport
        self.log_list_viewport = log_list_viewport
        body = ft.Container(expand=True, padding=ft.Padding(left=22, top=18, right=22, bottom=14), content=ft.Column([controls_panel, content_area], expand=True, spacing=14))
        self.page.add(ft.Column([header, body, footer], expand=True, spacing=0))
        self.update_download_button_state()
        self.safe_update()

    def calculate_content_heights(self, page_height: Optional[float] = None) -> Tuple[int, int, int]:
        try:
            height = int(page_height or getattr(self.page, "height", 0) or getattr(self.page.window, "height", 780) or 780)
        except Exception:
            height = 780
        content_area_height = max(350, height - 410)
        file_list_height = max(300, content_area_height - 98)
        log_list_height = max(320, content_area_height - 56)
        return content_area_height, file_list_height, log_list_height

    def update_responsive_layout(self, page_height: Optional[float] = None) -> None:
        required_controls = ("content_area_container", "files_panel", "log_panel", "file_list_viewport", "log_list_viewport")
        if not all(hasattr(self, name) for name in required_controls):
            return
        content_area_height, file_list_height, log_list_height = self.calculate_content_heights(page_height)
        self.content_area_container.height = content_area_height
        self.files_panel.height = content_area_height
        self.log_panel.height = content_area_height
        self.file_list.height = file_list_height
        self.file_list_viewport.height = file_list_height
        self.log_list.height = log_list_height
        self.log_list_viewport.height = log_list_height
        self.safe_update()

    def start_queue_pump(self) -> None:
        if hasattr(self.page, "run_task"):
            self.queue_task = self.page.run_task(self.process_queue_loop_async)
        elif hasattr(self.page, "run_thread"):
            self.page.run_thread(self.process_queue_loop)
        else:
            threading.Thread(target=self.process_queue_loop, daemon=True).start()

    async def process_queue_loop_async(self) -> None:
        while self.queue_running:
            self.process_queue_batch()
            await asyncio.sleep(0.1)

    def process_queue_loop(self) -> None:
        while self.queue_running:
            self.process_queue_batch()
            time.sleep(0.1)

    def process_queue_batch(self) -> None:
        changed = False
        processed = 0
        deadline = time.monotonic() + 0.05
        while processed < 100 and time.monotonic() < deadline:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self.process_event(event)
            changed = True
            processed += 1
            if event[0] == "files":
                self.safe_update()
                changed = False
                break
            if event[0] in ("log", "status", "summary", "error"):
                self.safe_update()
                changed = False
        if changed:
            self.safe_update()

    def process_event(self, event) -> None:
        kind = event[0]
        if kind == "log":
            self.append_log(event[1])
        elif kind == "status":
            self.status_text.value = event[1]
        elif kind == "summary":
            self.summary_text.value = event[1]
        elif kind == "overall_progress":
            self.overall_progress.value = max(0.0, min(1.0, float(event[1]) / 100.0))
        elif kind == "file_progress":
            self.update_file_progress(event[1], event[2], event[3], event[4], event[5])
            now = time.monotonic()
            if now - self.last_progress_update_time >= 0.25:
                self.last_progress_update_time = now
                self.safe_update()
        elif kind == "file_status":
            self.set_file_status(event[1], event[2], event[3] if len(event) > 3 else 0.0)
        elif kind == "download_dir":
            self.download_root_field.value = event[1]
        elif kind == "files":
            self.show_files(event[1], event[2])
        elif kind == "download_paused":
            self.download_paused = True
            self.download_stop_reason = ""
            self.summary_text.value = "下载已暂停，可继续或重新选择"
            self.update_download_button_state()
        elif kind == "download_reselect":
            self.reset_download_selection()
        elif kind == "download_finished":
            self.download_paused = False
            self.download_stop_reason = ""
            self.cancel_event.clear()
            self.update_download_button_state()
        elif kind == "error":
            self.append_log(f"[错误] {event[1]}")
            self.summary_text.value = "任务失败"
            self.show_alert("任务失败", event[1])
        elif kind == "info":
            self.show_snack(event[1])
        elif kind == "busy":
            self.set_busy(event[1])

    def on_window_event(self, event) -> None:
        event_type = getattr(event, "type", None)
        close_type = getattr(getattr(self.ft, "WindowEventType", object), "CLOSE", None)
        if event_type == close_type or getattr(event_type, "value", None) == "close" or getattr(event, "data", "") == "close":
            self.on_close()

    def on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.queue_running = False
        self.cancel_event.set()
        if self.queue_task:
            try:
                self.queue_task.cancel()
            except Exception:
                pass

    def safe_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            try:
                if hasattr(self.page, "schedule_update"):
                    self.page.schedule_update()
            except Exception:
                pass

    def show_snack(self, message: str) -> None:
        ft = self.ft
        snack = ft.SnackBar(ft.Text(message), bgcolor="#0f172a")
        try:
            self.page.open(snack)
        except Exception:
            self.page.snack_bar = snack
            snack.open = True
            self.safe_update()

    def show_alert(self, title: str, message: str) -> None:
        ft = self.ft
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Text(message),
            actions=[ft.TextButton("确定", on_click=lambda _e: self.close_dialog(dialog))],
        )
        try:
            self.page.open(dialog)
        except Exception:
            self.page.dialog = dialog
            dialog.open = True
            self.safe_update()

    def close_dialog(self, dialog) -> None:
        dialog.open = False
        self.safe_update()

    def append_startup_credential_status(self) -> None:
        self.append_log(f"[配置] 凭据文件: {self.config.credentials_file}")
        for message in self.config.credential_messages():
            self.append_log(f"[配置] {message}")
        self.safe_update()

    def normalize_log_entry(self, text: str) -> Tuple[str, str]:
        message = str(text or "").strip()
        if not message:
            return "INFO", ""
        labels = {
            "错误": "ERROR",
            "失败": "ERROR",
            "警告": "WARN",
            "取消": "STOP",
            "暂停": "PAUSE",
            "重新选择": "PAUSE",
            "完成": "OK",
            "跳过": "OK",
            "下载": "DOWN",
            "续传": "DOWN",
            "合并": "DOWN",
            "提示": "INFO",
            "配置": "CONFIG",
            "列表": "INFO",
        }
        if message.startswith("[") and "]" in message:
            label, clean_message = message[1:].split("]", 1)
            return labels.get(label.strip(), "INFO"), clean_message.strip() or message
        return "INFO", message

    def log_level_style(self, level: str) -> Tuple[str, str]:
        styles = {
            "ERROR": ("#fee2e2", "#991b1b"),
            "WARN": ("#fef3c7", "#92400e"),
            "STOP": ("#fee2e2", "#991b1b"),
            "PAUSE": ("#fef3c7", "#92400e"),
            "OK": ("#dcfce7", "#166534"),
            "DOWN": ("#dbeafe", "#1d4ed8"),
            "CONFIG": ("#e2e8f0", "#334155"),
            "INFO": ("#e2e8f0", "#334155"),
        }
        return styles.get(level, styles["INFO"])

    def append_log(self, text: str) -> None:
        ft = self.ft
        level, message = self.normalize_log_entry(text)
        if not message:
            return
        badge_bg, badge_fg = self.log_level_style(level)
        timestamp = time.strftime("%H:%M:%S")
        self.log_list.controls.append(
            ft.Container(
                bgcolor="#ffffff",
                border_radius=8,
                border=ft.Border(
                    left=ft.BorderSide(width=1, color="#e2e8f0"),
                    top=ft.BorderSide(width=1, color="#e2e8f0"),
                    right=ft.BorderSide(width=1, color="#e2e8f0"),
                    bottom=ft.BorderSide(width=1, color="#e2e8f0"),
                ),
                padding=ft.Padding(left=10, top=7, right=10, bottom=7),
                content=ft.Row(
                    [
                        ft.Text(timestamp, width=62, size=11, color="#94a3b8", font_family="Consolas"),
                        ft.Container(width=60, bgcolor=badge_bg, border_radius=6, padding=ft.Padding(left=6, top=2, right=6, bottom=2), content=ft.Text(level, size=10, weight=ft.FontWeight.BOLD, color=badge_fg, text_align=ft.TextAlign.CENTER)),
                        ft.Text(message, expand=True, size=12, color="#334155", selectable=True),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            )
        )
        if len(self.log_list.controls) > 500:
            self.log_list.controls = self.log_list.controls[-500:]

    def choose_download_dir(self) -> None:
        def worker() -> None:
            try:
                import tkinter as tk
                from tkinter import filedialog

                initial_dir = self.download_root_field.value or self.config.default_download_dir
                if not os.path.isdir(initial_dir):
                    initial_dir = self.config.default_download_dir if os.path.isdir(self.config.default_download_dir) else os.path.expanduser("~")
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(parent=root, initialdir=initial_dir, title="选择下载保存目录")
                root.destroy()
                if selected:
                    self.ui_queue.put(("download_dir", selected))
            except Exception as exc:
                self.ui_queue.put(("error", f"选择保存目录失败: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def read_worker_settings(self) -> Tuple[int, int]:
        file_workers = clamp_int(self.file_workers_field.value, self.config.default_file_workers, 1, self.config.max_file_workers)
        retries = clamp_int(self.retries_field.value, self.config.default_retries, 1, self.config.max_retries)
        self.file_workers_field.value = str(file_workers)
        self.retries_field.value = str(retries)
        return file_workers, retries

    def get_selected_files(self) -> List[CloudFile]:
        return [file for file in self.files_to_download if self.file_selected_map.get(file.path)]

    def update_selection_summary(self) -> None:
        total_files = len(self.files_to_download)
        selected_files = self.get_selected_files()
        selected_size = sum(int(item.size or 0) for item in selected_files)
        total_size = sum(int(item.size or 0) for item in self.files_to_download)
        if total_files:
            self.summary_text.value = f"已选择 {len(selected_files)}/{total_files} 个文件，选中 {format_size(selected_size)} / 合计 {format_size(total_size)}"
        else:
            self.summary_text.value = "等待解析分享"

    def update_download_button_state(self) -> None:
        selected_count = len(self.get_selected_files())
        is_download_task = self.active_task == "download"
        can_select = bool(self.files_to_download) and not self.busy
        self.download_button.disabled = not (selected_count > 0 and not self.busy and not self.download_paused)
        self.pause_button.disabled = not (self.busy and is_download_task and not self.cancel_event.is_set())
        self.resume_button.disabled = not (selected_count > 0 and not self.busy and self.download_paused)
        self.reselect_button.disabled = not (bool(self.files_to_download) and (not self.busy or is_download_task))
        for button in self.selection_buttons:
            button.disabled = not can_select

    def status_colors(self, status: str) -> Tuple[str, str]:
        if status == "完成":
            return "#dcfce7", "#166534"
        if status in ("失败", "已停止"):
            return "#fee2e2", "#991b1b"
        if status in ("下载中", "取链中", "续传", "重试", "合并中"):
            return "#dbeafe", "#1d4ed8"
        if status == "已暂停":
            return "#fef3c7", "#92400e"
        return "#f1f5f9", "#475569"

    def update_file_row_visual(self, path: str, selected: bool) -> None:
        controls = self.file_row_controls.get(path)
        if not controls:
            return
        controls["container"].bgcolor = "#dbeafe" if selected else self.file_row_tag_map.get(path, "#ffffff")
        controls["mark"].value = FILE_SELECTED_MARK if selected else FILE_UNSELECTED_MARK
        controls["mark"].color = "#1d4ed8" if selected else "#94a3b8"

    def set_file_selected(self, path: str, selected: bool) -> None:
        if path not in self.file_item_map:
            return
        self.file_selected_map[path] = selected
        controls = self.file_row_controls.get(path)
        if controls and controls["status"].content.value in ("未选择", "等待", "已暂停", "已停止"):
            self.set_file_status(path, "等待" if selected else "未选择", 0.0)
        self.update_file_row_visual(path, selected)

    def toggle_file_selection(self, path: str) -> None:
        if self.busy:
            return
        self.set_file_selected(path, not self.file_selected_map.get(path, False))
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def select_all_files(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, True)
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def clear_file_selection(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, False)
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def invert_file_selection(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, not self.file_selected_map.get(file.path, False))
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def make_file_row(self, file: CloudFile, index: int):
        ft = self.ft
        path = file.path
        size = int(file.size or 0)
        row_bg = "#ffffff" if index % 2 == 0 else "#f8fafc"
        mark = ft.Text(FILE_UNSELECTED_MARK, width=34, size=18, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER)
        progress_bar = ft.ProgressBar(value=0, width=110, height=6, color="#2563eb", bgcolor="#e2e8f0")
        progress_text = ft.Text("0.0%", width=64, size=12, color="#475569", text_align=ft.TextAlign.RIGHT)
        status_bg, status_fg = self.status_colors("未选择")
        status = ft.Container(content=ft.Text("未选择", size=12, color=status_fg, text_align=ft.TextAlign.CENTER), bgcolor=status_bg, border_radius=999, padding=ft.Padding(left=10, top=4, right=10, bottom=4), width=92)
        row = ft.Container(
            bgcolor=row_bg,
            ink=True,
            on_click=lambda _e, p=path: self.toggle_file_selection(p),
            padding=ft.Padding(left=12, top=8, right=12, bottom=8),
            content=ft.Row(
                [
                    mark,
                    ft.Text(path, expand=True, size=13, color="#1f2937", no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(format_size(size), width=95, size=12, color="#475569", text_align=ft.TextAlign.RIGHT),
                    ft.Row([progress_bar, progress_text], width=190, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Text("-", width=105, size=12, color="#475569", text_align=ft.TextAlign.RIGHT),
                    status,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self.file_row_controls[path] = {"container": row, "mark": mark, "progress_bar": progress_bar, "progress_text": progress_text, "speed": row.content.controls[4], "status": status}
        self.file_row_tag_map[path] = row_bg
        return row

    def show_files(self, files: Sequence[CloudFile], folder_count: int) -> None:
        self.file_item_map = {}
        self.file_selected_map = {}
        self.file_row_tag_map = {}
        self.file_progress_bytes = {}
        self.file_total_bytes = {}
        self.file_speed_bytes = {}
        self.file_row_controls = {}
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        self.file_list.controls.clear()
        total_files = len(files)
        for index, file in enumerate(files):
            row = self.make_file_row(file, index)
            self.file_list.controls.append(row)
            self.file_item_map[file.path] = row
            self.file_selected_map[file.path] = False
            self.file_progress_bytes[file.path] = 0
            self.file_total_bytes[file.path] = int(file.size or 0)
            self.file_speed_bytes[file.path] = 0.0
            if (index + 1) % 100 == 0:
                self.summary_text.value = f"正在显示文件列表 {index + 1}/{total_files}"
                self.safe_update()
        self.update_selection_summary()
        self.total_speed_text.value = "总速度 -"
        self.overall_progress.value = 0
        self.status_text.value = "已解析"
        folder_text = f"{folder_count} 个文件夹" if folder_count >= 0 else "文件夹已递归展开"
        self.append_log(f"[列表] 共发现 {len(files)} 个文件，{folder_text}。")
        self.update_download_button_state()

    def set_file_status(self, path: str, status: str, speed: float = 0.0) -> None:
        controls = self.file_row_controls.get(path)
        if not controls:
            return
        bg, fg = self.status_colors(status)
        controls["speed"].value = format_speed(speed)
        controls["status"].bgcolor = bg
        controls["status"].content.value = status
        controls["status"].content.color = fg
        self.file_speed_bytes[path] = max(0.0, float(speed or 0.0))

    def update_file_progress(self, path: str, current: int, total: int, speed: float, status: str) -> None:
        controls = self.file_row_controls.get(path)
        if not controls:
            return
        current = max(0, int(current or 0))
        total = max(int(total or 0), current)
        self.file_progress_bytes[path] = current
        self.file_total_bytes[path] = total
        self.file_speed_bytes[path] = max(0.0, float(speed or 0.0))
        percent = 0.0 if total <= 0 else max(0.0, min(100.0, current * 100.0 / total))
        controls["progress_bar"].value = percent / 100.0
        controls["progress_text"].value = f"{percent:.1f}%"
        self.set_file_status(path, status, speed)
        tracked_paths = [item for item in self.file_total_bytes if self.file_selected_map.get(item)] or list(self.file_total_bytes)
        total_bytes = sum(max(1, self.file_total_bytes.get(item, 0)) for item in tracked_paths)
        done_bytes = sum(min(self.file_progress_bytes.get(item, 0), self.file_total_bytes.get(item, 0)) for item in tracked_paths)
        self.overall_progress.value = 1.0 if total_bytes <= 0 else max(0.0, min(1.0, done_bytes / total_bytes))
        total_speed = sum(self.file_speed_bytes.get(item, 0.0) for item in tracked_paths)
        self.total_speed_text.value = f"总速度 {format_speed(total_speed)}"

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise GuiDownloadStopped("下载已停止")

    def pause_download(self) -> None:
        if not self.busy or self.active_task != "download" or self.cancel_event.is_set():
            return
        self.download_stop_reason = "pause"
        self.cancel_event.set()
        self.summary_text.value = "正在暂停下载"
        self.append_log("[暂停] 正在停止当前连接，已下载的 .part 文件会保留。")
        self.update_download_button_state()
        self.safe_update()

    def resume_download(self) -> None:
        if self.busy or not self.download_paused:
            return
        self.download()

    def reset_download_selection(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, False)
            controls = self.file_row_controls.get(file.path)
            if controls and controls["status"].content.value != "完成":
                self.set_file_status(file.path, "未选择", 0.0)
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        self.update_selection_summary()
        self.summary_text.value = "请重新选择需要下载的文件"
        self.update_download_button_state()
        self.safe_update()

    def reselect_download(self) -> None:
        if not self.files_to_download:
            return
        if self.busy:
            if self.active_task != "download":
                return
            self.download_stop_reason = "reselect"
            if not self.cancel_event.is_set():
                self.cancel_event.set()
                self.append_log("[重新选择] 正在停止当前下载，已下载的 .part 文件会保留。")
            self.summary_text.value = "正在停止下载，稍后可重新选择"
            self.update_download_button_state()
            self.safe_update()
            return
        self.reset_download_selection()

    def set_busy(self, busy: bool, task_name: Optional[str] = None) -> None:
        self.busy = busy
        self.active_task = task_name if busy else None
        self.parse_button.disabled = busy
        self.share_url_field.disabled = busy
        self.download_root_field.disabled = busy
        self.file_workers_field.disabled = busy
        self.retries_field.disabled = busy
        self.update_download_button_state()
        self.safe_update()

    def run_worker(self, target: Callable[[], None], task_name: str = "general") -> None:
        if self.busy:
            return
        self.set_busy(True, task_name)

        def wrapper() -> None:
            writer = QueueTextWriter(self.ui_queue)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    target()
            except (GuiDownloadStopped, DownloadCancelled) as exc:
                if not self.closing:
                    self.ui_queue.put(("summary", str(exc)))
            except Exception as exc:
                if not self.closing:
                    self.ui_queue.put(("error", str(exc)))
            finally:
                writer.flush()
                if not self.closing:
                    self.ui_queue.put(("busy", False))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def parse_share(self) -> None:
        share_text = (self.share_url_field.value or "").strip()
        if not share_text:
            self.show_snack(f"请先粘贴{self.config.provider_name}分享链接或完整分享文本。")
            return

        def worker() -> None:
            self.ui_queue.put(("status", "解析中"))
            self.ui_queue.put(("summary", f"正在解析{self.config.provider_name}分享"))
            print("正在解析分享内容...")
            session = self.config.create_session(share_text)
            files = [file for file in session.files if not file.is_folder]
            self.provider_session = session
            self.files_to_download = files
            if not files:
                self.ui_queue.put(("summary", "未发现可下载文件"))
                print("没有可下载的文件。")
                return
            folder_text = f"{session.folder_count} 个文件夹" if session.folder_count >= 0 else "文件夹已递归展开"
            print(f"共发现 {len(files)} 个文件，{folder_text}")
            self.ui_queue.put(("files", files, session.folder_count))

        self.run_worker(worker, "parse")

    def download(self) -> None:
        if not self.files_to_download or not self.provider_session:
            self.show_snack("请先解析分享文件列表。")
            return
        selected_files = self.get_selected_files()
        if not selected_files:
            self.show_snack("请先在文件列表中选择需要下载的文件。")
            return
        download_root = (self.download_root_field.value or "").strip() or self.config.default_download_dir
        file_workers, retries = self.read_worker_settings()
        session = self.provider_session
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        for file in selected_files:
            self.set_file_status(file.path, "等待", 0.0)
        self.safe_update()

        def worker() -> None:
            completed_paths = set()
            resolve_lock = threading.Lock()
            download_failed_count = 0
            download_ok_count = 0

            def make_progress_callback(path: str):
                def callback(current: int, total: int, speed: float, status: str) -> None:
                    self.ui_queue.put(("file_progress", path, current, total, speed, status))

                return callback

            def run_file(file: CloudFile) -> None:
                self.raise_if_cancelled()
                self.ui_queue.put(("file_status", file.path, "取链中", 0.0))

                def resolve() -> str:
                    self.raise_if_cancelled()
                    if session.serialize_resolve:
                        with resolve_lock:
                            self.raise_if_cancelled()
                            return session.resolve_url(file)
                    return session.resolve_url(file)

                download_config = DownloadConfig(
                    output_dir=download_root,
                    user_agent=session.user_agent,
                    referer=session.referer,
                    extra_headers=session.extra_headers,
                    retries=max(1, retries),
                    overwrite=False,
                    progress_callback=make_progress_callback(file.path),
                    cancel_event=self.cancel_event,
                    show_progress=False,
                )
                self.ui_queue.put(("file_status", file.path, "下载中", 0.0))
                download_file_with_resolver(file, resolve, download_config)

            try:
                os.makedirs(download_root, exist_ok=True)
                self.ui_queue.put(("status", "下载中"))
                self.ui_queue.put(("summary", f"正在下载 {len(selected_files)} 个文件，文件并发 {file_workers}，重试 {retries} 次"))
                self.ui_queue.put(("overall_progress", 0))
                with ThreadPoolExecutor(max_workers=file_workers) as executor:
                    future_map = {}
                    for file in selected_files:
                        self.raise_if_cancelled()
                        future_map[executor.submit(run_file, file)] = file
                    for future in as_completed(future_map):
                        file = future_map[future]
                        try:
                            future.result()
                            download_ok_count += 1
                            completed_paths.add(file.path)
                            done_total = max(0, int(file.size or self.file_total_bytes.get(file.path, 0)))
                            self.ui_queue.put(("file_progress", file.path, done_total, done_total, 0.0, "完成"))
                        except (GuiDownloadStopped, DownloadCancelled):
                            raise
                        except Exception as exc:
                            if self.cancel_event.is_set():
                                raise GuiDownloadStopped("下载已停止")
                            print(f"  [失败] 下载 {file.path}: {exc}")
                            download_failed_count += 1
                            self.ui_queue.put(("file_status", file.path, "失败", 0.0))
                self.raise_if_cancelled()
                message = f"所有任务处理完毕，成功 {download_ok_count} 个，失败 {download_failed_count} 个，文件保存至: {download_root}"
                print(f"\n{message}")
                self.ui_queue.put(("status", "已完成"))
                self.ui_queue.put(("summary", f"成功 {download_ok_count} 个，失败 {download_failed_count} 个"))
                self.ui_queue.put(("download_finished",))
                self.ui_queue.put(("info", message))
            except (GuiDownloadStopped, DownloadCancelled):
                reason = self.download_stop_reason
                if reason == "pause":
                    print("\n[暂停] 下载已暂停，未完成的 .part 文件已保留，点击继续可续传。")
                    for file in selected_files:
                        if file.path not in completed_paths:
                            self.ui_queue.put(("file_status", file.path, "已暂停", 0.0))
                    if not self.closing:
                        self.ui_queue.put(("download_paused",))
                    return
                if reason == "reselect":
                    print("\n[重新选择] 下载已停止，未完成的 .part 文件已保留，可重新选择文件后继续。")
                    for file in selected_files:
                        if file.path not in completed_paths:
                            self.ui_queue.put(("file_status", file.path, "未选择", 0.0))
                    if not self.closing:
                        self.ui_queue.put(("download_reselect",))
                    return
                print("\n[取消] 下载任务已取消，未完成的 .part 文件已保留。")
                for file in selected_files:
                    if file.path not in completed_paths:
                        self.ui_queue.put(("file_status", file.path, "已停止", 0.0))
                if not self.closing:
                    self.ui_queue.put(("status", "已停止"))
                    self.ui_queue.put(("summary", "下载已取消"))
                    self.ui_queue.put(("download_finished",))

        self.run_worker(worker, "download")


def launch_flet_gui(config: ProviderGuiConfig) -> None:
    import flet as ft

    def target(page):
        CommonPanFletApp(page, ft, config)

    if hasattr(ft, "run"):
        ft.run(target, view=ft.AppView.FLET_APP)
    else:
        ft.app(target=target)
