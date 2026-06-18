import argparse
import json
import os
import re
import sys
import time
import threading
from typing import Any, Dict, List, Tuple

import requests


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common_pan import (  # noqa: E402
    CloudFile,
    DownloadConfig,
    download_file_with_resolver,
    extract_password,
    first_key,
    first_url,
    load_credentials,
    parse_json_response,
    print_files,
    run_jobs,
    selected_files,
)


def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = get_app_dir()
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.local.json")
DEFAULT_DOWNLOAD_DIR = os.path.join("D:\\", "下载", "quark_pan_download")
CLOUD_SAVE_ROOT = "/xiaoze/quark_pan_downloader"
API_BASE = "https://drive-pc.quark.cn/1/clouddrive"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc"}


def parse_share(text: str, explicit_pwd: str = "") -> Tuple[str, str]:
    match = re.search(r"(?:pan\.quark|drive\.uc)\.cn/s/([A-Za-z0-9_-]+)", text)
    if not match:
        raise ValueError("Cannot find Quark pwd_id in input text.")
    return match.group(1), explicit_pwd.strip() or extract_password(text)


class QuarkPanClient:
    def __init__(self, credentials: Dict[str, str]) -> None:
        self.to_pdir_fid = credentials.get("QUARK_TO_PDIR_FID", "0") or "0"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://pan.quark.cn",
                "Referer": "https://pan.quark.cn/",
                "Content-Type": "application/json;charset=UTF-8",
            }
        )
        if credentials.get("QUARK_COOKIE"):
            self.session.headers["Cookie"] = credentials["QUARK_COOKIE"]

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(COMMON_PARAMS)
        merged.update(params)
        response = self.session.get(f"{API_BASE}{path}", params=merged, timeout=(10, 60))
        return parse_quark_response(response)

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(f"{API_BASE}{path}", params=COMMON_PARAMS, json=payload, timeout=(10, 60))
        return parse_quark_response(response)

    def get_share_token(self, pwd_id: str, passcode: str) -> str:
        payload = {"pwd_id": pwd_id, "passcode": passcode}
        errors: List[str] = []
        for method in ("post", "get"):
            try:
                data = self.post("/share/sharepage/token", payload) if method == "post" else self.get("/share/sharepage/token", payload)
            except Exception as exc:
                errors.append(f"{method}: {exc}")
                continue
            token = str(data.get("data", {}).get("stoken") or data.get("stoken") or "")
            if token:
                return token
            errors.append(f"{method}: no stoken in {json.dumps(data, ensure_ascii=False)}")
        raise RuntimeError("Quark did not return stoken. " + " | ".join(errors))

    def list_files(self, pwd_id: str, stoken: str, pdir_fid: str = "0", parent_path: str = "") -> List[CloudFile]:
        result: List[CloudFile] = []
        page = 1
        while True:
            data = self.get(
                "/share/sharepage/detail",
                {
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": pdir_fid,
                    "_page": page,
                    "_size": 100,
                    "_fetch_banner": 0,
                    "_fetch_share": 0,
                    "_fetch_total": 1,
                    "sort": "file_type:asc,file_name:asc",
                },
            )
            items = data.get("data", {}).get("list") or data.get("data", {}).get("items") or data.get("list") or []
            for item in items:
                name = str(item.get("file_name") or item.get("name") or item.get("title") or item.get("fid") or "unnamed")
                path = f"{parent_path}/{name}".strip("/")
                is_folder = self.is_folder(item)
                file = CloudFile(
                    file_id=str(item.get("fid") or ""),
                    fid_token=str(item.get("share_fid_token") or item.get("fid_token") or ""),
                    name=name,
                    path=path,
                    size=int(item.get("size") or 0),
                    is_folder=is_folder,
                    raw=item,
                )
                if is_folder:
                    result.extend(self.list_files(pwd_id, stoken, file.file_id, path))
                else:
                    result.append(file)
            metadata = data.get("metadata") or data.get("data", {}).get("metadata") or {}
            total = int(metadata.get("_total") or data.get("data", {}).get("total") or 0)
            if not items or (total and page * 100 >= total):
                break
            page += 1
        return result

    @staticmethod
    def is_folder(item: Dict[str, Any]) -> bool:
        if item.get("dir") is not None:
            return bool(item.get("dir"))
        if item.get("file") is not None:
            return not bool(item.get("file"))
        file_type = str(item.get("file_type") or item.get("type") or "").lower()
        return file_type in ("0", "folder", "dir")

    def get_download_url(self, pwd_id: str, stoken: str, file: CloudFile, save_first: bool = False) -> str:
        errors: List[str] = []
        if save_first:
            try:
                saved_fid = self.save_shared_file(pwd_id, stoken, file)
                data = self.post("/file/download", {"fids": [saved_fid]})
                url = first_url(data)
                if url:
                    return url
                errors.append(json.dumps(data, ensure_ascii=False))
            except Exception as exc:
                errors.append(str(exc))

        url = first_url(file.raw)
        if url:
            return url
        for payload in (
            {"pwd_id": pwd_id, "stoken": stoken, "fid_list": [file.file_id]},
            {"pwd_id": pwd_id, "stoken": stoken, "fids": [file.file_id]},
        ):
            try:
                data = self.post("/share/sharepage/download", payload)
            except Exception as exc:
                errors.append(str(exc))
                continue
            url = first_url(data)
            if url:
                return url
            errors.append(json.dumps(data, ensure_ascii=False))

        raise RuntimeError("Cannot get Quark download url. " + " | ".join(errors[-3:]))

    def save_shared_file(self, pwd_id: str, stoken: str, file: CloudFile) -> str:
        to_pdir_fid = self.ensure_own_cloud_dir(CLOUD_SAVE_ROOT)
        payload = {
            "fid_list": [file.file_id],
            "fid_token_list": [file.fid_token] if file.fid_token else [],
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": file.raw.get("pdir_fid") or "0",
            "scene": "link",
        }
        data = self.post("/share/sharepage/save", payload)
        task_id = str(data.get("data", {}).get("task_id") or data.get("task_id") or "")
        if not task_id:
            fid = first_key(data, ("fid", "file_id"))
            if fid:
                return fid
            raise RuntimeError(f"Quark save did not return task_id: {json.dumps(data, ensure_ascii=False)}")
        return self.wait_save_task(task_id, file.name)

    def wait_save_task(self, task_id: str, expected_name: str) -> str:
        last_data: Dict[str, Any] = {}
        for retry_index in range(30):
            data = self.get("/task", {"task_id": task_id, "retry_index": retry_index})
            last_data = data
            fid = first_key(data, ("fid", "file_id", "to_fid", "save_as_top_fid"))
            if fid:
                return fid
            fid_list = first_list_item(data, ("save_as_top_fids", "fids", "file_ids"))
            if fid_list:
                return fid_list
            status = str(data.get("data", {}).get("status") or data.get("status") or "")
            if status.lower() in ("failed", "error", "3", "4"):
                raise RuntimeError(f"Quark save task failed: {json.dumps(data, ensure_ascii=False)}")
            time.sleep(1)
        raise RuntimeError(f"Quark save task timed out for {expected_name}. Last response: {json.dumps(last_data, ensure_ascii=False)}")

    def ensure_own_cloud_dir(self, path: str) -> str:
        parent_fid = self.to_pdir_fid or "0"
        for name in [part for part in path.strip("/").split("/") if part]:
            existing_fid = self.find_own_child_folder(parent_fid, name)
            if existing_fid:
                parent_fid = existing_fid
                continue
            parent_fid = self.create_own_folder(parent_fid, name)
        return parent_fid

    def find_own_child_folder(self, pdir_fid: str, name: str) -> str:
        page = 1
        while True:
            data = self.get(
                "/file/sort",
                {
                    "pdir_fid": pdir_fid,
                    "_page": page,
                    "_size": 100,
                    "_fetch_total": 1,
                    "sort": "file_type:asc,file_name:asc",
                },
            )
            items = data.get("data", {}).get("list") or data.get("data", {}).get("items") or data.get("list") or []
            for item in items:
                item_name = str(item.get("file_name") or item.get("name") or "")
                if item_name == name and self.is_folder(item):
                    return str(item.get("fid") or "")
            metadata = data.get("metadata") or data.get("data", {}).get("metadata") or {}
            total = int(metadata.get("_total") or data.get("data", {}).get("total") or 0)
            if not items or (total and page * 100 >= total):
                return ""
            page += 1

    def create_own_folder(self, pdir_fid: str, name: str) -> str:
        payloads = (
            {"pdir_fid": pdir_fid, "file_name": name, "dir_path": "", "dir_init_lock": False},
            {"pdir_fid": pdir_fid, "file_name": name, "dir_path": ""},
            {"pdir_fid": pdir_fid, "name": name, "dir_path": ""},
        )
        errors: List[str] = []
        for payload in payloads:
            try:
                data = self.post("/file", payload)
            except Exception as exc:
                errors.append(str(exc))
                continue
            fid = first_key(data, ("fid", "file_id"))
            if fid:
                return fid
            existing_fid = self.find_own_child_folder(pdir_fid, name)
            if existing_fid:
                return existing_fid
            errors.append(json.dumps(data, ensure_ascii=False))
        raise RuntimeError(f"Quark create folder failed for {name}: {' | '.join(errors[-3:])}")


