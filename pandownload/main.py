"""PanDownload 启动入口。

用法：
    python -m pandownload            # 启动美化版 Flet 图形界面（推荐）
    python -m pandownload --cli URL  # 命令行：自动识别网盘并下载

也可直接运行本文件：python pandownload/main.py
"""

import argparse
import os
import sys

# 允许 `python pandownload/main.py` 直接运行（把仓库根目录加入 sys.path）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pandownload.providers import (  # noqa: E402
    build_pandownload_config,
    detect_provider,
    provider_display_name,
    unified_create_session,
)


def launch_gui() -> None:
    from pandownload.ui import launch_pandownload_gui

    launch_pandownload_gui(build_pandownload_config())


def run_cli(args: argparse.Namespace) -> int:
    from common_pan.core import (
        DownloadConfig,
        download_file_with_resolver,
        format_size,
        parse_selection,
    )

    share_text = (args.url or "").strip()
    if not share_text:
        print("请粘贴分享链接（百度 / 夸克 / 阿里云盘）：")
        share_text = input("> ").strip()
    if not share_text:
        print("未提供分享链接。")
        return 2

    provider = detect_provider(share_text)
    if provider is None:
        print("无法识别网盘类型，支持：百度网盘 / 夸克网盘 / 阿里云盘。")
        return 2
    print(f"识别为：{provider_display_name(provider)}")

    session = unified_create_session(share_text)
    files = [f for f in session.files if not f.is_folder]
    if not files:
        print("没有可下载的文件。")
        return 1

    for index, file in enumerate(files, start=1):
        print(f"{index:>3}. {file.path}  {format_size(file.size)}")

    if args.list_only:
        return 0

    selection = args.select
    if not selection:
        selection = input("选择要下载的文件（默认 all，可用 1,3-5）: ").strip() or "all"
    targets = [files[i] for i in parse_selection(selection, len(files))]

    out_dir = args.out or os.path.join("D:\\", "下载", "pandownload")
    os.makedirs(out_dir, exist_ok=True)
    print(f"保存目录：{out_dir}")

    import threading

    resolve_lock = threading.Lock()
    ok = fail = 0
    for file in targets:
        print(f"\n下载: {file.path}")

        def resolve(f=file) -> str:
            if session.serialize_resolve:
                with resolve_lock:
                    return session.resolve_url(f)
            return session.resolve_url(f)

        config = DownloadConfig(
            output_dir=out_dir,
            user_agent=session.user_agent,
            referer=session.referer,
            extra_headers=session.extra_headers,
            retries=max(1, args.retries),
            overwrite=args.overwrite,
            show_progress=True,
        )
        try:
            download_file_with_resolver(file, resolve, config)
            ok += 1
        except Exception as exc:
            print(f"  [失败] {file.path}: {exc}")
            fail += 1

    print(f"\n完成：成功 {ok} 个，失败 {fail} 个，保存至 {out_dir}")
    return 0 if fail == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pandownload",
        description="百度 / 夸克 / 阿里云盘 三合一直链下载，自动识别链接类型。",
    )
    parser.add_argument("url", nargs="?", help="分享链接或完整分享文本")
    parser.add_argument("--gui", action="store_true", help="强制启动图形界面")
    parser.add_argument("--cli", action="store_true", help="使用命令行模式")
    parser.add_argument("--select", default="", help="要下载的文件序号，如 1,3-5 或 all")
    parser.add_argument("--out", default="", help="保存目录")
    parser.add_argument("--retries", type=int, default=5, help="单文件重试次数")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在文件")
    parser.add_argument("--list-only", action="store_true", help="仅列出文件，不下载")
    return parser


def main() -> int:
    argv = sys.argv[1:]
    args = build_parser().parse_args()

    # 默认进入图形界面；带 URL 或 --cli 时走命令行。
    use_cli = args.cli or (args.url and not args.gui)
    if use_cli:
        return run_cli(args)

    launch_gui()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
