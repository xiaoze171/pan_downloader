import contextlib
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .core import CloudFile, DownloadCancelled, DownloadConfig, download_file_with_resolver, format_size


FILE_SELECTED_MARK = "✓"
FILE_UNSELECTED_MARK = ""


@dataclass
class ProviderDownloadSession:
    files: Sequence[CloudFile]
    resolve_url: Callable[[CloudFile], str]
    user_agent: str
    referer: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)
    folder_count: int = -1
    serialize_resolve: bool = True


@dataclass
class ProviderGuiConfig:
    title: str
    provider_name: str
    default_download_dir: str
    credentials_file: str
    create_session: Callable[[str], ProviderDownloadSession]
    credential_messages: Callable[[], Sequence[str]]
    share_hint: str = "粘贴分享链接或完整分享文本"
    default_file_workers: int = 2
    max_file_workers: int = 4
    default_retries: int = 5
    max_retries: int = 10


class GuiDownloadStopped(RuntimeError):
    pass


def clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    return f"{format_size(int(bytes_per_second))}/s"


def format_progress_text(current: int, total: int) -> str:
    if total <= 0:
        return "[----------] 0.0%"
    percent = max(0.0, min(100.0, current * 100.0 / total))
    filled = int(round(percent / 10))
    bar = "#" * filled + "-" * (10 - filled)
    return f"[{bar}] {percent:5.1f}%"


class QueueTextWriter:
    def __init__(self, output_queue: queue.Queue):
        self.output_queue = output_queue
        self.buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self.buffer += text.replace("\r", "\n")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.output_queue.put(("log", line))
        return len(text)

    def flush(self) -> None:
        if self.buffer.strip():
            self.output_queue.put(("log", self.buffer.strip()))
        self.buffer = ""