def parse_quark_response(response: requests.Response) -> Dict[str, Any]:
    data = parse_json_response(response, "Quark")
    status = data.get("status")
    code = data.get("code")
    if status not in (None, 0, 200, "0", "200"):
        raise RuntimeError(f"Quark API error: {json.dumps(data, ensure_ascii=False)}")
    if code not in (None, 0, 200, "0", "200"):
        raise RuntimeError(f"Quark API error: {json.dumps(data, ensure_ascii=False)}")
    return data


def first_list_item(value: Any, keys: Tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, list) and item:
                return str(item[0])
            if isinstance(item, str) and item:
                return item
        for item in value.values():
            found = first_list_item(item, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = first_list_item(item, keys)
            if found:
                return found
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quark Pan share direct-link downloader.")
    parser.add_argument("share", nargs="?", help="Share link or copied share text.")
    parser.add_argument("--pwd", default="", help="Share password, if the link text does not contain it.")
    parser.add_argument("--out", default=DEFAULT_DOWNLOAD_DIR, help="Download directory.")
    parser.add_argument("--select", default="all", help="File indexes to process, for example: all, 1, 1,3-5.")
    parser.add_argument("--download", action="store_true", help="Download files after resolving direct links.")
    parser.add_argument("--print-links", action="store_true", help="Print resolved direct links.")
    parser.add_argument("--list-only", action="store_true", help="Only list files.")
    parser.add_argument("--retries", type=int, default=5, help="Download retry count.")
    parser.add_argument("--file-workers", type=int, default=1, help="Concurrent file download count.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite finished local files.")
    parser.add_argument(
        "--save-first",
        dest="save_first",
        action="store_true",
        default=True,
        help="Save shared files to your Quark drive before resolving direct links. Enabled by default.",
    )
    parser.add_argument(
        "--direct-only",
        dest="save_first",
        action="store_false",
        help="Do not save shared files first; only try Quark share direct-link APIs.",
    )
    return parser


def prompt_for_missing_share(args: argparse.Namespace) -> None:
    if args.share:
        args.interactive = False
        return
    args.interactive = True
    print("Paste Quark Pan share link/text, then press Enter:")
    args.share = input("> ").strip()
    if not args.share:
        raise RuntimeError("Share link is required.")
    if not args.pwd and not extract_password(args.share):
        args.pwd = input("Password (press Enter if none or already in link): ").strip()


def prompt_for_interactive_options(args: argparse.Namespace) -> None:
    if not getattr(args, "interactive", False):
        return
    selection = input("Select files to process (default 1, use all for all files): ").strip()
    args.select = selection or "1"
    answer = input("Download selected files? (Y/n): ").strip().lower()
    args.download = answer not in ("n", "no")
    args.save_first = True


def pause(message: str) -> None:
    try:
        input(message)
    except EOFError:
        pass


def main() -> None:
    args = build_parser().parse_args()
    prompt_for_missing_share(args)
    pwd_id, passcode = parse_share(args.share, args.pwd)
    credentials = load_credentials(CREDENTIALS_FILE, ("QUARK_COOKIE", "QUARK_TO_PDIR_FID"), {"QUARK_TO_PDIR_FID": "0"})
    client = QuarkPanClient(credentials)
    stoken = client.get_share_token(pwd_id, passcode)
    files = client.list_files(pwd_id, stoken)
    if not files:
        print("No files found.")
        return

    print_files(files)
    if args.list_only:
        return
    prompt_for_interactive_options(args)

    targets = selected_files(files, args.select)
    url_lock = threading.Lock()
    config = DownloadConfig(
        output_dir=args.out,
        user_agent=USER_AGENT,
        referer="https://pan.quark.cn/",
        extra_headers={"Cookie": client.session.headers.get("Cookie", "")},
        retries=max(1, args.retries),
        overwrite=args.overwrite,
    )
    def process_file(file: CloudFile) -> None:
        print(f"\nResolving: {file.path}")
        def resolve() -> str:
            with url_lock:
                return client.get_download_url(pwd_id, stoken, file, save_first=args.save_first)

        if args.print_links and args.download:
            print(resolve())
        if args.download:
            download_file_with_resolver(file, resolve, config)
        else:
            print(resolve())

    run_jobs(targets, args.file_workers if args.download else 1, process_file)


if __name__ == "__main__":
    pause_on_exit = bool(getattr(sys, "frozen", False) and len(sys.argv) <= 1)
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        if pause_on_exit:
            pause("\nPress Enter to exit...")
        sys.exit(130)
    except Exception as exc:
        print(f"\nError: {exc}")
        if pause_on_exit:
            pause("\nPress Enter to exit...")
        sys.exit(1)
    if pause_on_exit:
        pause("\nDone. Press Enter to exit...")
