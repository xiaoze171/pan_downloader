import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
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
    print_files,
    run_jobs,
    selected_files,
)
from common_pan.tk_gui import ProviderDownloadSession, ProviderGuiConfig, launch_tk_gui  # noqa: E402


def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = get_app_dir()
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.local.json")
DEFAULT_DOWNLOAD_DIR = os.path.join("D:\\", "下载", "aliyun_drive_download")
CLOUD_SAVE_ROOT = "/xiaoze/ali_pan_downloader"
API_BASE = "https://api.aliyundrive.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.alipan.com/"
DOWNLOAD_REFERER = "https://www.aliyundrive.com/"
CLIENT_ID = "25dzX3vbYqktVxyX"
APP_ID = "pJZInNHN2dZWk8qg"


def parse_share(text: str, explicit_pwd: str = "") -> Tuple[str, str]:
    match = re.search(r"(?:aliyundrive|alipan)\.com/s/([A-Za-z0-9_-]+)", text)
    if not match:
        raise ValueError("Cannot find Aliyun Drive share_id in input text.")
    return match.group(1), explicit_pwd.strip() or extract_password(text)


def normalize_access_token(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            return ""
        return find_token(parsed)
    return text if looks_like_access_token(text) else ""


def find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("access_token", "accessToken", "token"):
            token = value.get(key)
            if isinstance(token, str) and looks_like_access_token(token):
                return token.strip()
        for item in value.values():
            token = find_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_token(item)
            if token:
                return token
    return ""


def looks_like_access_token(value: str) -> bool:
    text = (value or "").strip()
    return text.count(".") == 2 and all(part for part in text.split("."))


class AliyunDriveClient:
    def __init__(self, access_token: str = "", refresh_token: str = "", default_drive_id: str = "") -> None:
        self.refresh_token = (refresh_token or "").strip()
        self.default_drive_id = (default_drive_id or "").strip()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Origin": "https://www.alipan.com",
                "Referer": REFERER,
                "Content-Type": "application/json;charset=UTF-8",
                "x-canary": "client=web,app=adrive,version=v4.9.0",
                "x-device-id": stable_device_id(access_token, refresh_token),
                "x-client-id": CLIENT_ID,
            }
        )
        token = normalize_access_token(access_token)
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.share_token = ""

    def post(self, path: str, payload: Dict[str, Any], use_share_token: bool = True) -> Dict[str, Any]:
        headers = {}
        if use_share_token and self.share_token:
            headers["x-share-token"] = self.share_token
        response = self.session.post(
            f"{API_BASE}{path}",
            json=payload,
            headers=headers,
            timeout=(10, 60),
        )
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(response.text[:500])
        if is_access_token_error(response.status_code, data) and self.refresh_token:
            self.refresh_access_token()
            response = self.session.post(
                f"{API_BASE}{path}",
                json=payload,
                headers=headers,
                timeout=(10, 60),
            )
            try:
                data = response.json()
            except ValueError:
                response.raise_for_status()
                raise RuntimeError(response.text[:500])
        if response.status_code >= 400 or data.get("code"):
            raise RuntimeError(f"Aliyun API error {response.status_code}: {json.dumps(data, ensure_ascii=False)}")
        return data

    def post_url(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(url, json=payload, timeout=(10, 60))
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(response.text[:500])
        if is_access_token_error(response.status_code, data) and self.refresh_token:
            self.refresh_access_token()
            response = self.session.post(url, json=payload, timeout=(10, 60))
            try:
                data = response.json()
            except ValueError:
                response.raise_for_status()
                raise RuntimeError(response.text[:500])
        if response.status_code >= 400 or data.get("code"):
            raise RuntimeError(f"Aliyun API error {response.status_code}: {json.dumps(data, ensure_ascii=False)}")
        return data

    def refresh_access_token(self) -> None:
        errors: List[str] = []
        for url, payload in (
            ("https://auth.aliyundrive.com/v2/account/token", {"grant_type": "refresh_token", "refresh_token": self.refresh_token}),
            ("https://api.aliyundrive.com/token/refresh", {"refresh_token": self.refresh_token}),
        ):
            try:
                response = self.session.post(url, json=payload, timeout=(10, 60))
                data = response.json()
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            if response.status_code >= 400 or data.get("code"):
                errors.append(f"{url}: {json.dumps(data, ensure_ascii=False)}")
                continue
            access_token = data.get("access_token")
            if access_token:
                self.session.headers["Authorization"] = f"Bearer {access_token}"
                self.refresh_token = str(data.get("refresh_token") or self.refresh_token)
                default_drive_id = str(data.get("default_drive_id") or data.get("resource_drive_id") or self.default_drive_id)
                if default_drive_id:
                    self.default_drive_id = default_drive_id
                save_local_credentials(str(access_token), self.refresh_token, self.default_drive_id)
                return
            errors.append(f"{url}: no access_token in {json.dumps(data, ensure_ascii=False)}")
        raise RuntimeError("Cannot refresh Aliyun access token. " + " | ".join(errors))

    def get_share_token(self, share_id: str, share_pwd: str) -> str:
        data = self.post("/v2/share_link/get_share_token", {"share_id": share_id, "share_pwd": share_pwd})
        token = data.get("share_token")
        if not token:
            raise RuntimeError(f"Aliyun did not return share_token: {json.dumps(data, ensure_ascii=False)}")
        self.share_token = str(token)
        return self.share_token

    def list_files(self, share_id: str, parent_file_id: str = "root", parent_path: str = "") -> List[CloudFile]:
        result: List[CloudFile] = []
        marker = ""
        while True:
            payload = {
                "share_id": share_id,
                "parent_file_id": parent_file_id,
                "limit": 100,
                "marker": marker,
                "order_by": "name",
                "order_direction": "ASC",
            }
            data = self.post_first_success(
                (
                    "/adrive/v2/file/list_by_share",
                    "/adrive/v3/file/list_by_share",
                    "/v2/file/list_by_share",
                    "/v3/file/list_by_share",
                ),
                payload,
            )
            for item in data.get("items") or []:
                name = str(item.get("name") or item.get("file_name") or item.get("file_id") or "unnamed")
                path = f"{parent_path}/{name}".strip("/")
                is_folder = item.get("type") == "folder"
                file = CloudFile(
                    file_id=str(item.get("file_id") or ""),
                    drive_id=str(item.get("drive_id") or ""),
                    name=name,
                    path=path,
                    size=int(item.get("size") or 0),
                    is_folder=is_folder,
                    raw=item,
                )
                if is_folder:
                    result.extend(self.list_files(share_id, file.file_id, path))
                else:
                    result.append(file)
            marker = str(data.get("next_marker") or "")
            if not marker:
                break
        return result

    def post_first_success(self, paths: Tuple[str, ...], payload: Dict[str, Any]) -> Dict[str, Any]:
        errors: List[str] = []
        for path in paths:
            try:
                return self.post(path, payload)
            except RuntimeError as exc:
                errors.append(f"{path}: {exc}")
        raise RuntimeError("Aliyun API candidates all failed. " + " | ".join(errors))

    def get_download_url(self, share_id: str, file: CloudFile) -> str:
        payload = {
            "share_id": share_id,
            "file_id": file.file_id,
            "expire_sec": 14400,
        }
        if file.drive_id:
            payload["drive_id"] = file.drive_id
        errors: List[str] = []
        for path in (
            "/adrive/v2/file/get_share_link_download_url",
            "/adrive/v3/file/get_share_link_download_url",
            "/v3/file/get_share_link_download_url",
            "/v2/file/get_share_link_download_url",
            "/v2/file/get_download_url",
        ):
            try:
                data = self.post(path, payload)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue
            url = first_url(data)
            if url:
                return url
            errors.append(f"{path}: no url in {json.dumps(data, ensure_ascii=False)}")
        preview_url = self.get_video_preview_url(share_id, file)
        if preview_url:
            return preview_url
        raise RuntimeError("Cannot get Aliyun download url. " + " | ".join(errors))

    def get_download_url_with_fallback(self, share_id: str, file: CloudFile, save_first: bool = False) -> str:
        if not save_first:
            return self.get_download_url(share_id, file)
        saved_file_id = self.save_shared_file(share_id, file)
        return self.get_own_file_download_url(saved_file_id)

    def get_default_drive_id(self) -> str:
        if self.default_drive_id:
            return self.default_drive_id
        data = self.post_url("https://user.aliyundrive.com/v2/user/get", {})
        drive_id = str(data.get("default_drive_id") or data.get("resource_drive_id") or "").strip()
        if not drive_id:
            raise RuntimeError(f"Aliyun did not return default_drive_id: {json.dumps(data, ensure_ascii=False)}")
        self.default_drive_id = drive_id
        save_local_credentials(default_drive_id=drive_id)
        return drive_id

    def ensure_own_cloud_dir(self, drive_id: str, path: str) -> str:
        parent_file_id = "root"
        for name in [part for part in path.strip("/").split("/") if part]:
            existing_id = self.find_own_child_folder(drive_id, parent_file_id, name)
            if existing_id:
                parent_file_id = existing_id
                continue

            payload = {
                "drive_id": drive_id,
                "parent_file_id": parent_file_id,
                "name": name,
                "type": "folder",
                "check_name_mode": "refuse",
            }
            data = self.post_first_success_no_share(("/adrive/v2/file/create", "/v2/file/create"), payload)
            folder_id = first_key(data, ("file_id", "id"))
            if not folder_id:
                folder_id = self.find_own_child_folder(drive_id, parent_file_id, name)
            if not folder_id:
                raise RuntimeError(f"Aliyun create folder returned no file_id: {json.dumps(data, ensure_ascii=False)}")
            parent_file_id = folder_id
        return parent_file_id

    def find_own_child_folder(self, drive_id: str, parent_file_id: str, name: str) -> str:
        marker = ""
        while True:
            data = self.post_first_success_no_share(
                ("/adrive/v2/file/list", "/v2/file/list"),
                {
                    "drive_id": drive_id,
                    "parent_file_id": parent_file_id,
                    "limit": 100,
                    "marker": marker,
                    "order_by": "name",
                    "order_direction": "ASC",
                },
            )
            for item in data.get("items") or []:
                if item.get("type") == "folder" and str(item.get("name") or "") == name:
                    return str(item.get("file_id") or "")
            marker = str(data.get("next_marker") or "")
            if not marker:
                return ""

    def post_first_success_no_share(self, paths: Tuple[str, ...], payload: Dict[str, Any]) -> Dict[str, Any]:
        errors: List[str] = []
        for path in paths:
            try:
                return self.post(path, payload, use_share_token=False)
            except RuntimeError as exc:
                message = str(exc)
                if "AlreadyExist" in message or "FileAlreadyExists" in message:
                    raise
                errors.append(f"{path}: {exc}")
        raise RuntimeError("Aliyun API candidates all failed. " + " | ".join(errors))

    def save_shared_file(self, share_id: str, file: CloudFile) -> str:
        to_drive_id = self.get_default_drive_id()
        to_parent_file_id = self.ensure_own_cloud_dir(to_drive_id, CLOUD_SAVE_ROOT)
        base_payload = {
            "share_id": share_id,
            "file_id": file.file_id,
            "to_drive_id": to_drive_id,
            "to_parent_file_id": to_parent_file_id,
            "auto_rename": True,
        }
        payloads = []
        if file.drive_id:
            payload = dict(base_payload)
            payload["drive_id"] = file.drive_id
            payloads.append(payload)
        payloads.append(dict(base_payload))

        errors: List[str] = []
        for path in ("/adrive/v2/file/copy", "/v2/file/copy"):
            for payload in payloads:
                try:
                    data = self.post(path, payload)
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                file_id = first_key(data, ("file_id", "id"))
                if file_id:
                    return file_id
                task_id = first_key(data, ("async_task_id", "task_id"))
                if task_id:
                    return self.wait_async_task(task_id)
                errors.append(f"{path}: no file_id/task_id in {json.dumps(data, ensure_ascii=False)}")
        raise RuntimeError("Aliyun save shared file failed. " + " | ".join(errors))

    def wait_async_task(self, task_id: str) -> str:
        last_data: Dict[str, Any] = {}
        for _ in range(60):
            for path in ("/v2/async_task/get", "/adrive/v2/async_task/get"):
                try:
                    data = self.post(path, {"async_task_id": task_id})
                except Exception:
                    continue
                last_data = data
                file_id = first_key(data, ("file_id", "id"))
                if file_id:
                    return file_id
                state = str(data.get("state") or data.get("status") or data.get("data", {}).get("state") or "").lower()
                if state in ("failed", "failure", "error"):
                    raise RuntimeError(f"Aliyun async task failed: {json.dumps(data, ensure_ascii=False)}")
            time.sleep(1)
        raise RuntimeError(f"Aliyun async task timed out: {json.dumps(last_data, ensure_ascii=False)}")

    def get_own_file_download_url(self, file_id: str) -> str:
        drive_id = self.get_default_drive_id()
        payload = {"drive_id": drive_id, "file_id": file_id, "expire_sec": 14400, "app_id": APP_ID}
        errors: List[str] = []
        for path in ("/adrive/v2/file/get_download_url", "/v2/file/get_download_url", "/v2/file/get_download_url?x-oss-process=video/snapshot,t_0,f_jpg", "/adrive/v3/file/get_download_url"):
            try:
                data = self.post(path, payload, use_share_token=False)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue
            url = first_url(data)
            if url:
                return url
            errors.append(f"{path}: no url in {json.dumps(data, ensure_ascii=False)}")
        preview_url = self.get_own_video_preview_url(drive_id, file_id)
        if preview_url:
            return preview_url
        raise RuntimeError("Cannot get Aliyun own-file download url. " + " | ".join(errors))

    def get_video_preview_url(self, share_id: str, file: CloudFile) -> str:
        payload = {
            "category": "live_transcoding",
            "file_id": file.file_id,
            "share_id": share_id,
            "template_id": "",
        }
        if file.drive_id:
            payload["drive_id"] = file.drive_id
        return self.get_preview_url(payload, use_share_token=True)

    def get_own_video_preview_url(self, drive_id: str, file_id: str) -> str:
        return self.get_preview_url(
            {"category": "live_transcoding", "drive_id": drive_id, "file_id": file_id, "template_id": ""},
            use_share_token=False,
        )

    def get_preview_url(self, payload: Dict[str, Any], use_share_token: bool) -> str:
        errors: List[str] = []
        for path in ("/v2/file/get_video_preview_play_info", "/adrive/v2/file/get_video_preview_play_info"):
            try:
                data = self.post(path, payload, use_share_token=use_share_token)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue
            url = best_preview_url(data)
            if url:
                return url
            errors.append(f"{path}: no preview url in {json.dumps(data, ensure_ascii=False)}")
        return ""


def best_preview_url(data: Any) -> str:
    urls: List[Tuple[int, str]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            url = first_url(value)
            if url:
                width = int(value.get("width") or 0)
                height = int(value.get("height") or 0)
                urls.append((width * height, url))
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(data)
    if not urls:
        return ""
    urls.sort(key=lambda item: item[0], reverse=True)
    return urls[0][1]


def stable_device_id(access_token: str, refresh_token: str) -> str:
    seed = (access_token or refresh_token or "pan_downloader_aliyun").encode("utf-8")
    digest = hashlib.sha256(seed).digest()[:16]
    return str(uuid.UUID(bytes=digest))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aliyun Drive share direct-link downloader.")
    parser.add_argument("share", nargs="?", help="Share link or copied share text.")
    parser.add_argument("--gui", action="store_true", help="Launch the desktop downloader UI.")
    parser.add_argument("--cli", action="store_true", help="Force command-line mode when running a packaged exe.")
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
        help="Save shared files to your Aliyun drive before resolving direct links. Enabled by default.",
    )
    parser.add_argument(
        "--direct-only",
        dest="save_first",
        action="store_false",
        help="Do not save shared files first; only try Aliyun share direct-link APIs.",
    )
    return parser


def create_gui_session(share_text: str) -> ProviderDownloadSession:
    share_id, share_pwd = parse_share(share_text)
    credentials = load_credentials(
        CREDENTIALS_FILE,
        ("ALIYUN_ACCESS_TOKEN", "ALIYUN_REFRESH_TOKEN", "ALIYUN_DEFAULT_DRIVE_ID"),
    )
    client = AliyunDriveClient(
        credentials.get("ALIYUN_ACCESS_TOKEN", ""),
        credentials.get("ALIYUN_REFRESH_TOKEN", ""),
        credentials.get("ALIYUN_DEFAULT_DRIVE_ID", ""),
    )
    client.get_share_token(share_id, share_pwd)
    files = client.list_files(share_id)

    def resolve(file: CloudFile) -> str:
        return client.get_download_url_with_fallback(share_id, file, save_first=True)

    return ProviderDownloadSession(
        files=files,
        resolve_url=resolve,
        user_agent=USER_AGENT,
        referer=DOWNLOAD_REFERER,
        folder_count=-1,
        serialize_resolve=True,
    )


def gui_credential_messages() -> List[str]:
    try:
        credentials = load_credentials(
            CREDENTIALS_FILE,
            ("ALIYUN_ACCESS_TOKEN", "ALIYUN_REFRESH_TOKEN", "ALIYUN_DEFAULT_DRIVE_ID"),
        )
    except Exception as exc:
        return [f"读取失败: {exc}"]
    messages = []
    if credentials.get("ALIYUN_ACCESS_TOKEN") or credentials.get("ALIYUN_REFRESH_TOKEN"):
        messages.append("已读取阿里云盘 token。")
    else:
        messages.append("未读取到阿里云盘 token，解析或保存到自己网盘可能会失败。")
    if credentials.get("ALIYUN_DEFAULT_DRIVE_ID"):
        messages.append("已读取 ALIYUN_DEFAULT_DRIVE_ID。")
    else:
        messages.append("未配置 ALIYUN_DEFAULT_DRIVE_ID，保存前会尝试自动获取。")
    return messages


def build_gui_config() -> ProviderGuiConfig:
    return ProviderGuiConfig(
        title="轻云链 - 阿里云盘",
        provider_name="阿里云盘",
        default_download_dir=DEFAULT_DOWNLOAD_DIR,
        credentials_file=CREDENTIALS_FILE,
        create_session=create_gui_session,
        credential_messages=gui_credential_messages,
        share_hint="粘贴阿里云盘分享链接或完整分享文本",
        default_file_workers=2,
        max_file_workers=4,
        default_retries=5,
        max_retries=10,
    )


def launch_gui() -> None:
    launch_tk_gui(build_gui_config())


def should_launch_gui(argv: List[str]) -> bool:
    if "--cli" in argv:
        return False
    return "--gui" in argv or bool(getattr(sys, "frozen", False) and len(argv) <= 1)


def prompt_for_missing_share(args: argparse.Namespace) -> None:
    if args.share:
        args.interactive = False
        return
    args.interactive = True
    print("Paste Aliyun Drive share link/text, then press Enter:")
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


def is_access_token_error(status_code: int, data: Dict[str, Any]) -> bool:
    code = str(data.get("code") or "").lower()
    message = str(data.get("message") or "").lower()
    return status_code == 401 or "accesstoken" in code or "not login" in message


def save_local_credentials(
    access_token: str = "",
    refresh_token: str = "",
    default_drive_id: str = "",
) -> None:
    try:
        current: Dict[str, Any] = {}
        if os.path.exists(CREDENTIALS_FILE):
            with open(CREDENTIALS_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                current = loaded
        if access_token:
            current["ALIYUN_ACCESS_TOKEN"] = access_token
        if refresh_token:
            current["ALIYUN_REFRESH_TOKEN"] = refresh_token
        if default_drive_id:
            current["ALIYUN_DEFAULT_DRIVE_ID"] = default_drive_id
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        print(f"Warning: failed to save refreshed Aliyun token: {exc}")


def main() -> None:
    args = build_parser().parse_args()
    if args.gui:
        launch_gui()
        return
    prompt_for_missing_share(args)
    share_id, share_pwd = parse_share(args.share, args.pwd)
    credentials = load_credentials(CREDENTIALS_FILE, ("ALIYUN_ACCESS_TOKEN", "ALIYUN_REFRESH_TOKEN"))
    client = AliyunDriveClient(
        credentials.get("ALIYUN_ACCESS_TOKEN", ""),
        credentials.get("ALIYUN_REFRESH_TOKEN", ""),
        credentials.get("ALIYUN_DEFAULT_DRIVE_ID", ""),
    )
    client.get_share_token(share_id, share_pwd)
    files = client.list_files(share_id)
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
        referer=DOWNLOAD_REFERER,
        retries=max(1, args.retries),
        overwrite=args.overwrite,
    )
    def process_file(file: CloudFile) -> None:
        print(f"\nResolving: {file.path}")
        def resolve() -> str:
            with url_lock:
                return client.get_download_url_with_fallback(share_id, file, save_first=args.save_first)

        if args.print_links and args.download:
            print(resolve())
        if args.download:
            download_file_with_resolver(file, resolve, config)
        else:
            print(resolve())

    run_jobs(targets, args.file_workers if args.download else 1, process_file)


if __name__ == "__main__":
    if should_launch_gui(sys.argv):
        launch_gui()
        sys.exit(0)
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