class CommonPanTkApp:
    def __init__(self, config: ProviderGuiConfig):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.config = config
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.ui_queue = queue.Queue()
        self.provider_session: Optional[ProviderDownloadSession] = None
        self.files_to_download: List[CloudFile] = []
        self.file_item_map: Dict[str, str] = {}
        self.file_selected_map: Dict[str, bool] = {}
        self.file_row_tag_map: Dict[str, str] = {}
        self.file_progress_bytes: Dict[str, int] = {}
        self.file_total_bytes: Dict[str, int] = {}
        self.file_speed_bytes: Dict[str, float] = {}
        self.selection_buttons = []
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.active_task: Optional[str] = None
        self.download_paused = False
        self.download_stop_reason = ""
        self.closing = False
        self.force_exit_timer: Optional[threading.Timer] = None
        self.busy = False

        self.root = tk.Tk()
        self.root.title(config.title)
        self.root.geometry("1380x780")
        self.root.minsize(1180, 640)
        try:
            self.root.state("zoomed")
        except Exception:
            pass
        self.root.configure(bg="#f4f6f8")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.share_text_var = tk.StringVar()
        self.download_root_var = tk.StringVar(value=config.default_download_dir)
        self.file_workers_var = tk.StringVar(value=str(config.default_file_workers))
        self.retries_var = tk.StringVar(value=str(config.default_retries))
        self.status_var = tk.StringVar(value="就绪")
        self.summary_var = tk.StringVar(value="等待解析分享")
        self.total_speed_var = tk.StringVar(value="总速度 -")
        self.progress_var = tk.DoubleVar(value=0)

        self.configure_style()
        self.build_ui()
        self.append_startup_credential_status()
        self.share_entry.focus_set()
        self.root.after(100, self.process_queue)

    def configure_style(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("App.TFrame", background="#f4f6f8")
        style.configure("Panel.TFrame", background="#ffffff", borderwidth=0, relief="flat")
        style.configure("Header.TLabel", background="#f4f6f8", foreground="#111827", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Subtle.TLabel", background="#f4f6f8", foreground="#64748b", font=("Microsoft YaHei UI", 10))
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#111827", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Body.TLabel", background="#ffffff", foreground="#334155", font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft YaHei UI", 9))
        style.configure("Footer.TLabel", background="#f4f6f8", foreground="#334155", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", background="#dbeafe", foreground="#1d4ed8", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 6))
        style.configure("TEntry", padding=8, fieldbackground="#ffffff", bordercolor="#cbd5e1", lightcolor="#cbd5e1", darkcolor="#cbd5e1")
        style.configure("TSpinbox", padding=6, fieldbackground="#ffffff", bordercolor="#cbd5e1", lightcolor="#cbd5e1", darkcolor="#cbd5e1")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 9), background="#2563eb", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#bfdbfe")], foreground=[("disabled", "#f8fafc")])
        style.configure("Ghost.TButton", font=("Microsoft YaHei UI", 10), padding=(16, 9), background="#eef2f7", foreground="#334155")
        style.map("Ghost.TButton", background=[("active", "#e2e8f0"), ("disabled", "#f8fafc")], foreground=[("disabled", "#94a3b8")])
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=36, background="#ffffff", fieldbackground="#ffffff", borderwidth=0)
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#1e3a8a")])
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), background="#f1f5f9", foreground="#334155", padding=(8, 8))
        style.configure("Horizontal.TProgressbar", troughcolor="#e2e8f0", background="#2563eb", bordercolor="#e2e8f0", lightcolor="#2563eb", darkcolor="#2563eb")

    def build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        outer = ttk.Frame(self.root, style="App.TFrame", padding=(22, 20, 22, 18))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=self.config.title, style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.summary_var, style="Subtle.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=2, rowspan=2, sticky="e")

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=(18, 16))
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="分享文本", style="Body.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        self.share_entry = ttk.Entry(panel, textvariable=self.share_text_var)
        self.share_entry.grid(row=0, column=1, columnspan=6, sticky="ew", pady=(0, 12))

        ttk.Label(panel, text="保存位置", style="Body.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        self.download_root_entry = ttk.Entry(panel, textvariable=self.download_root_var)
        self.download_root_entry.grid(row=1, column=1, columnspan=5, sticky="ew", pady=(0, 12))
        self.choose_button = ttk.Button(panel, text="浏览", style="Ghost.TButton", command=self.choose_download_dir)
        self.choose_button.grid(row=1, column=6, sticky="e", padx=(10, 0), pady=(0, 12))

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=2, column=0, columnspan=7, sticky="ew")
        actions.columnconfigure(10, weight=1)
        ttk.Label(actions, text="文件并发", style="Body.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.file_workers_spinbox = ttk.Spinbox(
            actions,
            from_=1,
            to=self.config.max_file_workers,
            width=6,
            textvariable=self.file_workers_var,
        )
        self.file_workers_spinbox.grid(row=0, column=1, sticky="w", padx=(0, 18))
        ttk.Label(actions, text="重试次数", style="Body.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.retries_spinbox = ttk.Spinbox(
            actions,
            from_=1,
            to=self.config.max_retries,
            width=6,
            textvariable=self.retries_var,
        )
        self.retries_spinbox.grid(row=0, column=3, sticky="w", padx=(0, 22))
        self.parse_button = ttk.Button(actions, text="解析文件", style="Primary.TButton", command=self.parse_share)
        self.parse_button.grid(row=0, column=4, padx=(0, 10))
        self.download_button = ttk.Button(actions, text="开始下载", style="Primary.TButton", command=self.download, state="disabled")
        self.download_button.grid(row=0, column=5, padx=(0, 10))
        self.pause_button = ttk.Button(actions, text="暂停", style="Ghost.TButton", command=self.pause_download, state="disabled")
        self.pause_button.grid(row=0, column=6, padx=(0, 10))
        self.resume_button = ttk.Button(actions, text="继续", style="Primary.TButton", command=self.resume_download, state="disabled")
        self.resume_button.grid(row=0, column=7, padx=(0, 10))
        self.reselect_button = ttk.Button(actions, text="重新选择", style="Ghost.TButton", command=self.reselect_download, state="disabled")
        self.reselect_button.grid(row=0, column=8, padx=(0, 10))
        ttk.Label(
            panel,
            text="提示：暂停或重新选择会停止当前连接；未完成的 .part 文件会保留，继续下载会自动续传。",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=7, sticky="w", pady=(12, 0))

        content = ttk.Frame(outer, style="App.TFrame")
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=7)
        content.columnconfigure(1, weight=3)
        content.rowconfigure(0, weight=1)

        files_panel = ttk.Frame(content, style="Panel.TFrame", padding=(14, 14))
        files_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        files_panel.rowconfigure(1, weight=1)
        files_panel.columnconfigure(0, weight=1)
        files_header = ttk.Frame(files_panel, style="Panel.TFrame")
        files_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        files_header.columnconfigure(0, weight=1)
        ttk.Label(files_header, text="文件列表", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        select_all_button = ttk.Button(files_header, text="全选", style="Ghost.TButton", command=self.select_all_files)
        clear_selection_button = ttk.Button(files_header, text="全不选", style="Ghost.TButton", command=self.clear_file_selection)
        invert_selection_button = ttk.Button(files_header, text="反选", style="Ghost.TButton", command=self.invert_file_selection)
        select_all_button.grid(row=0, column=1, padx=(8, 0))
        clear_selection_button.grid(row=0, column=2, padx=(8, 0))
        invert_selection_button.grid(row=0, column=3, padx=(8, 0))
        self.selection_buttons = [select_all_button, clear_selection_button, invert_selection_button]

        tree_frame = ttk.Frame(files_panel, style="Panel.TFrame")
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.file_tree = ttk.Treeview(
            tree_frame,
            columns=("selected", "path", "size", "progress", "speed", "status"),
            show="headings",
            selectmode="browse",
        )
        self.file_tree.heading("selected", text="选择")
        self.file_tree.heading("path", text="路径")
        self.file_tree.heading("size", text="大小")
        self.file_tree.heading("progress", text="进度")
        self.file_tree.heading("speed", text="速度")
        self.file_tree.heading("status", text="状态")
        self.file_tree.column("selected", width=42, anchor="center", stretch=False)
        self.file_tree.column("path", width=380, anchor="w", stretch=True)
        self.file_tree.column("size", width=95, anchor="e", stretch=False)
        self.file_tree.column("progress", width=190, anchor="center", stretch=False)
        self.file_tree.column("speed", width=105, anchor="e", stretch=False)
        self.file_tree.column("status", width=86, anchor="center", stretch=False)
        self.file_tree.tag_configure("odd", background="#f8fafc", foreground="#334155")
        self.file_tree.tag_configure("even", background="#ffffff", foreground="#334155")
        self.file_tree.tag_configure("selected", background="#dbeafe", foreground="#1e3a8a")
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        self.file_tree.bind("<ButtonRelease-1>", self.on_file_tree_click)
        self.file_tree.bind("<space>", self.on_file_tree_keyboard_toggle)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.file_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        xscrollbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.file_tree.xview)
        xscrollbar.grid(row=1, column=0, sticky="ew")
        self.file_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=xscrollbar.set)

        log_panel = ttk.Frame(content, style="Panel.TFrame", padding=14)
        log_panel.grid(row=0, column=1, sticky="nsew")
        log_panel.rowconfigure(1, weight=1)
        log_panel.columnconfigure(0, weight=1)
        ttk.Label(log_panel, text="任务日志", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.log_text = tk.Text(
            log_panel,
            height=12,
            wrap="word",
            bg="#ffffff",
            fg="#334155",
            insertbackground="#334155",
            relief="flat",
            padx=12,
            pady=10,
            font=("Consolas", 9),
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        footer.columnconfigure(1, weight=1)
        ttk.Label(footer, text="总进度", style="Footer.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.progress = ttk.Progressbar(footer, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew")
        ttk.Label(footer, textvariable=self.total_speed_var, style="Footer.TLabel").grid(row=0, column=2, sticky="e", padx=(12, 0))

    def run(self) -> None:
        self.root.mainloop()

    def append_startup_credential_status(self) -> None:
        self.append_log(f"[配置] 凭据文件: {self.config.credentials_file}")
        for message in self.config.credential_messages():
            self.append_log(f"[配置] {message}")

    def force_exit_after_close(self) -> None:
        if self.worker and self.worker.is_alive():
            os._exit(0)

    def on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.cancel_event.set()
        try:
            self.status_var.set("正在停止")
            self.summary_var.set("正在取消任务并退出")
            if self.busy:
                self.append_log("[取消] 正在停止下载任务，未完成的 .part 文件会保留。")
        except Exception:
            pass

        if self.worker and self.worker.is_alive():
            self.force_exit_timer = threading.Timer(8, self.force_exit_after_close)
            self.force_exit_timer.daemon = True
            self.force_exit_timer.start()

        try:
            self.root.destroy()
        except Exception:
            pass

    def choose_download_dir(self) -> None:
        selected = self.filedialog.askdirectory(initialdir=self.download_root_var.get() or self.config.default_download_dir)
        if selected:
            self.download_root_var.set(selected)

    def read_worker_settings(self) -> tuple:
        file_workers = clamp_int(self.file_workers_var.get(), self.config.default_file_workers, 1, self.config.max_file_workers)
        retries = clamp_int(self.retries_var.get(), self.config.default_retries, 1, self.config.max_retries)
        self.file_workers_var.set(str(file_workers))
        self.retries_var.set(str(retries))
        return file_workers, retries

    def get_selected_files(self) -> List[CloudFile]:
        return [file for file in self.files_to_download if self.file_selected_map.get(file.path)]

    def update_selection_summary(self) -> None:
        total_files = len(self.files_to_download)
        selected_files = self.get_selected_files()
        selected_size = sum(int(item.size or 0) for item in selected_files)
        total_size = sum(int(item.size or 0) for item in self.files_to_download)
        if total_files:
            self.summary_var.set(
                f"已选择 {len(selected_files)}/{total_files} 个文件，选中 {format_size(selected_size)} / 合计 {format_size(total_size)}"
            )
        else:
            self.summary_var.set("等待解析分享")

    def update_download_button_state(self) -> None:
        selected_count = len(self.get_selected_files())
        is_download_task = self.active_task == "download"
        can_select = bool(self.files_to_download) and not self.busy
        can_start = selected_count > 0 and not self.busy and not self.download_paused
        can_pause = self.busy and is_download_task and not self.cancel_event.is_set()
        can_resume = selected_count > 0 and not self.busy and self.download_paused
        can_reselect = bool(self.files_to_download) and (not self.busy or is_download_task)

        self.download_button.configure(state="normal" if can_start else "disabled")
        self.pause_button.configure(state="normal" if can_pause else "disabled")
        self.resume_button.configure(state="normal" if can_resume else "disabled")
        self.reselect_button.configure(state="normal" if can_reselect else "disabled")

        selection_state = "normal" if can_select else "disabled"
        for button in self.selection_buttons:
            button.configure(state=selection_state)

    def update_file_row_visual(self, path: str, selected: bool) -> None:
        item_id = self.file_item_map.get(path)
        if not item_id:
            return
        row_tags = ("selected",) if selected else (self.file_row_tag_map.get(path, "even"),)
        self.file_tree.item(item_id, tags=row_tags)

    def set_file_selected(self, path: str, selected: bool) -> None:
        if path not in self.file_item_map:
            return
        self.file_selected_map[path] = selected
        item_id = self.file_item_map[path]
        values = list(self.file_tree.item(item_id, "values"))
        if len(values) >= 1:
            values[0] = FILE_SELECTED_MARK if selected else FILE_UNSELECTED_MARK
            if len(values) >= 6 and values[5] in ("未选择", "等待", "已暂停", "已停止"):
                values[5] = "等待" if selected else "未选择"
            self.file_tree.item(item_id, values=values)
        self.update_file_row_visual(path, selected)

    def toggle_file_selection(self, path: str) -> None:
        self.set_file_selected(path, not self.file_selected_map.get(path, False))
        self.update_selection_summary()
        self.update_download_button_state()

    def select_all_files(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, True)
        self.update_selection_summary()
        self.update_download_button_state()

    def clear_file_selection(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, False)
        self.update_selection_summary()
        self.update_download_button_state()

    def invert_file_selection(self) -> None:
        for file in self.files_to_download:
            self.set_file_selected(file.path, not self.file_selected_map.get(file.path, False))
        self.update_selection_summary()
        self.update_download_button_state()

    def on_file_tree_click(self, event) -> None:
        if self.busy:
            return
        item_id = self.file_tree.identify_row(event.y)
        if not item_id:
            return
        values = self.file_tree.item(item_id, "values")
        if len(values) >= 2:
            self.toggle_file_selection(str(values[1]))

    def on_file_tree_keyboard_toggle(self, _event) -> str:
        if self.busy:
            return "break"
        focus_item = self.file_tree.focus()
        if not focus_item:
            return "break"
        values = self.file_tree.item(focus_item, "values")
        if len(values) >= 2:
            self.toggle_file_selection(str(values[1]))
        return "break"

    def pause_download(self) -> None:
        if not self.busy or self.active_task != "download" or self.cancel_event.is_set():
            return
        self.download_stop_reason = "pause"
        self.cancel_event.set()
        self.summary_var.set("正在暂停下载")
        self.append_log("[暂停] 正在停止当前连接，已下载的 .part 文件会保留。")
        self.update_download_button_state()

    def resume_download(self) -> None:
        if self.busy or not self.download_paused:
            return
        self.download()

    def reset_download_selection(self) -> None:
        for file in self.files_to_download:
            path = file.path
            self.set_file_selected(path, False)
            item_id = self.file_item_map.get(path)
            if not item_id:
                continue
            values = list(self.file_tree.item(item_id, "values"))
            if len(values) >= 6 and values[5] != "完成":
                values[4] = "-"
                values[5] = "未选择"
                self.file_speed_bytes[path] = 0.0
                self.file_tree.item(item_id, values=values)

        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        self.update_selection_summary()
        self.summary_var.set("请重新选择需要下载的文件")
        self.update_download_button_state()

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
            self.summary_var.set("正在停止下载，稍后可重新选择")
            self.update_download_button_state()
            return

        self.reset_download_selection()

    def set_busy(self, busy: bool, task_name: Optional[str] = None) -> None:
        self.busy = busy
        self.active_task = task_name if busy else None
        state = "disabled" if busy else "normal"
        for widget in (
            self.parse_button,
            self.share_entry,
            self.download_root_entry,
            self.choose_button,
            self.file_workers_spinbox,
            self.retries_spinbox,
        ):
            widget.configure(state=state)
        self.update_download_button_state()

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

    def process_queue(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            kind = event[0]
            if kind == "log":
                self.append_log(event[1])
            elif kind == "status":
                self.status_var.set(event[1])
            elif kind == "summary":
                self.summary_var.set(event[1])
            elif kind == "overall_progress":
                self.progress.configure(maximum=100)
                self.progress_var.set(event[1])
            elif kind == "file_progress":
                self.update_file_progress(event[1], event[2], event[3], event[4], event[5])
            elif kind == "file_status":
                self.set_file_status(event[1], event[2], event[3] if len(event) > 3 else 0.0)
            elif kind == "files":
                self.show_files(event[1], event[2])
            elif kind == "download_paused":
                self.download_paused = True
                self.download_stop_reason = ""
                self.summary_var.set("下载已暂停，可继续或重新选择")
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
                self.summary_var.set("任务失败")
                self.messagebox.showerror("任务失败", event[1])
            elif kind == "info":
                self.messagebox.showinfo("完成", event[1])
            elif kind == "busy":
                self.set_busy(event[1])

        self.root.after(100, self.process_queue)

    def append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def show_files(self, files: Sequence[CloudFile], folder_count: int) -> None:
        self.file_item_map = {}
        self.file_selected_map = {}
        self.file_row_tag_map = {}
        self.file_progress_bytes = {}
        self.file_total_bytes = {}
        self.file_speed_bytes = {}
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        for index, file in enumerate(files):
            path = file.path
            size = int(file.size or 0)
            row_tag = "even" if index % 2 == 0 else "odd"
            item_id = self.file_tree.insert(
                "",
                "end",
                values=(FILE_UNSELECTED_MARK, path, format_size(size), format_progress_text(0, size), "-", "未选择"),
                tags=(row_tag,),
            )
            self.file_item_map[path] = item_id
            self.file_selected_map[path] = False
            self.file_row_tag_map[path] = row_tag
            self.file_progress_bytes[path] = 0
            self.file_total_bytes[path] = size
            self.file_speed_bytes[path] = 0.0
        self.update_selection_summary()
        self.total_speed_var.set("总速度 -")
        self.progress.configure(maximum=100)
        self.progress_var.set(0)
        self.status_var.set("已解析")
        folder_text = f"{folder_count} 个文件夹" if folder_count >= 0 else "文件夹已递归展开"
        self.append_log(f"[列表] 共发现 {len(files)} 个文件，{folder_text}。")
        self.update_download_button_state()

    def set_file_status(self, path: str, status: str, speed: float = 0.0) -> None:
        item_id = self.file_item_map.get(path)
        if not item_id:
            return
        values = list(self.file_tree.item(item_id, "values"))
        if len(values) < 6:
            return
        values[4] = format_speed(speed)
        values[5] = status
        self.file_speed_bytes[path] = max(0.0, float(speed or 0.0))
        self.file_tree.item(item_id, values=values)

    def update_file_progress(self, path: str, current: int, total: int, speed: float, status: str) -> None:
        item_id = self.file_item_map.get(path)
        if not item_id:
            return

        current = max(0, int(current or 0))
        total = max(int(total or 0), current)
        self.file_progress_bytes[path] = current
        self.file_total_bytes[path] = total
        self.file_speed_bytes[path] = max(0.0, float(speed or 0.0))

        values = list(self.file_tree.item(item_id, "values"))
        if len(values) < 6:
            values = [FILE_SELECTED_MARK if self.file_selected_map.get(path) else FILE_UNSELECTED_MARK, path, format_size(total), "", "", ""]
        values[2] = format_size(total)
        values[3] = format_progress_text(current, total)
        values[4] = format_speed(speed)
        values[5] = status
        self.file_tree.item(item_id, values=values)

        tracked_paths = [path for path in self.file_total_bytes if self.file_selected_map.get(path)]
        if not tracked_paths:
            tracked_paths = list(self.file_total_bytes)
        total_bytes = sum(max(1, self.file_total_bytes.get(path, 0)) for path in tracked_paths)
        done_bytes = sum(min(self.file_progress_bytes.get(path, 0), self.file_total_bytes.get(path, 0)) for path in tracked_paths)
        overall_percent = 100.0 if total_bytes <= 0 else max(0.0, min(100.0, done_bytes * 100.0 / total_bytes))
        self.progress.configure(maximum=100)
        self.progress_var.set(overall_percent)
        total_speed = sum(self.file_speed_bytes.get(path, 0.0) for path in tracked_paths)
        self.total_speed_var.set(f"总速度 {format_speed(total_speed)}")

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise GuiDownloadStopped("下载已停止")

    def parse_share(self) -> None:
        share_text = self.share_text_var.get().strip()
        if not share_text:
            self.messagebox.showwarning("缺少分享文本", f"请先粘贴{self.config.provider_name}分享链接或完整分享文本。")
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
            self.messagebox.showwarning("没有文件", "请先解析分享文件列表。")
            return

        selected_files = self.get_selected_files()
        if not selected_files:
            self.messagebox.showwarning("没有选择文件", "请先在文件列表中选择需要下载的文件。")
            return

        download_root = self.download_root_var.get().strip() or self.config.default_download_dir
        file_workers, retries = self.read_worker_settings()
        session = self.provider_session
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        for file in selected_files:
            self.set_file_status(file.path, "等待", 0.0)

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

                config = DownloadConfig(
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
                download_file_with_resolver(file, resolve, config)

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


def launch_tk_gui(config: ProviderGuiConfig) -> None:
    try:
        app = CommonPanTkApp(config)
    except Exception as exc:
        print(f"启动窗口失败: {exc}")
        return
    app.run()
