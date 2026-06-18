import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_FILE = os.path.join(ROOT_DIR, "测试数据以及对应配置")
DEFAULT_OUTPUT_DIR = os.path.join("D:\\", "pan_downloader_test_download")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class TestCase:
    provider: str
    link: str
    script: str


PROVIDER_SCRIPTS: Dict[str, str] = {
    "aliyun": os.path.join(ROOT_DIR, "aliyun_drive", "aliyun_drive_direct_downloader.py"),
    "xunlei": os.path.join(ROOT_DIR, "xunlei_pan", "xunlei_pan_direct_downloader.py"),
    "quark": os.path.join(ROOT_DIR, "quark_pan", "quark_pan_direct_downloader.py"),
}


def detect_provider(link: str) -> str:
    if re.search(r"(?:aliyundrive|alipan)\.com/s/", link):
        return "aliyun"
    if "pan.xunlei.com/s/" in link:
        return "xunlei"
    if re.search(r"(?:pan\.quark|drive\.uc)\.cn/s/", link):
        return "quark"
    raise ValueError(f"Cannot detect provider for link: {link}")


def read_test_cases(path: str) -> List[TestCase]:
    with open(path, "r", encoding="utf-8-sig") as f:
        links = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#") and re.search(r"https?://", line)
        ]

    cases: List[TestCase] = []
    for link in links:
        provider = detect_provider(link)
        cases.append(TestCase(provider=provider, link=link, script=PROVIDER_SCRIPTS[provider]))
    return cases


def build_command(args: argparse.Namespace, case: TestCase) -> List[str]:
    command = [sys.executable, case.script, case.link, "--select", args.select, "--retries", str(args.retries)]
    if args.mode == "list":
        command.append("--list-only")
    elif args.mode == "download":
        command.extend(["--download", "--out", os.path.join(args.out, case.provider)])
        command.extend(["--file-workers", str(args.file_workers)])
        if args.print_links:
            command.append("--print-links")
        if args.overwrite:
            command.append("--overwrite")
    elif args.mode != "links":
        raise ValueError(f"Unsupported mode: {args.mode}")

    if case.provider == "quark" or (case.provider == "aliyun" and args.save_first):
        command.append("--save-first")
    return command


def run_case(args: argparse.Namespace, case: TestCase) -> int:
    print(f"\n===== {case.provider.upper()} =====", flush=True)
    command = build_command(args, case)
    display_command = " ".join(f'"{part}"' if " " in part else part for part in command)
    print(display_command, flush=True)
    completed = subprocess.run(command, cwd=ROOT_DIR)
    print(f"===== {case.provider.upper()} EXIT {completed.returncode} =====", flush=True)
    return int(completed.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Aliyun/Xunlei/Quark test links with their standalone scripts.")
    parser.add_argument("--data", default=DEFAULT_DATA_FILE, help="Text file containing one share link per line.")
    parser.add_argument("--mode", choices=("list", "links", "download"), default="links", help="Test mode.")
    parser.add_argument("--provider", choices=("all", "aliyun", "xunlei", "quark"), default="all", help="Run only one provider.")
    parser.add_argument("--select", default="1", help="File indexes passed to provider scripts. Default downloads/resolves the first file.")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_DIR, help="Root output directory for download mode.")
    parser.add_argument("--retries", type=int, default=3, help="Download retry count.")
    parser.add_argument("--file-workers", type=int, default=1, help="Concurrent file download count.")
    parser.add_argument("--print-links", action="store_true", help="Print direct links while downloading.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite finished local files.")
    parser.add_argument("--save-first", action="store_true", help="Use provider save-first fallback when supported. Quark always saves first.")
    parser.add_argument("--quark-save-first", action="store_true", help="Deprecated; Quark always saves first.")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a provider fails.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cases = read_test_cases(args.data)
    if args.provider != "all":
        cases = [case for case in cases if case.provider == args.provider]
    if not cases:
        print("No matching test cases.")
        return 1

    failures = 0
    for case in cases:
        code = run_case(args, case)
        if code != 0:
            failures += 1
            if not args.keep_going:
                break
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
