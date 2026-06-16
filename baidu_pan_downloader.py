import os
import re
import sys
import json
import time
import base64
import ctypes
import shutil
import socket
import sqlite3
import struct
import queue
import asyncio
import threading
import subprocess
import tempfile
import contextlib
import requests
from urllib.parse import urlparse, parse_qs, unquote, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional, Tuple, List, Dict

# ==================== 配置项 ====================
# 推荐通过 credentials.local.json 设置，避免在代码中暴露敏感信息
def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = get_app_dir()
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.local.json")
CREDENTIALS_LOAD_ERROR = ""
FILE_SELECTED_MARK = "✓"
FILE_UNSELECTED_MARK = ""

# 下载保存根目录
DOWNLOAD_ROOT = os.path.join("D:\\", "下载", "baidu_download")

# 文件级并发数
CPU_THREAD_COUNT = max(1, os.cpu_count() or 4)
MAX_WORKERS = 4
MAX_FILE_WORKERS = 4

# 单文件内部分段下载线程数，默认使用当前 CPU 逻辑线程数的一半
DOWNLOAD_PART_WORKERS = max(1, CPU_THREAD_COUNT // 2)
DOWNLOAD_MAX_PART_WORKERS = max(1, CPU_THREAD_COUNT // 2)
DOWNLOAD_PART_SIZE = 256 * 1024 * 1024

# 是否使用进度条（需要安装 tqdm）
USE_PROGRESS_BAR = True
try:
    from tqdm import tqdm
except ImportError:
    USE_PROGRESS_BAR = False
# ================================================

APP_ID = "250528"
CHANNEL = "chunlei"
CLIENTTYPE = "0"
WEB = "1"
PAN_DOWNLOAD_CLIENTTYPE = "8"
DOWNLOAD_USER_AGENT = "netdisk;P2SP;2.2.60.26"
DOWNLOAD_RANGE_SIZE = 4 * 1024 * 1024
DOWNLOAD_LINK_REFRESH_RETRIES = 12
DOWNLOAD_PART_REQUEST_RETRIES = 5
DOWNLOAD_PART_RETRY_DELAY = 2
DOWNLOAD_REQUEST_TIMEOUT = (20, 90)
DOWNLOAD_USE_ENV_PROXY = False
BAIDU_API_USE_ENV_PROXY = False
SHARE_REQUEST_TIMEOUT = (10, 30)
SHARE_LIST_PAGE_SIZE = 1000
CLOUD_SAVE_ROOT = "/xiaoze/baidu_pan_downloader"
TRANSFER_SEARCH_RETRIES = 6
TRANSFER_SEARCH_DELAY = 2
BAIDU_PAN_HOME_URL = "https://pan.baidu.com/disk/main#/index?category=all"
BAIDU_LOGIN_URL = BAIDU_PAN_HOME_URL
BAIDU_PASSPORT_CENTER_URL = "https://passport.baidu.com/center"
LOGIN_TIMEOUT_SECONDS = 10 * 60
LOGIN_POLL_INTERVAL = 2
LOGIN_STOKEN_GRACE_SECONDS = 45


class NoRetryError(RuntimeError):
    """已知业务限制，重复请求不会自动恢复。"""


class DownloadCancelled(RuntimeError):
    """用户取消任务。"""


def raise_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event and cancel_event.is_set():
        raise DownloadCancelled("下载任务已取消")


def sleep_or_cancel(cancel_event: Optional[threading.Event], seconds: float) -> None:
    if cancel_event and cancel_event.wait(seconds):
        raise DownloadCancelled("下载任务已取消")
    if not cancel_event:
        time.sleep(seconds)


def empty_credentials() -> Dict[str, str]:
    return {"BDUSS": "", "STOKEN": ""}


def normalize_credential_value(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in ("your_bduss_here", "your_stoken_here", "your_value_here"):
        return ""
    return text


def ensure_credentials_file() -> None:
    if os.path.exists(CREDENTIALS_FILE):
        return
    try:
        os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(empty_credentials(), f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        # 读取环境变量仍然可用；保存登录态时会再次报告具体错误。
        pass


def load_credentials() -> Tuple[str, str]:
    """优先读取本地私密配置文件，其次读取当前进程环境变量。"""
    global CREDENTIALS_LOAD_ERROR

    CREDENTIALS_LOAD_ERROR = ""
    bduss = os.getenv("BAIDU_BDUSS", "").strip()
    stoken = os.getenv("BAIDU_STOKEN", "").strip()

    ensure_credentials_file()
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8-sig") as f:
                credentials = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            CREDENTIALS_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
            credentials = {}

        bduss = normalize_credential_value(
            credentials.get("BDUSS")
            or credentials.get("BDUSS_BFESS")
            or credentials.get("BAIDU_BDUSS")
            or bduss
        )
        stoken = normalize_credential_value(
            credentials.get("STOKEN")
            or credentials.get("STOKEN_BFESS")
            or credentials.get("BAIDU_STOKEN")
            or stoken
        )

    return bduss, stoken


def save_credentials(credentials: Dict[str, str], require_stoken: bool = False) -> None:
    data = {}
    for key in ("BDUSS", "STOKEN", "BAIDUID"):
        value = (credentials.get(key) or "").strip()
        if value:
            data[key] = value

    if not data.get("BDUSS"):
        raise RuntimeError("登录成功但没有获取到 BDUSS Cookie")
    if require_stoken and not data.get("STOKEN"):
        raise RuntimeError("登录成功但没有获取到 STOKEN Cookie，请确认弹出的百度网盘页面已经完成登录")

    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_headers(bduss: str, stoken: str) -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://pan.baidu.com/",
    }


BDUSS, STOKEN = load_credentials()
HEADERS = build_headers(BDUSS, STOKEN)


def refresh_global_credentials(credentials: Dict[str, str]) -> None:
    global BDUSS, STOKEN, HEADERS
    BDUSS = (credentials.get("BDUSS") or "").strip()
    STOKEN = (credentials.get("STOKEN") or "").strip()
    HEADERS = build_headers(BDUSS, STOKEN)


def find_available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_edge_executable() -> str:
    candidates = []
    env_browser = (os.getenv("BAIDU_EDGE_PATH") or os.getenv("BAIDU_LOGIN_BROWSER") or "").strip()
    if env_browser:
        candidates.append(env_browser)

    for command in ("msedge.exe", "msedge"):
        path = shutil.which(command)
        if path:
            candidates.append(path)

    local_app_data = os.getenv("LOCALAPPDATA", "")
    program_files = [os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", "")]
    candidates.extend([
        os.path.join(local_app_data, "Microsoft", "Edge", "Application", "msedge.exe"),
    ])
    for root in program_files:
        if not root:
            continue
        candidates.extend([
            os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
        ])

    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise RuntimeError("未找到 Microsoft Edge。可以用 BAIDU_EDGE_PATH 指定 msedge.exe 路径")


def get_default_edge_user_data_dir() -> str:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return ""
    return os.path.join(local_app_data, "Microsoft", "Edge", "User Data")


def get_edge_source_profile_config() -> Tuple[str, str]:
    user_data_dir = (os.getenv("BAIDU_EDGE_USER_DATA_DIR") or "").strip()
    if not user_data_dir:
        user_data_dir = get_default_edge_user_data_dir()
    profile_name = normalize_edge_profile_name(os.getenv("BAIDU_EDGE_PROFILE") or "Default")
    return user_data_dir, profile_name


def should_use_temp_edge_profile() -> bool:
    value = (os.getenv("BAIDU_EDGE_USE_TEMP_PROFILE") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def should_use_direct_edge_profile() -> bool:
    value = (os.getenv("BAIDU_EDGE_DIRECT_PROFILE") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def normalize_edge_profile_name(profile_name: str) -> str:
    profile_name = (profile_name or "Default").strip().strip('"') or "Default"
    parts = profile_name.replace("\\", "/").split("/")
    if os.path.isabs(profile_name) or any(part in ("", ".", "..") for part in parts):
        return "Default"
    return profile_name


def copy_edge_profile_file(src_root: str, dst_root: str, relative_path: str) -> bool:
    src = os.path.join(src_root, *relative_path.split("/"))
    if not os.path.isfile(src):
        return False
    dst = os.path.join(dst_root, *relative_path.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def create_seeded_edge_user_data_dir(source_user_data_dir: str, profile_name: str) -> Tuple[str, int]:
    temp_dir = tempfile.mkdtemp(prefix="baidu_pan_login_")
    copied_count = 0
    try:
        for relative_path in (
            "Local State",
            f"{profile_name}/Preferences",
            f"{profile_name}/Secure Preferences",
            f"{profile_name}/Network/Cookies",
            f"{profile_name}/Network/Cookies-journal",
            f"{profile_name}/Network/Cookies-wal",
            f"{profile_name}/Network/Cookies-shm",
            f"{profile_name}/Cookies",
            f"{profile_name}/Cookies-journal",
            f"{profile_name}/Cookies-wal",
            f"{profile_name}/Cookies-shm",
        ):
            if copy_edge_profile_file(source_user_data_dir, temp_dir, relative_path):
                copied_count += 1
    except OSError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return temp_dir, copied_count


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def windows_crypt_unprotect_data(data: bytes) -> bytes:
    if os.name != "nt" or not data:
        return b""

    input_buffer = ctypes.create_string_buffer(data, len(data))
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def get_edge_cookie_encryption_key(user_data_dir: str) -> bytes:
    local_state_path = os.path.join(user_data_dir, "Local State")
    if not os.path.isfile(local_state_path):
        return b""
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)
    encrypted_key = ((local_state.get("os_crypt") or {}).get("encrypted_key") or "").strip()
    if not encrypted_key:
        return b""
    key_data = base64.b64decode(encrypted_key)
    if key_data.startswith(b"DPAPI"):
        key_data = key_data[5:]
    return windows_crypt_unprotect_data(key_data)


def decrypt_edge_cookie_value(value: Any, encrypted_value: Any, key: bytes) -> str:
    if value:
        return str(value)
    if not encrypted_value:
        return ""

    encrypted_bytes = bytes(encrypted_value)
    try:
        if encrypted_bytes.startswith((b"v10", b"v11", b"v20")) and key:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce = encrypted_bytes[3:15]
            payload = encrypted_bytes[15:]
            return AESGCM(key).decrypt(nonce, payload, None).decode("utf-8", "ignore")
        return windows_crypt_unprotect_data(encrypted_bytes).decode("utf-8", "ignore")
    except Exception:
        return ""


def copy_cookie_database(profile_dir: str, cookie_db_path: str) -> Tuple[str, str]:
    temp_dir = tempfile.mkdtemp(prefix="baidu_pan_cookie_")
    copied_db = os.path.join(temp_dir, "Cookies")
    shutil.copy2(cookie_db_path, copied_db)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = cookie_db_path + suffix
        if os.path.isfile(sidecar):
            shutil.copy2(sidecar, copied_db + suffix)
    return temp_dir, copied_db


def read_edge_profile_cookies(user_data_dir: str, profile_name: str) -> List[Dict]:
    if os.name != "nt":
        return []
    profile_dir = os.path.join(user_data_dir, profile_name)
    if not os.path.isdir(profile_dir):
        return []

    key = get_edge_cookie_encryption_key(user_data_dir)
    cookies = []
    for relative_path in (os.path.join("Network", "Cookies"), "Cookies"):
        cookie_db_path = os.path.join(profile_dir, relative_path)
        if not os.path.isfile(cookie_db_path):
            continue
        temp_dir = ""
        try:
            temp_dir, copied_db = copy_cookie_database(profile_dir, cookie_db_path)
            with sqlite3.connect(copied_db) as conn:
                rows = conn.execute(
                    """
                    SELECT host_key, name, value, encrypted_value
                    FROM cookies
                    WHERE name IN (
                        'BDUSS', 'BDUSS_BFESS',
                        'STOKEN', 'STOKEN_BFESS',
                        'BAIDUID', 'BAIDUID_BFESS'
                    )
                    """
                ).fetchall()
            for host_key, name, value, encrypted_value in rows:
                if "baidu.com" not in str(host_key):
                    continue
                cookie_value = decrypt_edge_cookie_value(value, encrypted_value, key)
                if cookie_value:
                    cookies.append({"domain": host_key, "name": name, "value": cookie_value})
        except Exception:
            continue
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
    return cookies


def read_edge_profile_credentials() -> Dict[str, str]:
    user_data_dir, profile_name = get_edge_source_profile_config()
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return {}
    return extract_login_credentials(read_edge_profile_cookies(user_data_dir, profile_name))


def get_edge_profile_config() -> Tuple[str, str, bool, str]:
    if should_use_temp_edge_profile():
        return tempfile.mkdtemp(prefix="baidu_pan_login_"), "", True, "clean"

    user_data_dir, profile_name = get_edge_source_profile_config()
    if user_data_dir and os.path.isdir(user_data_dir):
        if should_use_direct_edge_profile():
            return user_data_dir, profile_name, False, "direct"
        try:
            seeded_dir, copied_count = create_seeded_edge_user_data_dir(user_data_dir, profile_name)
            if copied_count:
                return seeded_dir, profile_name, True, "seeded"
            shutil.rmtree(seeded_dir, ignore_errors=True)
        except OSError:
            pass
        return tempfile.mkdtemp(prefix="baidu_pan_login_"), "", True, "clean"

    return tempfile.mkdtemp(prefix="baidu_pan_login_"), "", True, "clean"


def build_edge_login_args(browser: str, port: int, profile_dir: str, profile_name: str) -> List[str]:
    args = [
        browser,
        f"--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--disable-default-apps",
        "--new-window",
        f"--app={BAIDU_LOGIN_URL}",
    ]
    if profile_name:
        args.insert(4, f"--profile-directory={profile_name}")
    return args


def wait_for_browser_debug(port: int, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=1)
            if resp.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)
    raise RuntimeError("浏览器调试接口启动超时")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("WebSocket 连接已关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def websocket_send_frame(sock: socket.socket, payload: bytes, opcode: int = 1) -> None:
    first_byte = 0x80 | opcode
    payload_len = len(payload)
    if payload_len < 126:
        header = struct.pack("!BB", first_byte, 0x80 | payload_len)
    elif payload_len < 65536:
        header = struct.pack("!BBH", first_byte, 0x80 | 126, payload_len)
    else:
        header = struct.pack("!BBQ", first_byte, 0x80 | 127, payload_len)

    mask = os.urandom(4)
    masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header + mask + masked_payload)


def websocket_read_frame(sock: socket.socket) -> Tuple[int, bytes]:
    message_opcode = None
    chunks = []

    while True:
        first_byte, second_byte = recv_exact(sock, 2)
        fin = bool(first_byte & 0x80)
        opcode = first_byte & 0x0F
        payload_len = second_byte & 0x7F
        if payload_len == 126:
            payload_len = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack("!Q", recv_exact(sock, 8))[0]

        mask = recv_exact(sock, 4) if second_byte & 0x80 else None
        payload = recv_exact(sock, payload_len) if payload_len else b""
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        if opcode == 9:
            websocket_send_frame(sock, payload, opcode=10)
            continue
        if opcode == 8:
            raise RuntimeError("WebSocket 被浏览器关闭")
        if opcode in (1, 2):
            message_opcode = opcode
            chunks = [payload]
        elif opcode == 0 and message_opcode is not None:
            chunks.append(payload)
        else:
            continue

        if fin:
            return message_opcode or opcode, b"".join(chunks)


def websocket_request(ws_url: str, message: Dict, timeout: int = 5) -> Dict:
    parsed = urlparse(ws_url)
    if parsed.scheme != "ws":
        raise RuntimeError(f"不支持的 WebSocket 地址: {ws_url}")

    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("浏览器 WebSocket 握手连接已关闭")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("浏览器 WebSocket 握手失败")

        websocket_send_frame(sock, json.dumps(message).encode("utf-8"))
        while True:
            opcode, payload = websocket_read_frame(sock)
            if opcode != 1:
                continue
            data = json.loads(payload.decode("utf-8"))
            if data.get("id") == message.get("id"):
                return data


def cdp_call(ws_url: str, method: str, params: Optional[Dict] = None) -> Dict:
    message = {"id": int(time.time() * 1000), "method": method}
    if params:
        message["params"] = params
    response = websocket_request(ws_url, message)
    if response.get("error"):
        error = response["error"]
        raise RuntimeError(error.get("message") or error)
    return response.get("result") or {}


def get_cdp_cookies(port: int) -> List[Dict]:
    version = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
    browser_ws = version.get("webSocketDebuggerUrl")
    if browser_ws:
        try:
            result = cdp_call(browser_ws, "Storage.getCookies")
            cookies = result.get("cookies") or []
            if cookies:
                return cookies
        except Exception:
            pass

    targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
    last_error = None
    for target in targets:
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            result = cdp_call(ws_url, "Network.getAllCookies")
            cookies = result.get("cookies") or []
            if cookies:
                return cookies
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"读取浏览器 Cookie 失败: {last_error}")
    return []


def open_cdp_url(port: int, url: str) -> None:
    try:
        version = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
        browser_ws = version.get("webSocketDebuggerUrl")
        if browser_ws:
            cdp_call(browser_ws, "Target.createTarget", {"url": url})
            return
    except Exception:
        pass

    try:
        requests.put(f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}", timeout=2)
    except Exception:
        pass


def choose_cookie_value(cookies: List[Dict], names: Tuple[str, ...]) -> str:
    for name in names:
        for cookie in cookies:
            if cookie.get("name") == name and cookie.get("value"):
                return str(cookie["value"])
    return ""


def extract_login_credentials(cookies: List[Dict]) -> Dict[str, str]:
    return {
        "BDUSS": choose_cookie_value(cookies, ("BDUSS", "BDUSS_BFESS")),
        "STOKEN": choose_cookie_value(cookies, ("STOKEN", "STOKEN_BFESS")),
        "BAIDUID": choose_cookie_value(cookies, ("BAIDUID", "BAIDUID_BFESS")),
    }


def wait_for_login_credentials(port: int) -> Dict[str, str]:
    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    stoken_deadline = 0.0
    best_credentials: Dict[str, str] = {}
    passport_opened = False
    last_error = None
    while time.time() < deadline:
        try:
            credentials = extract_login_credentials(get_cdp_cookies(port))
            if credentials.get("BDUSS") and credentials.get("STOKEN"):
                return credentials
            if credentials.get("BDUSS"):
                best_credentials = credentials
                if not passport_opened:
                    open_cdp_url(port, BAIDU_PASSPORT_CENTER_URL)
                    stoken_deadline = time.time() + LOGIN_STOKEN_GRACE_SECONDS
                    passport_opened = True
                elif stoken_deadline and time.time() >= stoken_deadline:
                    return best_credentials
        except Exception as exc:
            last_error = exc
        time.sleep(LOGIN_POLL_INTERVAL)

    if best_credentials.get("BDUSS"):
        return best_credentials
    if last_error:
        raise RuntimeError(f"等待网页登录超时: {last_error}")
    raise RuntimeError("等待网页登录超时，未获取到 BDUSS Cookie")


def login_with_browser() -> Dict[str, str]:
    edge_credentials: Dict[str, str] = {}
    if not should_use_temp_edge_profile():
        edge_credentials = read_edge_profile_credentials()
        if edge_credentials.get("BDUSS") and edge_credentials.get("STOKEN"):
            save_credentials(edge_credentials, require_stoken=True)
            refresh_global_credentials(edge_credentials)
            print(f"已从 Edge 百度网盘登录态读取 BDUSS/STOKEN，并保存到 {CREDENTIALS_FILE}")
            return edge_credentials
        if edge_credentials.get("BDUSS"):
            print("已从 Edge 读取到 BDUSS，但没有读取到 STOKEN，正在打开网页登录页补齐...")

    browser = find_edge_executable()
    port = find_available_port()
    profile_dir, profile_name, is_temp_profile, profile_mode = get_edge_profile_config()
    process = None
    try:
        args = build_edge_login_args(browser, port, profile_dir, profile_name)
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for_browser_debug(port)
        except RuntimeError as exc:
            if profile_mode == "direct":
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                port = find_available_port()
                profile_dir = tempfile.mkdtemp(prefix="baidu_pan_login_")
                profile_name = ""
                is_temp_profile = True
                profile_mode = "clean"
                print("Edge 默认配置目录无法开启调试端口，已切换到独立登录窗口。")
                args = build_edge_login_args(browser, port, profile_dir, profile_name)
                process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait_for_browser_debug(port)
            else:
                raise
        if profile_mode == "seeded":
            print("已复制 Edge 当前登录态到临时窗口，正在读取百度 Cookie；如果没有自动识别，请在弹出窗口重新登录。")
        elif profile_mode == "clean":
            print("请在弹出的 Edge 百度网盘登录窗口完成登录，脚本会自动读取登录态并继续...")
        else:
            print(f"正在复用 Edge 配置目录: {profile_dir} ({profile_name})")
        open_cdp_url(port, BAIDU_PASSPORT_CENTER_URL)
        credentials = wait_for_login_credentials(port)
        for key, value in edge_credentials.items():
            if value and not credentials.get(key):
                credentials[key] = value
        save_credentials(credentials, require_stoken=True)
        refresh_global_credentials(credentials)
        print(f"已保存登录态到 {CREDENTIALS_FILE}")
        return credentials
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if is_temp_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)


def ensure_login_credentials(force_login: bool = False) -> None:
    if BDUSS and not force_login:
        return
    login_with_browser()


def is_likely_login_error(exc: Exception) -> bool:
    message = str(exc)
    return any(keyword in message for keyword in ("Cookie", "登录", "未找到 yunData", "login"))


# 重试装饰器
def retry(times=3, delay=2):
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for i in range(times):
                try:
                    return func(*args, **kwargs)
                except NoRetryError:
                    raise
                except Exception as e:
                    last_exc = e
                    if i + 1 < times:
                        print(f"  [!] 第 {i + 1}/{times} 次尝试失败: {e}，{delay}秒后重试...")
                        time.sleep(delay)
                    else:
                        print(f"  [!] 第 {i + 1}/{times} 次尝试失败: {e}")
            raise last_exc
        return wrapper
    return decorator


def extract_share_password(text: str) -> Optional[str]:
    decoded = unquote(str(text or ""))
    patterns = [
        r"(?:[?&#]|^)\s*pwd\s*=\s*([a-zA-Z0-9]{4,})",
        r"(?:提取码|密码|访问码)\s*[:：= ]\s*([a-zA-Z0-9]{4,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def require_password_from_link() -> str:
    raise RuntimeError("链接中未找到提取码，请粘贴包含提取码的完整分享文本，或带 ?pwd= 的分享链接")


def parse_share_link(url: str) -> Tuple[str, Optional[str]]:
    """提取分享链接中的 surl 和提取码"""
    text = str(url or "").strip()
    if "pan.baidu.com/s/" not in text:
        raise ValueError("不是有效的百度网盘分享链接")

    url_match = re.search(r"(https?://pan\.baidu\.com/[^\s\"'<>]+|pan\.baidu\.com/[^\s\"'<>]+)", text)
    share_url = url_match.group(1) if url_match else text
    share_url = share_url.strip(" \t\r\n，,。；;、)")
    if not share_url.startswith(("http://", "https://")):
        share_url = "https://" + share_url

    match = re.search(r"/s/([a-zA-Z0-9_-]+)", share_url)
    if not match:
        raise ValueError("无法提取分享ID")
    surl = match.group(1)

    # 提取密码（支持 ?pwd=、&pwd=、#pwd=、#abcd，以及复制分享文本里的“提取码: abcd”）
    pwd = None
    parsed = urlparse(share_url)
    query_params = parse_qs(parsed.query)
    if "pwd" in query_params:
        pwd = query_params["pwd"][0]
    elif parsed.fragment:
        fragment = unquote(parsed.fragment.strip())
        fragment_params = parse_qs(fragment)
        if "pwd" in fragment_params:
            pwd = fragment_params["pwd"][0]
        elif re.fullmatch(r"[a-zA-Z0-9]{4,}", fragment):
            pwd = fragment
    if not pwd:
        pwd = extract_share_password(text)
    if pwd:
        pwd = unquote(pwd).strip()
    return surl, pwd


def get_verify_surl(surl: str) -> str:
    """The verify API expects the share id without the leading /s/1 marker."""
    return surl[1:] if surl.startswith("1") else surl


def make_logid() -> str:
    raw = str(int(time.time() * 1000)).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def get_cookie_value(session: requests.Session, name: str) -> Optional[str]:
    for cookie in session.cookies:
        if cookie.name == name and cookie.value:
            return cookie.value
    return None


def make_session_logid(session: requests.Session) -> str:
    baiduid = get_cookie_value(session, "BAIDUID")
    if baiduid:
        return base64.b64encode(baiduid.encode("utf-8")).decode("ascii")
    return make_logid()


def set_login_cookies(session: requests.Session, bduss: str, stoken: str) -> None:
    if not bduss:
        return

    cookie_values = {
        "BDUSS": bduss,
        "BDUSS_BFESS": bduss,
    }
    if stoken:
        cookie_values["STOKEN"] = stoken
        cookie_values["STOKEN_BFESS"] = stoken

    for name, value in cookie_values.items():
        session.cookies.set(name, value)


def get_session_sekey(session: requests.Session) -> Optional[str]:
    value = get_cookie_value(session, "BDCLND")
    return unquote(value) if value else None


def is_api_success(payload: Dict) -> bool:
    return str(payload.get("errno", 0)) == "0"


def is_api_errno(payload: Dict, code: int) -> bool:
    return str(payload.get("errno")) == str(code)


def get_api_error_message(payload: Dict) -> str:
    return str(payload.get("errmsg") or payload.get("show_msg") or payload.get("errno") or "")


def is_download_verification_error(payload: Dict) -> bool:
    message = get_api_error_message(payload).lower()
    return (
        is_api_errno(payload, -20)
        or "验证码" in message
        or "vcode" in message
        or "captcha" in message
    )


def verify_share_password(session: requests.Session, surl: str, pwd: str) -> Optional[str]:
    """Submit the extraction code and keep the verification cookie in session."""
    verify_surl = get_verify_surl(surl)
    params = {
        "surl": verify_surl,
        "t": str(int(time.time() * 1000)),
        "channel": CHANNEL,
        "web": WEB,
        "app_id": APP_ID,
        "bdstoken": "null",
        "logid": make_logid(),
        "clienttype": CLIENTTYPE,
    }
    data = {
        "pwd": pwd,
        "vcode": "",
        "vcode_str": "",
    }
    headers = HEADERS.copy()
    headers.update({
        "Referer": f"https://pan.baidu.com/share/init?surl={verify_surl}",
        "X-Requested-With": "XMLHttpRequest",
    })

    resp = session.post(
        "https://pan.baidu.com/share/verify",
        params=params,
        data=data,
        headers=headers,
        timeout=SHARE_REQUEST_TIMEOUT,
    )

    try:
        result = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"提取码验证接口返回非 JSON，HTTP {resp.status_code}") from exc

    if not is_api_success(result):
        errmsg = result.get("errmsg") or result.get("show_msg") or result.get("errno")
        raise RuntimeError(f"提取码验证失败: errno={result.get('errno')}, message={errmsg}")

    randsk = result.get("randsk")
    if randsk:
        session.cookies.set("BDCLND", randsk)
        return unquote(randsk)

    return get_session_sekey(session)


def get_share_tplconfig(session: requests.Session, surl: str, fields: List[str]) -> Dict:
    params = {
        "surl": surl,
        "fields": ",".join(fields),
        "view_mode": "1",
        "channel": CHANNEL,
        "web": WEB,
        "app_id": APP_ID,
        "clienttype": CLIENTTYPE,
    }
    resp = session.get("https://pan.baidu.com/share/tplconfig", params=params, timeout=SHARE_REQUEST_TIMEOUT)
    data = resp.json()
    if not is_api_success(data):
        errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
        raise RuntimeError(f"获取分享配置失败: {errmsg}")
    return data.get("data") or data.get("result") or {}


def is_extract_code_page(html: str) -> bool:
    return "请输入提取码" in html or ("提取码" in html and "verify" in html)


def parse_yun_data(yun_data_text: str) -> Dict:
    try:
        return json.loads(yun_data_text)
    except json.JSONDecodeError:
        pass

    def read_value(key: str, required: bool = True) -> Optional[str]:
        pattern = rf"""["']?{re.escape(key)}["']?\s*:\s*(?:"([^"]*)"|'([^']*)'|([0-9]+))"""
        match = re.search(pattern, yun_data_text)
        if not match:
            if not required:
                return None
            raise RuntimeError(f"yunData 中缺少必要字段: {key}")
        return next(group for group in match.groups() if group is not None)

    share_uk = read_value("share_uk", required=False)
    uk = read_value("uk", required=False) or share_uk
    if not uk:
        raise RuntimeError("yunData 中缺少必要字段: uk/share_uk")

    return {
        "shareid": read_value("shareid"),
        "uk": uk,
        "share_uk": share_uk or uk,
        "sign": read_value("sign", required=False),
        "timestamp": read_value("timestamp", required=False),
        "bdstoken": read_value("bdstoken", required=False),
    }


def parse_inline_value(html: str, key: str) -> Optional[str]:
    patterns = [
        rf"""window\.{re.escape(key)}\s*=\s*["']([^"']+)["']""",
        rf"""locals\.set\(["']{re.escape(key)}["']\s*,\s*["']([^"']+)["']\)""",
        rf"""["']?{re.escape(key)}["']?\s*:\s*["']([^"']+)["']""",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def parse_locals_mset(html: str) -> Dict:
    match = re.search(r"locals\.mset\((\{.*?\})\);", html, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def get_yun_data(
    surl: str,
    pwd: Optional[str] = None,
    password_provider: Optional[Callable[[], str]] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[Dict, requests.Session]:
    """获取分享页面的 yunData 并返回 session"""
    def notify(message: str) -> None:
        print(f"  [解析] {message}")
        if status_callback:
            status_callback(message)

    base_url = f"https://pan.baidu.com/s/{surl}"
    if pwd:
        base_url += f"?pwd={pwd}"

    session = requests.Session()
    session.trust_env = BAIDU_API_USE_ENV_PROXY
    session.headers.update(HEADERS)
    set_login_cookies(session, BDUSS, STOKEN)
    if not session.trust_env:
        notify("百度接口请求不使用系统代理")

    password_verified = False
    prompted_for_pwd = False

    while True:
        notify("打开分享页面...")
        try:
            resp = session.get(base_url, allow_redirects=True, timeout=SHARE_REQUEST_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(f"打开分享页面失败: {exc}") from exc
        notify(f"分享页面已响应 HTTP {resp.status_code}")
        html = resp.text

        if is_extract_code_page(html):
            notify("页面要求提取码，正在验证...")
            if password_verified:
                raise RuntimeError("提取码已验证，但页面仍要求输入提取码；可能 Cookie 失效、提取码错误或分享链接异常")

            if not pwd and not prompted_for_pwd:
                if password_provider:
                    pwd = password_provider().strip()
                else:
                    pwd = input("页面需要提取码，请手动输入: ").strip()
                prompted_for_pwd = True
            if not pwd:
                raise RuntimeError("未提供提取码")

            try:
                sekey = verify_share_password(session, surl, pwd)
            except Exception as exc:
                raise RuntimeError(f"验证提取码失败: {exc}") from exc
            password_verified = True
            notify("提取码验证通过，重新打开分享页面...")
            base_url = f"https://pan.baidu.com/s/{surl}"
            continue  # 重新请求

        # 提取 yunData
        notify("提取分享页面数据...")
        match = re.search(r"window\.yunData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            match = re.search(r"yunData\s*=\s*({.*?});", html, re.DOTALL)
        if not match:
            raise RuntimeError("未找到 yunData，可能 Cookie 失效或页面结构变化")

        yun_data = parse_yun_data(match.group(1))
        locals_data = parse_locals_mset(html)
        for key in ("bdstoken", "loginstate", "is_vip", "is_svip", "vip_level", "vip_type"):
            if locals_data.get(key) is not None:
                yun_data[key] = locals_data[key]
        servertime_match = re.search(r"""locals\.set\(["']servertime["']\s*,\s*([0-9]+)\)""", html)
        if servertime_match and not yun_data.get("timestamp"):
            yun_data["timestamp"] = servertime_match.group(1)
        for key in ("jsToken", "bdstoken"):
            value = parse_inline_value(html, key)
            if value and not yun_data.get(key):
                yun_data[key] = value
        if "sekey" not in yun_data:
            yun_data["sekey"] = get_session_sekey(session)
        notify("获取分享下载配置...")
        try:
            tplconfig = get_share_tplconfig(session, surl, ["sign", "timestamp", "public"])
        except Exception as exc:
            raise RuntimeError(f"获取分享下载配置失败: {exc}") from exc
        for key in ("sign", "timestamp", "public"):
            if tplconfig.get(key) is not None:
                yun_data[key] = tplconfig[key]
        notify("分享解析完成")
        return yun_data, session


def is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def normalize_cloud_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized or normalized == ".":
        return "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def join_share_path(parent: str, name: str) -> str:
    parent = normalize_cloud_path(parent).rstrip("/")
    safe_name = str(name or "").replace("\\", "/").strip("/")
    if not safe_name:
        return parent or "/"
    return f"/{safe_name}" if not parent else f"{parent}/{safe_name}"


def extract_share_entries(payload: Dict) -> List[Dict]:
    containers = [payload]
    if isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    if isinstance(payload.get("result"), dict):
        containers.append(payload["result"])

    for container in containers:
        for key in ("list", "file_list", "records"):
            entries = container.get(key)
            if isinstance(entries, list):
                return entries
    return []


def has_more_share_pages(payload: Dict, entries: List[Dict], page: int) -> bool:
    containers = [payload]
    if isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    if isinstance(payload.get("result"), dict):
        containers.append(payload["result"])

    for container in containers:
        for key in ("has_more", "hasmore", "has_more_data"):
            if key in container:
                return is_truthy_flag(container.get(key))

        for key in ("total", "total_count"):
            value = container.get(key)
            try:
                return int(value) > page * SHARE_LIST_PAGE_SIZE
            except (TypeError, ValueError):
                pass

    return len(entries) >= SHARE_LIST_PAGE_SIZE


def build_share_file_info(entry: Dict, current_path: str) -> Dict:
    raw_path = entry.get("path") or entry.get("server_path")
    name = entry.get("server_filename") or entry.get("filename") or entry.get("name")

    if raw_path:
        rel_path = normalize_cloud_path(raw_path)
    else:
        rel_path = join_share_path(current_path, name)

    if not name:
        name = rel_path.rstrip("/").rsplit("/", 1)[-1]

    fs_id = entry.get("fs_id") or entry.get("fsid") or entry.get("id")
    if fs_id is None:
        raise RuntimeError(f"文件列表条目缺少 fs_id: {rel_path}")

    try:
        fs_id = int(fs_id)
    except (TypeError, ValueError):
        pass

    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    return {
        "path": rel_path,
        "name": str(name),
        "fs_id": fs_id,
        "isdir": is_truthy_flag(entry.get("isdir")),
        "size": size,
        "dlink": entry.get("dlink"),
    }


def get_file_list_recursive(yun_data: Dict, session: requests.Session, path: str = "/") -> List[Dict]:
    """递归获取所有文件和文件夹信息，兼容分页和字段类型差异。"""
    file_list = []
    seen_entries = set()
    fetched_dirs = set()
    pending_dirs = [normalize_cloud_path(path)]
    shareid = yun_data["shareid"]
    uk = yun_data.get("share_uk") or yun_data["uk"]

    def add_entry(entry: Dict, current_path: str) -> bool:
        try:
            info = build_share_file_info(entry, current_path)
        except Exception as exc:
            print(f"  [警告] 跳过无法识别的列表条目: {exc}")
            return False

        key = (str(info["fs_id"]), info["path"])
        if key in seen_entries:
            return False

        seen_entries.add(key)
        file_list.append(info)
        if info["isdir"]:
            pending_dirs.append(info["path"])
        return True

    @retry(times=3)
    def fetch_dir_page(current_path: str, page: int) -> Tuple[Dict, List[Dict]]:
        params = {
            "uk": uk,
            "shareid": shareid,
            "channel": CHANNEL,
            "clienttype": CLIENTTYPE,
            "web": WEB,
            "app_id": APP_ID,
            "order": "time",
            "desc": "1",
            "showempty": "0",
            "num": str(SHARE_LIST_PAGE_SIZE),
            "page": str(page),
            "dir": current_path,
            "root": "1" if current_path == "/" else "0",
        }
        if yun_data.get("sekey"):
            params["sekey"] = yun_data["sekey"]

        resp = session.get("https://pan.baidu.com/share/list", params=params, timeout=30)
        data = resp.json()
        if not is_api_success(data):
            errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
            raise RuntimeError(f"获取文件列表失败: {errmsg}")
        return data, extract_share_entries(data)

    for entry in extract_share_entries(yun_data):
        add_entry(entry, path)

    index = 0
    while index < len(pending_dirs):
        current_path = normalize_cloud_path(pending_dirs[index])
        index += 1
        if current_path in fetched_dirs:
            continue
        fetched_dirs.add(current_path)

        page = 1
        while True:
            data, entries = fetch_dir_page(current_path, page)
            added_count = 0
            for entry in entries:
                if add_entry(entry, current_path):
                    added_count += 1

            if not entries or not has_more_share_pages(data, entries, page) or (page > 1 and added_count == 0):
                break
            page += 1

    return file_list


def normalize_download_url(value: str) -> Optional[str]:
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return f"https:{value}"
    match = re.search(r"https?://[^\s\"'<>]+", value)
    if match:
        return match.group(0)
    return None


def extract_dlink(payload: Any) -> Optional[str]:
    """兼容 sharedownload 返回字典、字符串或嵌套列表的情况。"""
    if isinstance(payload, str):
        return normalize_download_url(payload)

    if isinstance(payload, list):
        for item in payload:
            dlink = extract_dlink(item)
            if dlink:
                return dlink
        return None

    if isinstance(payload, dict):
        direct = payload.get("dlink")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        for key in ("list", "data", "result"):
            dlink = extract_dlink(payload.get(key))
            if dlink:
                return dlink

    return None


def is_encrypted_client_task(payload: Any) -> bool:
    return isinstance(payload, str) and len(payload) > 100 and not normalize_download_url(payload)


def get_vip_value(yun_data: Dict) -> str:
    if str(yun_data.get("is_svip") or "0") == "1" or str(yun_data.get("vip_type") or "0") == "2":
        return "2"
    if str(yun_data.get("is_vip") or "0") == "1" or str(yun_data.get("vip_level") or "0") not in ("", "0"):
        return "1"
    return "0"


def get_txt_alias_name(file_name: str) -> str:
    stem, ext = os.path.splitext(file_name)
    return file_name if ext.lower() == ".txt" else f"{stem}.txt"


def get_cloud_file_name(path: str) -> str:
    return path.rstrip("/").split("/")[-1]


def join_cloud_path(parent: str, file_name: str) -> str:
    parent = parent.rstrip("/")
    return f"/{file_name}" if not parent else f"{parent}/{file_name}"


def get_cloud_parent(path: str) -> str:
    path = path.rstrip("/")
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def is_cloud_dir_exists_error(payload: Dict) -> bool:
    errmsg = str(payload.get("errmsg") or payload.get("show_msg") or "").lower()
    return str(payload.get("errno")) in ("-8", "31061") or "exist" in errmsg or "已存在" in errmsg


def ensure_cloud_dir(session: requests.Session, yun_data: Dict, path: str) -> str:
    path = normalize_cloud_path(path)
    if path == "/":
        return path

    ensured_dirs = yun_data.setdefault("_ensured_cloud_dirs", set())
    current = ""
    for part in [item for item in path.strip("/").split("/") if item]:
        current = f"{current}/{part}" if current else f"/{part}"
        if current in ensured_dirs:
            continue

        params = {
            "a": "commit",
            "web": WEB,
            "app_id": APP_ID,
            "channel": CHANNEL,
            "clienttype": CLIENTTYPE,
            "bdstoken": yun_data.get("bdstoken") or "",
            "logid": make_session_logid(session),
        }
        resp = session.post(
            "https://pan.baidu.com/api/create",
            params=params,
            data={"path": current, "isdir": "1", "block_list": "[]"},
            timeout=30,
        )
        data = resp.json()
        if not is_api_success(data) and not is_cloud_dir_exists_error(data):
            errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
            raise RuntimeError(f"创建自己网盘目录失败 {current}: {errmsg}")
        ensured_dirs.add(current)

    return path


def get_transfer_target_dir(yun_data: Dict, file_info: Dict) -> str:
    root = join_cloud_path(CLOUD_SAVE_ROOT, str(yun_data["shareid"]))
    parent = get_cloud_parent(normalize_cloud_path(file_info.get("path") or file_info["name"]))
    if parent == "/":
        return normalize_cloud_path(root)
    return normalize_cloud_path(join_cloud_path(root, parent.strip("/")))


def walk_dicts(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from walk_dicts(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from walk_dicts(item)


def raise_transfer_item_error(payload: Dict) -> None:
    for item in walk_dicts(payload):
        if "errno" not in item or is_api_success(item):
            continue
        if not any(key in item for key in ("from", "to", "fs_id", "to_fs_id", "path")):
            continue
        errmsg = item.get("errmsg") or item.get("show_msg") or item.get("errno")
        raise RuntimeError(f"保存分享文件到自己网盘失败: {errmsg}")


def extract_transferred_file(payload: Dict, file_info: Dict, target_dir: str) -> Optional[Dict]:
    expected_path = join_cloud_path(target_dir, file_info["name"])
    for item in walk_dicts(payload):
        if "errno" in item and not is_api_success(item):
            continue

        fs_id = item.get("to_fs_id") or item.get("fs_id") or item.get("id")
        path = item.get("to") or item.get("path") or item.get("server_path")
        if not fs_id and not path:
            continue

        path = normalize_cloud_path(path or expected_path)
        name = item.get("server_filename") or item.get("filename") or get_cloud_file_name(path)
        own_file = {
            "path": path,
            "server_filename": name,
            "size": int(file_info.get("size") or 0),
            "isdir": 0,
        }
        if fs_id is not None:
            try:
                own_file["fs_id"] = int(fs_id)
            except (TypeError, ValueError):
                own_file["fs_id"] = fs_id
        if own_file.get("fs_id"):
            return own_file

    return None


def wait_for_transferred_file(session: requests.Session, yun_data: Dict, file_info: Dict) -> Optional[Dict]:
    for attempt in range(TRANSFER_SEARCH_RETRIES):
        own_file = search_own_file(session, yun_data, file_info)
        if own_file:
            return own_file
        if attempt + 1 < TRANSFER_SEARCH_RETRIES:
            time.sleep(TRANSFER_SEARCH_DELAY)
    return None


def save_share_file_to_own_pan(session: requests.Session, yun_data: Dict, file_info: Dict) -> Dict:
    target_dir = get_transfer_target_dir(yun_data, file_info)
    try:
        ensure_cloud_dir(session, yun_data, target_dir)
    except Exception as exc:
        print(f"  [警告] 创建转存目录失败，将尝试保存到网盘根目录: {exc}")
        target_dir = "/"

    params = {
        "shareid": yun_data["shareid"],
        "from": yun_data.get("share_uk") or yun_data["uk"],
        "channel": CHANNEL,
        "web": WEB,
        "app_id": APP_ID,
        "clienttype": CLIENTTYPE,
        "bdstoken": yun_data.get("bdstoken") or "",
        "logid": make_session_logid(session),
        "async": "1",
        "ondup": "newcopy",
    }
    if yun_data.get("sekey"):
        params["sekey"] = yun_data["sekey"]

    resp = session.post(
        "https://pan.baidu.com/share/transfer",
        params=params,
        data={
            "fsidlist": json.dumps([int(file_info["fs_id"])]),
            "path": target_dir,
        },
        timeout=60,
    )
    data = resp.json()
    if not is_api_success(data):
        errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
        raise RuntimeError(f"保存分享文件到自己网盘失败: {errmsg}")
    raise_transfer_item_error(data)

    own_file = extract_transferred_file(data, file_info, target_dir)
    if own_file:
        own_file["_auto_saved_by_app"] = True
        return own_file

    print("  [提示] 转存成功，等待自己网盘索引更新...")
    own_file = wait_for_transferred_file(session, yun_data, file_info)
    if own_file:
        own_file["_auto_saved_by_app"] = True
        return own_file

    raise RuntimeError("转存后未能定位自己网盘文件，请稍后重试或检查网盘空间")


def rename_own_file(session: requests.Session, yun_data: Dict, old_path: str, new_name: str) -> str:
    params = {
        "opera": "rename",
        "async": "2",
        "onnest": "fail",
        "web": WEB,
        "app_id": APP_ID,
        "channel": CHANNEL,
        "clienttype": CLIENTTYPE,
        "bdstoken": yun_data.get("bdstoken") or "",
    }
    filelist = [{"path": old_path, "newname": new_name}]
    resp = session.post(
        "https://pan.baidu.com/api/filemanager",
        params=params,
        data={"filelist": json.dumps(filelist, ensure_ascii=False)},
        timeout=30,
    )
    data = resp.json()
    if not is_api_success(data):
        errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
        raise RuntimeError(f"重命名自己网盘文件失败: {errmsg}")

    for item in data.get("info") or []:
        if item.get("errno") not in (0, "0", None):
            errmsg = item.get("errmsg") or item.get("errno")
            raise RuntimeError(f"重命名自己网盘文件失败: {errmsg}")

    return join_cloud_path(get_cloud_parent(old_path), new_name)


def is_app_managed_cloud_path(yun_data: Dict, path: str) -> bool:
    shareid = str(yun_data.get("shareid") or "").strip()
    if not shareid:
        return False
    path = normalize_cloud_path(path)
    share_root = normalize_cloud_path(join_cloud_path(CLOUD_SAVE_ROOT, shareid))
    return path == share_root or path.startswith(f"{share_root}/")


def remember_own_pan_cleanup_path(file_info: Dict, yun_data: Dict, path: str, allow_unmanaged: bool = False) -> None:
    if not path:
        return
    normalized = normalize_cloud_path(path)
    if not allow_unmanaged and not is_app_managed_cloud_path(yun_data, normalized):
        return
    cleanup_paths = file_info.setdefault("_own_pan_cleanup_paths", [])
    for item in cleanup_paths:
        if item.get("path") == normalized:
            item["allow_unmanaged"] = item.get("allow_unmanaged") or allow_unmanaged
            return
    cleanup_paths.append({"path": normalized, "allow_unmanaged": allow_unmanaged})


def delete_own_pan_paths(
    session: requests.Session,
    yun_data: Dict,
    paths: List[str],
    allow_unmanaged: bool = False,
) -> None:
    managed_paths = []
    seen = set()
    for path in paths:
        if not path:
            continue
        normalized = normalize_cloud_path(path)
        if normalized in seen:
            continue
        if not allow_unmanaged and not is_app_managed_cloud_path(yun_data, normalized):
            continue
        seen.add(normalized)
        managed_paths.append(normalized)

    if not managed_paths:
        return

    params = {
        "opera": "delete",
        "async": "2",
        "onnest": "fail",
        "web": WEB,
        "app_id": APP_ID,
        "channel": CHANNEL,
        "clienttype": CLIENTTYPE,
        "bdstoken": yun_data.get("bdstoken") or "",
    }
    filelist = [{"path": path} for path in managed_paths]
    resp = session.post(
        "https://pan.baidu.com/api/filemanager",
        params=params,
        data={"filelist": json.dumps(filelist, ensure_ascii=False)},
        timeout=30,
    )
    data = resp.json()
    if not is_api_success(data):
        errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
        raise RuntimeError(f"删除自己网盘临时文件失败: {errmsg}")

    failed = []
    for item in data.get("info") or []:
        if item.get("errno") not in (0, "0", None):
            failed.append(str(item.get("errmsg") or item.get("errno")))
    if failed:
        raise RuntimeError(f"删除自己网盘临时文件失败: {'; '.join(failed)}")


def delete_own_pan_temp_file(
    session: requests.Session,
    yun_data: Dict,
    path: str,
    allow_unmanaged: bool = False,
) -> None:
    if not path:
        return
    if not allow_unmanaged and not is_app_managed_cloud_path(yun_data, path):
        return
    try:
        delete_own_pan_paths(session, yun_data, [path], allow_unmanaged=allow_unmanaged)
        print(f"  [清理] 已删除自己网盘临时文件: {path}")
    except Exception as exc:
        print(f"  [警告] 删除自己网盘临时文件失败: {path}: {exc}")


def cleanup_downloaded_own_pan_files(session: requests.Session, yun_data: Dict, file_info: Dict) -> None:
    cleanup_paths = file_info.get("_own_pan_cleanup_paths") or []
    if not cleanup_paths:
        return
    for item in cleanup_paths:
        delete_own_pan_temp_file(
            session,
            yun_data,
            item.get("path") or "",
            allow_unmanaged=bool(item.get("allow_unmanaged")),
        )
    file_info["_own_pan_cleanup_paths"] = []


def cleanup_own_pan_share_dir(session: requests.Session, yun_data: Dict) -> None:
    shareid = str(yun_data.get("shareid") or "").strip()
    if not shareid:
        return
    share_root = normalize_cloud_path(join_cloud_path(CLOUD_SAVE_ROOT, shareid))
    try:
        delete_own_pan_paths(session, yun_data, [share_root])
        print(f"  [清理] 已删除自己网盘临时目录: {share_root}")
    except Exception as exc:
        print(f"  [提示] 自己网盘临时目录未删除，可能仍有未完成文件: {exc}")


def search_own_file(session: requests.Session, yun_data: Dict, file_info: Dict) -> Optional[Dict]:
    expected_names = [file_info["name"], get_txt_alias_name(file_info["name"])]
    expected_names = list(dict.fromkeys(expected_names))
    expected_size = int(file_info.get("size") or 0)

    for name in expected_names:
        params = {
            "key": name,
            "recursion": "1",
            "num": "100",
            "page": "1",
            "web": WEB,
            "app_id": APP_ID,
            "channel": CHANNEL,
            "clienttype": CLIENTTYPE,
            "bdstoken": yun_data.get("bdstoken") or "",
        }
        resp = session.get("https://pan.baidu.com/api/search", params=params, timeout=30)
        data = resp.json()
        if not is_api_success(data):
            errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
            raise RuntimeError(f"搜索自己网盘文件失败: {errmsg}")

        for item in data.get("list") or []:
            if item.get("isdir") in (1, "1"):
                continue
            if item.get("server_filename") not in expected_names:
                continue
            if int(item.get("size") or 0) == expected_size:
                return item
    return None


def invalidate_pan_download_sign(yun_data: Dict) -> None:
    yun_data.pop("_pan_download_sign", None)
    yun_data.pop("_pan_download_timestamp", None)


def get_own_file_dlink(session: requests.Session, yun_data: Dict, fs_id: int) -> str:
    last_error = None
    for attempt in range(1, 3):
        sign, timestamp = get_pan_download_sign(session, yun_data, force_refresh=(attempt > 1))
        params = {
            "sign": sign,
            "timestamp": timestamp,
            "fidlist": json.dumps([int(fs_id)]),
            "type": "dlink",
            "vip": get_vip_value(yun_data),
            "web": WEB,
            "app_id": APP_ID,
            "channel": CHANNEL,
            "clienttype": PAN_DOWNLOAD_CLIENTTYPE,
            "bdstoken": yun_data.get("bdstoken") or "",
            "logid": make_session_logid(session),
        }
        resp = session.get("https://pan.baidu.com/api/download", params=params, timeout=30)
        data = resp.json()
        if is_api_success(data):
            dlink = extract_dlink(data.get("dlink") or data)
            if not dlink:
                raise RuntimeError("获取自己网盘下载链接失败: 响应中没有 dlink")
            return dlink

        errmsg = get_api_error_message(data)
        last_error = RuntimeError(f"获取自己网盘下载链接失败: {errmsg}")
        if attempt == 1 and is_download_verification_error(data):
            invalidate_pan_download_sign(yun_data)
            print("  [提示] 自己网盘下载签名已过期，正在刷新签名后重试...")
            time.sleep(1)
            continue
        raise last_error

    if last_error:
        raise last_error
    raise RuntimeError("获取自己网盘下载链接失败")


def make_pan_download_sign(sign3: str, sign1: str) -> str:
    key = [ord(sign3[i % len(sign3)]) for i in range(256)]
    box = list(range(256))

    j = 0
    for i in range(256):
        j = (j + box[i] + key[i]) % 256
        box[i], box[j] = box[j], box[i]

    output = bytearray()
    i = j = 0
    for char in sign1:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        k = box[(box[i] + box[j]) % 256]
        output.append(ord(char) ^ k)

    return base64.b64encode(bytes(output)).decode("ascii")


def get_pan_download_sign(session: requests.Session, yun_data: Dict, force_refresh: bool = False) -> Tuple[str, int]:
    if force_refresh:
        invalidate_pan_download_sign(yun_data)

    if yun_data.get("_pan_download_sign") and yun_data.get("_pan_download_timestamp"):
        return yun_data["_pan_download_sign"], int(yun_data["_pan_download_timestamp"])

    fields = ["sign1", "sign2", "sign3", "timestamp", "bdstoken"]
    params = {
        "fields": json.dumps(fields),
        "web": WEB,
        "app_id": APP_ID,
        "channel": CHANNEL,
        "clienttype": PAN_DOWNLOAD_CLIENTTYPE,
        "bdstoken": yun_data.get("bdstoken") or "",
    }
    resp = session.get("https://pan.baidu.com/api/gettemplatevariable", params=params, timeout=30)
    data = resp.json()
    if not is_api_success(data):
        errmsg = get_api_error_message(data)
        if is_download_verification_error(data):
            invalidate_pan_download_sign(yun_data)
        raise RuntimeError(f"获取自己网盘下载签名失败: {errmsg}")

    result = data.get("result") or {}
    if not result.get("sign1") or not result.get("sign3") or not result.get("timestamp"):
        raise RuntimeError("获取自己网盘下载签名失败: 响应缺少 sign1/sign3/timestamp")

    if result.get("bdstoken"):
        yun_data["bdstoken"] = result["bdstoken"]
    yun_data["_pan_download_sign"] = make_pan_download_sign(result["sign3"], result["sign1"])
    yun_data["_pan_download_timestamp"] = int(result["timestamp"])
    return yun_data["_pan_download_sign"], yun_data["_pan_download_timestamp"]


def prepare_own_file_for_download(
    session: requests.Session,
    yun_data: Dict,
    file_info: Dict,
) -> Tuple[Dict, Optional[Dict]]:
    own_file = search_own_file(session, yun_data, file_info)
    if not own_file:
        print("  [提示] 自己网盘中未找到同名同大小文件，正在自动保存分享文件...")
        own_file = save_share_file_to_own_pan(session, yun_data, file_info)
        print(f"  [保存] 已转存到自己网盘: {own_file.get('path')}")

    original_name = file_info["name"]
    txt_name = get_txt_alias_name(original_name)
    current_name = own_file.get("server_filename") or get_cloud_file_name(own_file.get("path") or "")
    restore_info = None
    cleanup_path = own_file.get("path") or ""
    cleanup_allow_unmanaged = bool(own_file.get("_auto_saved_by_app"))

    if txt_name != original_name and current_name == original_name:
        old_path = own_file["path"]
        new_path = rename_own_file(session, yun_data, old_path, txt_name)
        own_file = own_file.copy()
        own_file["path"] = new_path
        own_file["server_filename"] = txt_name
        restore_info = {"path": new_path, "newname": original_name}
        cleanup_path = old_path
        print(f"  [重命名] 自己网盘文件临时改为: {new_path}")

    remember_own_pan_cleanup_path(
        file_info,
        yun_data,
        cleanup_path,
        allow_unmanaged=cleanup_allow_unmanaged,
    )
    return own_file, restore_info


def restore_own_file_name(session: requests.Session, yun_data: Dict, restore_info: Optional[Dict]) -> None:
    if not restore_info:
        return
    try:
        restored_path = rename_own_file(session, yun_data, restore_info["path"], restore_info["newname"])
        print(f"  [恢复] 自己网盘文件名已恢复: {restored_path}")
    except Exception as exc:
        print(f"  [警告] 自己网盘文件名恢复失败: {exc}")


def get_prepared_own_download_link(
    session: requests.Session,
    yun_data: Dict,
    file_info: Dict,
) -> Tuple[str, Optional[Dict]]:
    own_file, restore_info = prepare_own_file_for_download(session, yun_data, file_info)
    print(f"  [匹配] 自己网盘文件: {own_file.get('path')}")
    try:
        return get_own_file_dlink(session, yun_data, own_file["fs_id"]), restore_info
    except Exception:
        restore_own_file_name(session, yun_data, restore_info)
        raise


def get_download_link(
    session: requests.Session,
    yun_data: Dict,
    file_info: Dict,
    prefer_embedded: bool = True,
) -> Tuple[str, Optional[Dict]]:
    if file_info.get("_use_own_pan_download"):
        print("  [提示] 使用已转存到自己网盘的文件刷新下载链接...")
        return get_prepared_own_download_link(session, yun_data, file_info)

    dlink = extract_dlink(file_info.get("dlink"))
    if prefer_embedded and dlink:
        return dlink, None

    try:
        return get_dlink(session, yun_data, file_info["fs_id"]), None
    except NoRetryError as exc:
        print(f"  [提示] 分享直链不可用: {exc}")
        print("  [提示] 尝试从已转存到自己网盘的同名文件下载...")

    file_info["_use_own_pan_download"] = True
    return get_prepared_own_download_link(session, yun_data, file_info)


def clone_session(session: requests.Session, trust_env: Optional[bool] = None) -> requests.Session:
    cloned = requests.Session()
    cloned.trust_env = session.trust_env if trust_env is None else trust_env
    cloned.headers.update(session.headers)
    for cookie in session.cookies:
        cloned.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return cloned


def get_dlink(session: requests.Session, yun_data: Dict, fs_id: int) -> str:
    """获取单个文件的下载直链"""
    if not yun_data.get("sign") or not yun_data.get("timestamp"):
        raise RuntimeError("当前分享页未暴露 sign/timestamp，暂时无法调用 sharedownload 获取直链")

    params = {
        "sign": yun_data["sign"],
        "timestamp": yun_data["timestamp"],
        "app_id": APP_ID,
        "channel": CHANNEL,
        "clienttype": CLIENTTYPE,
        "web": WEB,
        "bdstoken": yun_data.get("bdstoken") or "",
        "logid": make_session_logid(session),
    }
    if yun_data.get("jsToken"):
        params["jsToken"] = yun_data["jsToken"]

    post_data = {
        "encrypt": "0",
        "product": "share",
        "uk": yun_data.get("share_uk") or yun_data["uk"],
        "primaryid": yun_data["shareid"],
        "fid_list": f"[{fs_id}]",
        "path_list": "",
        "type": "nolimit",
        "vip": get_vip_value(yun_data),
    }
    if yun_data.get("sekey"):
        post_data["extra"] = json.dumps({"sekey": yun_data["sekey"]}, ensure_ascii=False)

    @retry(times=3)
    def request_dlink():
        resp = session.post("https://pan.baidu.com/api/sharedownload", params=params, data=post_data, timeout=30)
        data = resp.json()
        if not is_api_success(data):
            errmsg = get_api_error_message(data)
            if is_download_verification_error(data):
                raise NoRetryError(f"下载接口返回验证错误：{errmsg}；改用自己网盘文件下载")
            raise RuntimeError(f"获取下载链接失败: {errmsg}")

        dlink = extract_dlink(data)
        if not dlink:
            if is_encrypted_client_task(data.get("list")):
                raise NoRetryError(
                    "百度返回的是客户端加密下载任务，不是浏览器直链；该文件可能过大或被限制为必须使用百度网盘客户端/转存后下载"
                )
            raise RuntimeError("获取下载链接失败: 响应中没有 dlink")
        return dlink

    return request_dlink()


def parse_content_range(value: str) -> Optional[Tuple[int, int, Optional[int]]]:
    if not value:
        return None
    match = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value, re.IGNORECASE)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    return start, end, total


def parse_content_length(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def get_download_part_dir(full_path: str) -> str:
    return f"{full_path}.parts"


def get_part_file_path(part_dir: str, index: int) -> str:
    return os.path.join(part_dir, f"{index:05d}.part")


def build_download_part_ranges(file_size: int) -> List[Tuple[int, int, int]]:
    ranges = []
    start = 0
    index = 0
    while start < file_size:
        end = min(start + DOWNLOAD_PART_SIZE - 1, file_size - 1)
        ranges.append((index, start, end))
        start = end + 1
        index += 1
    return ranges


def has_part_files(part_dir: str) -> bool:
    if not os.path.isdir(part_dir):
        return False
    return any(name.endswith(".part") for name in os.listdir(part_dir))


def sum_part_file_sizes(part_dir: str, file_size: int = 0) -> int:
    if not os.path.isdir(part_dir):
        return 0

    total = 0
    for name in os.listdir(part_dir):
        if not name.endswith(".part"):
            continue
        path = os.path.join(part_dir, name)
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return min(total, file_size) if file_size > 0 else total


def get_partial_download_size(full_path: str, file_size: int = 0) -> int:
    part_dir = get_download_part_dir(full_path)
    part_size = sum_part_file_sizes(part_dir, file_size)
    if part_size > 0:
        return part_size
    if os.path.exists(full_path):
        try:
            size = os.path.getsize(full_path)
        except OSError:
            return 0
        return min(size, file_size) if file_size > 0 else size
    return 0


def copy_limited(src, dst, size: int) -> None:
    remaining = size
    while remaining > 0:
        chunk = src.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        dst.write(chunk)
        remaining -= len(chunk)


def prepare_parallel_part_cache(full_path: str, file_size: int, ranges: List[Tuple[int, int, int]]) -> str:
    part_dir = get_download_part_dir(full_path)
    os.makedirs(part_dir, exist_ok=True)

    for index, start, end in ranges:
        part_path = get_part_file_path(part_dir, index)
        expected = end - start + 1
        if os.path.exists(part_path) and os.path.getsize(part_path) > expected:
            os.remove(part_path)

    if not os.path.exists(full_path):
        return part_dir

    existing_size = os.path.getsize(full_path)
    if existing_size == file_size:
        return part_dir
    if existing_size > file_size:
        os.remove(full_path)
        return part_dir
    if existing_size <= 0:
        return part_dir

    if has_part_files(part_dir):
        print("  [提示] 已存在分段缓存，忽略未完成的目标文件并继续分段续传")
        return part_dir

    print(f"  [续传] 将已有文件缓存转换为分段缓存: {format_size(existing_size)}")
    remaining = existing_size
    with open(full_path, "rb") as src:
        for index, start, end in ranges:
            if remaining <= 0:
                break
            expected = end - start + 1
            to_copy = min(expected, remaining)
            part_path = get_part_file_path(part_dir, index)
            with open(part_path, "wb") as dst:
                copy_limited(src, dst, to_copy)
            remaining -= to_copy
    os.remove(full_path)
    return part_dir


def probe_download_size(session: requests.Session, dlink: str, headers: Dict[str, str], declared_size: int) -> int:
    probe_headers = headers.copy()
    probe_headers["Range"] = "bytes=0-0"
    try:
        resp = session.get(dlink, headers=probe_headers, stream=True, timeout=DOWNLOAD_REQUEST_TIMEOUT)
    except Exception as exc:
        print(f"  [警告] 无法探测远端文件大小，将使用列表大小: {exc}")
        return declared_size

    try:
        if resp.status_code not in (200, 206):
            return declared_size

        content_range = parse_content_range(resp.headers.get("Content-Range", ""))
        if content_range and content_range[2]:
            return max(declared_size, content_range[2])

        content_length = parse_content_length(resp.headers.get("Content-Length"))
        if resp.status_code == 200 and content_length:
            return max(declared_size, content_length)
    finally:
        resp.close()

    return declared_size


def download_file_sequential(
    session: requests.Session,
    dlink: str,
    save_path: str,
    file_name: str,
    file_size: int,
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
):
    """带断点续传的文件下载"""
    raise_if_cancelled(cancel_event)
    full_path = os.path.join(save_path, file_name)
    os.makedirs(save_path, exist_ok=True)

    headers = HEADERS.copy()
    headers["Referer"] = "https://pan.baidu.com/disk/home"
    headers["User-Agent"] = DOWNLOAD_USER_AGENT
    if not session.trust_env:
        print("  [提示] 文件直链下载不使用系统代理")

    declared_size = int(file_size or 0)
    file_size = probe_download_size(session, dlink, headers, declared_size)
    if declared_size and file_size != declared_size:
        print(f"  [校正] {file_name} 文件大小: 列表 {format_size(declared_size)}，远端 {format_size(file_size)}")

    if file_size == 0:
        open(full_path, "ab").close()
        if progress_callback:
            progress_callback(0, 0, 0.0, "完成")
        print(f"  [完成] {file_name}")
        return

    # 文件已完整，跳过
    if os.path.exists(full_path) and os.path.getsize(full_path) == file_size:
        if progress_callback:
            progress_callback(file_size, file_size, 0.0, "完成")
        print(f"  [跳过] {file_name} 已存在且完整")
        return

    # 检查已有部分，构造 Range 请求
    downloaded_bytes = 0
    if os.path.exists(full_path):
        downloaded_bytes = os.path.getsize(full_path)
        if downloaded_bytes > file_size:
            # 本地文件异常，删除重新下载
            os.remove(full_path)
            downloaded_bytes = 0

    print(f"  [下载] {file_name} ({format_size(file_size)})", end="")
    if downloaded_bytes > 0:
        print(f"，从 {downloaded_bytes} 字节处续传", end="")
    print()

    last_report_time = time.monotonic()
    last_report_bytes = downloaded_bytes

    def emit_progress(current: int, total: int, status: str, force: bool = False) -> None:
        nonlocal last_report_time, last_report_bytes
        if not progress_callback:
            return
        now = time.monotonic()
        elapsed = now - last_report_time
        if not force and elapsed < 1 and current < total:
            return
        speed = 0.0
        if elapsed > 0:
            speed = max(0.0, (current - last_report_bytes) / elapsed)
        last_report_time = now
        last_report_bytes = current
        progress_callback(current, total, speed, status)

    @retry(times=3)
    def do_download():
        nonlocal file_size
        current = os.path.getsize(full_path) if os.path.exists(full_path) else 0
        if current > file_size:
            os.remove(full_path)
            current = 0

        mode = "ab" if current > 0 else "wb"
        progress = None
        emit_progress(current, file_size, "下载中", force=True)
        with open(full_path, mode) as f:
            if USE_PROGRESS_BAR:
                progress = tqdm(total=file_size, initial=current, unit="B", unit_scale=True, desc=file_name)
            try:
                while current < file_size:
                    raise_if_cancelled(cancel_event)
                    start = current
                    end = min(start + DOWNLOAD_RANGE_SIZE - 1, file_size - 1)
                    chunk_headers = headers.copy()
                    chunk_headers["Range"] = f"bytes={start}-{end}"

                    resp = session.get(dlink, headers=chunk_headers, stream=True, timeout=DOWNLOAD_REQUEST_TIMEOUT)
                    try:
                        if resp.status_code not in (200, 206):
                            resp.raise_for_status()
                        if resp.status_code == 200 and start != 0:
                            raise RuntimeError(f"服务器忽略续传 Range: HTTP {resp.status_code}")

                        content_range = parse_content_range(resp.headers.get("Content-Range", ""))
                        if resp.status_code == 206:
                            if not content_range:
                                raise RuntimeError("服务器返回 206 但缺少 Content-Range")
                            range_start, range_end, range_total = content_range
                            if range_start != start:
                                raise RuntimeError(f"服务器返回分段起点异常: 期望 {start}，实际 {range_start}")
                            if range_total and range_total > file_size:
                                file_size = range_total
                                if progress:
                                    progress.total = file_size
                                    progress.refresh()
                            expected = range_end - range_start + 1
                        else:
                            content_length = parse_content_length(resp.headers.get("Content-Length"))
                            if content_length and content_length > file_size:
                                file_size = content_length
                                if progress:
                                    progress.total = file_size
                                    progress.refresh()
                            expected = file_size if start == 0 else end - start + 1

                        written = 0
                        for chunk in resp.iter_content(chunk_size=1024 * 1024):
                            raise_if_cancelled(cancel_event)
                            if chunk:
                                f.write(chunk)
                                written += len(chunk)
                                current += len(chunk)
                                if progress:
                                    progress.update(len(chunk))
                                emit_progress(current, file_size, "下载中")

                        if written != expected:
                            raise RuntimeError(f"分段下载不完整: 预期 {expected}，实际 {written}")
                        if resp.status_code == 200 and current < file_size:
                            raise RuntimeError(f"服务器返回完整响应但文件不完整: 当前 {current}，预期 {file_size}")
                    finally:
                        resp.close()
            finally:
                if progress:
                    progress.close()

        actual_size = os.path.getsize(full_path)
        if actual_size != file_size:
            raise RuntimeError(f"文件大小校验失败: 预期 {file_size}，实际 {actual_size}")
        emit_progress(actual_size, file_size, "完成", force=True)

    do_download()
    print(f"  [完成] {file_name}")


def download_part_range(
    session_template: requests.Session,
    dlink: str,
    headers: Dict[str, str],
    part_dir: str,
    part_index: int,
    start: int,
    end: int,
    progress_callback: Callable[[int, int], None],
    stop_event: threading.Event,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    part_session = clone_session(session_template)
    part_path = get_part_file_path(part_dir, part_index)
    expected_size = end - start + 1

    current = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    if current > expected_size:
        os.remove(part_path)
        current = 0
    if current == expected_size:
        return

    no_progress_failures = 0
    with open(part_path, "ab") as f:
        while current < expected_size:
            raise_if_cancelled(cancel_event)
            if stop_event.is_set():
                raise DownloadCancelled("下载任务已取消")

            absolute_start = start + current
            absolute_end = min(absolute_start + DOWNLOAD_RANGE_SIZE - 1, end)
            chunk_headers = headers.copy()
            chunk_headers["Range"] = f"bytes={absolute_start}-{absolute_end}"

            before_request = current
            resp = None
            try:
                resp = part_session.get(dlink, headers=chunk_headers, stream=True, timeout=DOWNLOAD_REQUEST_TIMEOUT)
                if resp.status_code != 206:
                    if resp.status_code == 200:
                        raise RuntimeError("服务器忽略分段 Range，无法多线程下载")
                    resp.raise_for_status()

                content_range = parse_content_range(resp.headers.get("Content-Range", ""))
                if not content_range:
                    raise RuntimeError("服务器返回 206 但缺少 Content-Range")
                range_start, range_end, _range_total = content_range
                if range_start != absolute_start:
                    raise RuntimeError(f"服务器返回分段起点异常: 期望 {absolute_start}，实际 {range_start}")
                if range_end > absolute_end or range_end > end:
                    raise RuntimeError(f"服务器返回分段终点异常: 期望不超过 {absolute_end}，实际 {range_end}")

                expected_chunk_size = range_end - range_start + 1
                written = 0
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    raise_if_cancelled(cancel_event)
                    if stop_event.is_set():
                        raise DownloadCancelled("下载任务已取消")
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        current += len(chunk)
                        progress_callback(part_index, len(chunk))

                if written != expected_chunk_size:
                    if written > 0:
                        print(
                            f"  [提示] 分片 {part_index + 1} 连接提前结束，已保存 {format_size(written)}，继续续传..."
                        )
                        no_progress_failures = 0
                        continue
                    raise RuntimeError(f"分段下载不完整: 预期 {expected_chunk_size}，实际 {written}")
                no_progress_failures = 0
            except Exception as exc:
                if stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                    raise
                if current > before_request:
                    no_progress_failures = 0
                    print(
                        f"  [提示] 分片 {part_index + 1} 连接中断，已保存 {format_size(current - before_request)}，继续续传: {exc}"
                    )
                    sleep_or_cancel(cancel_event, DOWNLOAD_PART_RETRY_DELAY)
                    continue

                no_progress_failures += 1
                if no_progress_failures < DOWNLOAD_PART_REQUEST_RETRIES:
                    wait_seconds = DOWNLOAD_PART_RETRY_DELAY * no_progress_failures
                    print(
                        f"  [提示] 分片 {part_index + 1} 连接失败，第 {no_progress_failures}/{DOWNLOAD_PART_REQUEST_RETRIES} 次重试: {exc}"
                    )
                    sleep_or_cancel(cancel_event, wait_seconds)
                    continue
                raise
            finally:
                if resp is not None:
                    resp.close()

    actual_size = os.path.getsize(part_path)
    if actual_size != expected_size:
        raise RuntimeError(f"分片大小校验失败: 预期 {expected_size}，实际 {actual_size}")


def merge_download_parts(
    full_path: str,
    part_dir: str,
    ranges: List[Tuple[int, int, int]],
    file_size: int,
) -> None:
    assembling_path = f"{full_path}.assembling"
    if os.path.exists(assembling_path):
        os.remove(assembling_path)

    with open(assembling_path, "wb") as dst:
        for index, start, end in ranges:
            part_path = get_part_file_path(part_dir, index)
            expected_size = end - start + 1
            if not os.path.exists(part_path):
                raise RuntimeError(f"缺少下载分片: {index}")
            actual_size = os.path.getsize(part_path)
            if actual_size != expected_size:
                raise RuntimeError(f"分片大小异常 {index}: 预期 {expected_size}，实际 {actual_size}")
            with open(part_path, "rb") as src:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

    actual_size = os.path.getsize(assembling_path)
    if actual_size != file_size:
        raise RuntimeError(f"合并后文件大小校验失败: 预期 {file_size}，实际 {actual_size}")

    os.replace(assembling_path, full_path)
    shutil.rmtree(part_dir, ignore_errors=True)


def download_file_parallel(
    session: requests.Session,
    dlink: str,
    save_path: str,
    file_name: str,
    file_size: int,
    part_workers: int,
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    raise_if_cancelled(cancel_event)
    full_path = os.path.join(save_path, file_name)
    os.makedirs(save_path, exist_ok=True)

    headers = HEADERS.copy()
    headers["Referer"] = "https://pan.baidu.com/disk/home"
    headers["User-Agent"] = DOWNLOAD_USER_AGENT
    if not session.trust_env:
        print("  [提示] 文件直链下载不使用系统代理")

    declared_size = int(file_size or 0)
    file_size = probe_download_size(session, dlink, headers, declared_size)
    raise_if_cancelled(cancel_event)
    if declared_size and file_size != declared_size:
        print(f"  [校正] {file_name} 文件大小: 列表 {format_size(declared_size)}，远端 {format_size(file_size)}")

    if file_size == 0:
        open(full_path, "ab").close()
        if progress_callback:
            progress_callback(0, 0, 0.0, "完成")
        print(f"  [完成] {file_name}")
        return

    part_dir = get_download_part_dir(full_path)
    if os.path.exists(full_path) and os.path.getsize(full_path) == file_size:
        if os.path.isdir(part_dir):
            shutil.rmtree(part_dir, ignore_errors=True)
        if progress_callback:
            progress_callback(file_size, file_size, 0.0, "完成")
        print(f"  [跳过] {file_name} 已存在且完整")
        return

    ranges = build_download_part_ranges(file_size)
    part_workers = min(part_workers, len(ranges))
    downloaded_bytes = get_partial_download_size(full_path, file_size)
    print(f"  [下载] {file_name} ({format_size(file_size)})，分段线程 {part_workers}", end="")
    if downloaded_bytes > 0:
        print(f"，从 {downloaded_bytes} 字节处续传", end="")
    print()

    part_dir = prepare_parallel_part_cache(full_path, file_size, ranges)

    part_progress: Dict[int, int] = {}
    part_expected: Dict[int, int] = {}
    for index, start, end in ranges:
        part_path = get_part_file_path(part_dir, index)
        expected_size = end - start + 1
        part_expected[index] = expected_size
        size = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        part_progress[index] = min(size, expected_size)

    progress_lock = threading.Lock()
    last_report_time = time.monotonic()
    last_report_bytes = sum(part_progress.values())

    def emit_progress(current: int, total: int, status: str, force: bool = False) -> None:
        nonlocal last_report_time, last_report_bytes
        if not progress_callback:
            return
        now = time.monotonic()
        elapsed = now - last_report_time
        if not force and elapsed < 1 and current < total:
            return
        speed = 0.0
        if elapsed > 0:
            speed = max(0.0, (current - last_report_bytes) / elapsed)
        last_report_time = now
        last_report_bytes = current
        progress_callback(current, total, speed, status)

    def update_part_progress(part_index: int, delta: int) -> None:
        with progress_lock:
            expected_size = part_expected.get(part_index, file_size)
            part_progress[part_index] = min(expected_size, part_progress.get(part_index, 0) + delta)
            current_total = sum(part_progress.values())
            emit_progress(current_total, file_size, "下载中")

    with progress_lock:
        emit_progress(sum(part_progress.values()), file_size, "下载中", force=True)

    pending_ranges = []
    for index, start, end in ranges:
        raise_if_cancelled(cancel_event)
        expected_size = end - start + 1
        if part_progress.get(index, 0) < expected_size:
            pending_ranges.append((index, start, end))

    stop_event = threading.Event()
    if pending_ranges:
        with ThreadPoolExecutor(max_workers=part_workers) as executor:
            future_map = {
                executor.submit(
                    download_part_range,
                    session,
                    dlink,
                    headers,
                    part_dir,
                    index,
                    start,
                    end,
                    update_part_progress,
                    stop_event,
                    cancel_event,
                ): index
                for index, start, end in pending_ranges
            }
            for future in as_completed(future_map):
                try:
                    raise_if_cancelled(cancel_event)
                    future.result()
                except Exception:
                    stop_event.set()
                    for pending in future_map:
                        pending.cancel()
                    raise

    raise_if_cancelled(cancel_event)
    with progress_lock:
        emit_progress(file_size, file_size, "合并中", force=True)
    print(f"  [合并] {file_name}")
    merge_download_parts(full_path, part_dir, ranges, file_size)
    if progress_callback:
        progress_callback(file_size, file_size, 0.0, "完成")
    print(f"  [完成] {file_name}")


def download_file(
    session: requests.Session,
    dlink: str,
    save_path: str,
    file_name: str,
    file_size: int,
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None,
    part_workers: int = DOWNLOAD_PART_WORKERS,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    raise_if_cancelled(cancel_event)
    part_workers = clamp_int(part_workers, DOWNLOAD_PART_WORKERS, 1, DOWNLOAD_MAX_PART_WORKERS)
    if part_workers <= 1:
        download_file_sequential(session, dlink, save_path, file_name, file_size, progress_callback, cancel_event)
        return
    download_file_parallel(session, dlink, save_path, file_name, file_size, part_workers, progress_callback, cancel_event)


def download_file_with_link_refresh(
    link_session: requests.Session,
    yun_data: Dict,
    file_info: Dict,
    save_dir: str,
    file_name: str,
    link_lock: Optional[threading.Lock] = None,
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None,
    part_workers: int = DOWNLOAD_PART_WORKERS,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    restore_infos = []
    last_error = None
    full_path = os.path.join(save_dir, file_name)

    def existing_size() -> int:
        return get_partial_download_size(full_path, int(file_info.get("size") or 0))

    def emit_status(status: str) -> None:
        if progress_callback:
            progress_callback(existing_size(), int(file_info.get("size") or 0), 0.0, status)

    try:
        link_attempt = 0
        failures_without_progress = 0
        while failures_without_progress < DOWNLOAD_LINK_REFRESH_RETRIES:
            raise_if_cancelled(cancel_event)
            link_attempt += 1
            size_before_attempt = existing_size()
            restore_info = None
            try:
                emit_status("获取链接")
                if link_lock:
                    with link_lock:
                        dlink, restore_info = get_download_link(
                            link_session,
                            yun_data,
                            file_info,
                            prefer_embedded=(link_attempt == 1),
                        )
                else:
                    dlink, restore_info = get_download_link(
                        link_session,
                        yun_data,
                        file_info,
                        prefer_embedded=(link_attempt == 1),
                    )
                restore_infos.append(restore_info)

                download_session = clone_session(link_session, trust_env=DOWNLOAD_USE_ENV_PROXY)
                download_file(download_session, dlink, save_dir, file_name, file_info["size"], progress_callback, part_workers, cancel_event)
                return
            except Exception as exc:
                if isinstance(exc, DownloadCancelled):
                    emit_status("已取消")
                    raise
                last_error = exc
                size_after_attempt = existing_size()
                made_progress = size_after_attempt > size_before_attempt
                if made_progress:
                    failures_without_progress = 0
                else:
                    failures_without_progress += 1

                if failures_without_progress >= DOWNLOAD_LINK_REFRESH_RETRIES:
                    emit_status("失败")
                    raise
                emit_status("续传" if made_progress else "重试")
                progress_note = f"，已保存 {format_size(size_after_attempt)}" if size_after_attempt else ""
                print(
                    f"  [提示] 下载中断，刷新下载链接后续传 {file_name}{progress_note}"
                    f"（连续无进展 {failures_without_progress}/{DOWNLOAD_LINK_REFRESH_RETRIES}）: {exc}"
                )
                sleep_or_cancel(cancel_event, min(2 + failures_without_progress, 10))
        if last_error:
            raise last_error
    finally:
        seen_restore_keys = set()
        for restore_info in reversed(restore_infos):
            if not restore_info:
                continue
            key = (restore_info.get("path"), restore_info.get("newname"))
            if key in seen_restore_keys:
                continue
            seen_restore_keys.add(key)
            restore_own_file_name(link_session, yun_data, restore_info)


def format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} TB"


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


class FletPanDownloaderApp:
    def __init__(self, page, ft_module):
        self.page = page
        self.ft = ft_module
        self.ui_queue = queue.Queue()
        self.session = None
        self.yun_data = None
        self.files_to_download = []
        self.file_item_map = {}
        self.file_selected_map = {}
        self.file_row_tag_map = {}
        self.file_progress_bytes = {}
        self.file_total_bytes = {}
        self.file_speed_bytes = {}
        self.file_row_controls = {}
        self.selection_buttons = []
        self.worker = None
        self.cancel_event = threading.Event()
        self.active_task = None
        self.download_paused = False
        self.download_stop_reason = ""
        self.closing = False
        self.exit_timer_started = False
        self.force_exit_timer = None
        self.queue_task = None
        self.busy = False
        self.queue_running = True
        self.last_progress_update_time = 0.0

        self.configure_page()
        self.build_ui()
        self.append_startup_credential_status()
        self.start_queue_pump()

    def configure_page(self) -> None:
        ft = self.ft
        self.page.title = "轻云链"
        self.page.padding = 0
        self.page.bgcolor = "#f5f7fb"
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.scroll = ft.ScrollMode.HIDDEN
        self.page.on_resize = lambda event: self.update_responsive_layout(getattr(event, "height", None))
        self.page.on_close = None
        self.page.on_disconnect = None
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

        self.status_text = ft.Text("未登录" if not BDUSS else "已登录", size=13, weight=ft.FontWeight.BOLD, color="#1d4ed8")
        self.summary_text = ft.Text("等待解析分享", size=13, color="#64748b", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.total_speed_text = ft.Text("总速度 -", size=13, weight=ft.FontWeight.BOLD, color="#334155")
        self.overall_progress = ft.ProgressBar(value=0, height=8, color="#2563eb", bgcolor="#e2e8f0")

        self.share_url_field = ft.TextField(
            label="分享文本",
            hint_text="粘贴百度网盘分享链接或完整分享文本",
            expand=True,
            border_radius=10,
            prefix_icon=ft.Icons.LINK,
            filled=True,
            bgcolor="#ffffff",
        )
        self.download_root_field = ft.TextField(
            label="保存位置",
            value=DOWNLOAD_ROOT,
            expand=True,
            border_radius=10,
            prefix_icon=ft.Icons.FOLDER_OPEN,
            filled=True,
            bgcolor="#ffffff",
        )
        self.file_workers_field = ft.TextField(
            label="文件并发",
            value=str(MAX_WORKERS),
            width=96,
            border_radius=10,
            text_align=ft.TextAlign.CENTER,
            filled=True,
            bgcolor="#ffffff",
        )
        self.part_workers_field = ft.TextField(
            label="单文件线程",
            value=str(DOWNLOAD_PART_WORKERS),
            width=112,
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
                        [
                            ft.Text("轻云链", size=24, weight=ft.FontWeight.BOLD, color="#111827"),
                            self.summary_text,
                        ],
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
                            ft.IconButton(
                                icon=ft.Icons.FOLDER_OPEN,
                                tooltip="选择保存目录",
                                on_click=lambda _e: self.choose_download_dir(),
                            ),
                            self.file_workers_field,
                            self.part_workers_field,
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            self.parse_button,
                            self.download_button,
                            self.pause_button,
                            self.resume_button,
                            self.reselect_button,
                        ],
                        spacing=10,
                        wrap=True,
                    ),
                    ft.Text(
                        "暂停或重新选择会停止当前连接；未完成的 .parts 分片会保留，继续下载会自动续传。",
                        size=12,
                        color="#64748b",
                    ),
                ],
                spacing=12,
            ),
        )

        content_area_height, file_list_height, log_list_height = self.calculate_content_heights()

        self.file_list = ft.ListView(
            spacing=0,
            padding=0,
            auto_scroll=False,
            scroll=ft.ScrollMode.ALWAYS,
            height=file_list_height,
            build_controls_on_demand=False,
        )
        file_list_viewport = ft.Container(
            content=self.file_list,
            height=file_list_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
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
                            [
                                ft.Text("文件列表", size=16, weight=ft.FontWeight.BOLD, color="#111827", expand=True),
                                self.select_all_button,
                                self.clear_selection_button,
                                self.invert_selection_button,
                            ],
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

        self.log_list = ft.ListView(
            spacing=6,
            padding=12,
            auto_scroll=True,
            scroll=ft.ScrollMode.ALWAYS,
            height=log_list_height,
            build_controls_on_demand=False,
        )
        log_list_viewport = ft.Container(
            content=self.log_list,
            height=log_list_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
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
                        content=ft.Row(
                            [
                                ft.Icon(ft.Icons.TERMINAL, color="#2563eb", size=18),
                                ft.Text("任务日志", size=16, weight=ft.FontWeight.BOLD, color="#111827"),
                            ],
                            spacing=8,
                        ),
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
                [
                    ft.Text("总进度", size=13, weight=ft.FontWeight.BOLD, color="#334155"),
                    ft.Container(content=self.overall_progress, expand=True),
                    self.total_speed_text,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        content_area = ft.Container(
            height=content_area_height,
            content=ft.Row(
                [files_panel, log_panel],
                expand=True,
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )
        self.content_area_container = content_area
        self.files_panel = files_panel
        self.log_panel = log_panel
        self.file_list_viewport = file_list_viewport
        self.log_list_viewport = log_list_viewport
        body = ft.Container(
            expand=True,
            padding=ft.Padding(left=22, top=18, right=22, bottom=14),
            content=ft.Column(
                [
                    controls_panel,
                    content_area,
                ],
                expand=True,
                spacing=14,
            ),
        )

        self.page.add(ft.Column([header, body, footer], expand=True, spacing=0))
        self.update_responsive_layout()
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

    def process_queue_batch(self) -> None:
        changed = False
        processed = 0
        deadline = time.monotonic() + 0.05
        while True:
            if processed >= 100 or time.monotonic() >= deadline:
                break
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

    def on_window_event(self, event) -> None:
        event_type = getattr(event, "type", None)
        close_type = getattr(getattr(self.ft, "WindowEventType", object), "CLOSE", None)
        if event_type == close_type or getattr(event_type, "value", None) == "close" or getattr(event, "data", "") == "close":
            self.on_close()

    def force_exit_after_close(self) -> None:
        os._exit(0)

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
        self.append_log(f"[配置] 凭据文件: {CREDENTIALS_FILE}")
        if CREDENTIALS_LOAD_ERROR:
            self.append_log(f"[配置] 凭据读取失败: {CREDENTIALS_LOAD_ERROR}")
        elif BDUSS:
            self.append_log("[配置] 已读取 BDUSS。")
        else:
            self.append_log("[配置] 未读取到 BDUSS，解析前会打开网页登录。")
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
            "清理": "CLEAN",
            "保存": "SAVE",
            "匹配": "PAN",
            "重命名": "PAN",
            "恢复": "PAN",
            "下载": "DOWN",
            "续传": "DOWN",
            "合并": "DOWN",
            "提示": "INFO",
            "配置": "CONFIG",
            "校正": "INFO",
        }

        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", message)
        if match:
            label = match.group(1).strip()
            level = labels.get(label, "INFO")
            clean_message = match.group(2).strip() or message
            return level, clean_message
        return "INFO", message

    def log_level_style(self, level: str) -> Tuple[str, str]:
        styles = {
            "ERROR": ("#fee2e2", "#991b1b"),
            "WARN": ("#fef3c7", "#92400e"),
            "STOP": ("#fee2e2", "#991b1b"),
            "PAUSE": ("#fef3c7", "#92400e"),
            "OK": ("#dcfce7", "#166534"),
            "CLEAN": ("#d1fae5", "#047857"),
            "SAVE": ("#e0e7ff", "#3730a3"),
            "PAN": ("#ede9fe", "#6d28d9"),
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
                        ft.Container(
                            width=60,
                            bgcolor=badge_bg,
                            border_radius=6,
                            padding=ft.Padding(left=6, top=2, right=6, bottom=2),
                            content=ft.Text(level, size=10, weight=ft.FontWeight.BOLD, color=badge_fg, text_align=ft.TextAlign.CENTER),
                        ),
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
        def worker():
            try:
                import tkinter as tk
                from tkinter import filedialog

                initial_dir = self.download_root_field.value or DOWNLOAD_ROOT
                if not os.path.isdir(initial_dir):
                    initial_dir = DOWNLOAD_ROOT if os.path.isdir(DOWNLOAD_ROOT) else os.path.expanduser("~")
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
        file_workers = clamp_int(self.file_workers_field.value, MAX_WORKERS, 1, MAX_FILE_WORKERS)
        part_workers = clamp_int(self.part_workers_field.value, DOWNLOAD_PART_WORKERS, 1, DOWNLOAD_MAX_PART_WORKERS)
        self.file_workers_field.value = str(file_workers)
        self.part_workers_field.value = str(part_workers)
        return file_workers, part_workers

    def get_selected_files(self) -> List[Dict]:
        return [file_info for file_info in self.files_to_download if self.file_selected_map.get(file_info["path"])]

    def update_selection_summary(self) -> None:
        total_files = len(self.files_to_download)
        selected_files = self.get_selected_files()
        selected_size = sum(int(item.get("size") or 0) for item in selected_files)
        total_size = sum(int(item.get("size") or 0) for item in self.files_to_download)
        if total_files:
            self.summary_text.value = (
                f"已选择 {len(selected_files)}/{total_files} 个文件，选中 {format_size(selected_size)} / 合计 {format_size(total_size)}"
            )
        else:
            self.summary_text.value = "等待解析分享"

    def update_download_button_state(self) -> None:
        selected_count = len(self.get_selected_files())
        is_download_task = self.active_task == "download"
        can_select = bool(self.files_to_download) and not self.busy
        can_start = selected_count > 0 and not self.busy and not self.download_paused
        can_pause = self.busy and is_download_task and not self.cancel_event.is_set()
        can_resume = selected_count > 0 and not self.busy and self.download_paused
        can_reselect = bool(self.files_to_download) and (not self.busy or is_download_task)

        self.download_button.disabled = not can_start
        self.pause_button.disabled = not can_pause
        self.resume_button.disabled = not can_resume
        self.reselect_button.disabled = not can_reselect
        for button in self.selection_buttons:
            button.disabled = not can_select

    def status_colors(self, status: str) -> Tuple[str, str]:
        if status in ("完成", "已登录"):
            return "#dcfce7", "#166534"
        if status in ("失败", "已停止"):
            return "#fee2e2", "#991b1b"
        if status in ("下载中", "获取链接", "续传", "重试", "合并中"):
            return "#dbeafe", "#1d4ed8"
        if status in ("已暂停",):
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
        for file_info in self.files_to_download:
            self.set_file_selected(file_info["path"], True)
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def clear_file_selection(self) -> None:
        for file_info in self.files_to_download:
            self.set_file_selected(file_info["path"], False)
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def invert_file_selection(self) -> None:
        for file_info in self.files_to_download:
            path = file_info["path"]
            self.set_file_selected(path, not self.file_selected_map.get(path, False))
        self.update_selection_summary()
        self.update_download_button_state()
        self.safe_update()

    def make_file_row(self, file_info: Dict, index: int):
        ft = self.ft
        path = file_info["path"]
        size = int(file_info.get("size") or 0)
        row_bg = "#ffffff" if index % 2 == 0 else "#f8fafc"
        mark = ft.Text(FILE_UNSELECTED_MARK, width=34, size=18, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER)
        progress_bar = ft.ProgressBar(value=0, width=110, height=6, color="#2563eb", bgcolor="#e2e8f0")
        progress_text = ft.Text("0.0%", width=64, size=12, color="#475569", text_align=ft.TextAlign.RIGHT)
        status_bg, status_fg = self.status_colors("未选择")
        status = ft.Container(
            content=ft.Text("未选择", size=12, color=status_fg, text_align=ft.TextAlign.CENTER),
            bgcolor=status_bg,
            border_radius=999,
            padding=ft.Padding(left=10, top=4, right=10, bottom=4),
            width=92,
        )
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
        self.file_row_controls[path] = {
            "container": row,
            "mark": mark,
            "progress_bar": progress_bar,
            "progress_text": progress_text,
            "speed": row.content.controls[4],
            "status": status,
        }
        self.file_row_tag_map[path] = row_bg
        return row

    def show_files(self, files: List[Dict], dirs: List[Dict]) -> None:
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
        if total_files:
            self.summary_text.value = f"正在显示文件列表 0/{total_files}"
            self.safe_update()
        for index, file_info in enumerate(files):
            path = file_info["path"]
            size = int(file_info.get("size") or 0)
            row = self.make_file_row(file_info, index)
            self.file_list.controls.append(row)
            self.file_item_map[path] = row
            self.file_selected_map[path] = False
            self.file_progress_bytes[path] = 0
            self.file_total_bytes[path] = size
            self.file_speed_bytes[path] = 0.0
            if (index + 1) % 100 == 0:
                self.summary_text.value = f"正在显示文件列表 {index + 1}/{total_files}"
                self.safe_update()
        self.update_selection_summary()
        self.total_speed_text.value = "总速度 -"
        self.overall_progress.value = 0
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

        tracked_paths = [item for item in self.file_total_bytes if self.file_selected_map.get(item)]
        if not tracked_paths:
            tracked_paths = list(self.file_total_bytes)
        total_bytes = sum(max(1, self.file_total_bytes.get(item, 0)) for item in tracked_paths)
        done_bytes = sum(min(self.file_progress_bytes.get(item, 0), self.file_total_bytes.get(item, 0)) for item in tracked_paths)
        overall_percent = 1.0 if total_bytes <= 0 else max(0.0, min(1.0, done_bytes / total_bytes))
        self.overall_progress.value = overall_percent
        total_speed = sum(self.file_speed_bytes.get(item, 0.0) for item in tracked_paths)
        self.total_speed_text.value = f"总速度 {format_speed(total_speed)}"

    def pause_download(self) -> None:
        if not self.busy or self.active_task != "download" or self.cancel_event.is_set():
            return
        self.download_stop_reason = "pause"
        self.cancel_event.set()
        self.summary_text.value = "正在暂停下载"
        self.append_log("[暂停] 正在停止当前连接，已下载的分片会保留。")
        self.update_download_button_state()
        self.safe_update()

    def resume_download(self) -> None:
        if self.busy or not self.download_paused:
            return
        self.download()

    def reset_download_selection(self) -> None:
        for file_info in self.files_to_download:
            path = file_info["path"]
            self.set_file_selected(path, False)
            controls = self.file_row_controls.get(path)
            if controls and controls["status"].content.value != "完成":
                self.set_file_status(path, "未选择", 0.0)
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
                self.append_log("[重新选择] 正在停止当前下载，已下载的分片会保留。")
            self.summary_text.value = "正在停止下载，稍后可重新选择"
            self.update_download_button_state()
            self.safe_update()
            return
        self.reset_download_selection()

    def set_busy(self, busy: bool, task_name: Optional[str] = None) -> None:
        self.busy = busy
        self.active_task = task_name if busy else None
        for control in (self.parse_button,):
            control.disabled = busy
        self.share_url_field.disabled = busy
        self.download_root_field.disabled = busy
        self.file_workers_field.disabled = busy
        self.part_workers_field.disabled = busy
        self.update_download_button_state()
        self.safe_update()

    def run_worker(self, target: Callable[[], None], task_name: str = "general") -> None:
        if self.busy:
            return
        self.set_busy(True, task_name)

        def wrapper():
            writer = QueueTextWriter(self.ui_queue)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    target()
            except DownloadCancelled as exc:
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

    def process_queue_loop(self) -> None:
        while self.queue_running:
            self.process_queue_batch()
            time.sleep(0.1)

    def process_event(self, event) -> None:
        kind = event[0]
        if kind == "log":
            self.append_log(event[1])
        elif kind == "status":
            self.status_text.value = event[1]
        elif kind == "summary":
            self.summary_text.value = event[1]
        elif kind == "progress":
            total = max(1, event[2])
            self.overall_progress.value = max(0.0, min(1.0, event[1] / total))
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

    def login(self) -> None:
        def worker():
            self.ui_queue.put(("summary", "等待网页登录"))
            ensure_login_credentials(force_login=True)
            self.ui_queue.put(("status", "已登录"))
            self.ui_queue.put(("summary", "登录态已保存"))

        self.run_worker(worker, "login")

    def parse_share(self) -> None:
        share_url = (self.share_url_field.value or "").strip()
        if not share_url:
            self.show_snack("请先粘贴百度网盘分享链接或完整分享文本。")
            return

        def worker():
            if not BDUSS:
                self.ui_queue.put(("summary", "等待网页登录"))
                ensure_login_credentials()
                self.ui_queue.put(("status", "已登录"))

            surl, pwd = parse_share_link(share_url)
            self.ui_queue.put(("summary", "正在解析分享"))
            print("正在解析分享内容...")
            status_callback = lambda message: self.ui_queue.put(("summary", message))
            try:
                yun_data, session = get_yun_data(
                    surl,
                    pwd,
                    password_provider=require_password_from_link,
                    status_callback=status_callback,
                )
            except Exception as exc:
                if not is_likely_login_error(exc):
                    raise
                print(f"解析失败，可能是登录态失效: {exc}")
                ensure_login_credentials(force_login=True)
                self.ui_queue.put(("status", "已登录"))
                yun_data, session = get_yun_data(
                    surl,
                    pwd,
                    password_provider=require_password_from_link,
                    status_callback=status_callback,
                )

            self.ui_queue.put(("summary", "正在读取文件列表"))
            print("获取文件列表（包括子文件夹）...")
            all_files = get_file_list_recursive(yun_data, session)
            files = [item for item in all_files if not item["isdir"]]
            dirs = [item for item in all_files if item["isdir"]]
            self.session = session
            self.yun_data = yun_data
            self.files_to_download = files
            print(f"共发现 {len(files)} 个文件，{len(dirs)} 个文件夹")
            self.ui_queue.put(("files", files, dirs))

        self.run_worker(worker, "parse")

    def download(self) -> None:
        if not self.files_to_download or not self.session or not self.yun_data:
            self.show_snack("请先解析分享文件列表。")
            return

        selected_files = self.get_selected_files()
        if not selected_files:
            self.show_snack("请先在文件列表中选择需要下载的文件。")
            return

        download_root = (self.download_root_field.value or "").strip() or DOWNLOAD_ROOT
        file_workers, part_workers = self.read_worker_settings()
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        for file_info in selected_files:
            self.set_file_status(file_info["path"], "等待", 0.0)
        self.safe_update()

        def worker():
            global USE_PROGRESS_BAR
            old_progress = USE_PROGRESS_BAR
            USE_PROGRESS_BAR = False
            completed_paths = set()
            try:
                os.makedirs(download_root, exist_ok=True)
                self.ui_queue.put(("summary", f"正在下载 {len(selected_files)} 个文件，文件并发 {file_workers}，单文件线程 {part_workers}"))
                self.ui_queue.put(("overall_progress", 0))

                link_failed_count = 0
                download_failed_count = 0
                download_ok_count = 0
                link_lock = threading.Lock()

                def make_progress_callback(path: str):
                    def callback(current: int, total: int, speed: float, status: str) -> None:
                        self.ui_queue.put(("file_progress", path, current, total, speed, status))
                    return callback

                with ThreadPoolExecutor(max_workers=file_workers) as executor:
                    future_map = {}
                    for file_info in selected_files:
                        raise_if_cancelled(self.cancel_event)
                        file_path = file_info["path"]
                        rel_path = file_info["path"]
                        if rel_path.startswith("/"):
                            rel_path = rel_path[1:]
                        dir_part, file_name = os.path.split(rel_path)
                        if not file_name:
                            print(f"  [失败] 文件路径异常，无法确定文件名: {file_info['path']}")
                            link_failed_count += 1
                            self.ui_queue.put(("file_progress", file_path, 0, file_info.get("size") or 0, 0.0, "失败"))
                            continue

                        save_dir = os.path.join(download_root, dir_part)
                        future = executor.submit(
                            download_file_with_link_refresh,
                            self.session,
                            self.yun_data,
                            file_info,
                            save_dir,
                            file_name,
                            link_lock,
                            make_progress_callback(file_path),
                            part_workers,
                            self.cancel_event,
                        )
                        future_map[future] = (file_name, file_path, file_info.get("size") or 0, file_info)

                    for future in as_completed(future_map):
                        raise_if_cancelled(self.cancel_event)
                        file_name, file_path, file_size, file_info = future_map[future]
                        try:
                            future.result()
                            download_ok_count += 1
                            completed_paths.add(file_path)
                            cleanup_downloaded_own_pan_files(self.session, self.yun_data, file_info)
                        except DownloadCancelled:
                            print(f"  [取消] 下载任务已取消: {file_name}")
                            raise
                        except Exception as exc:
                            print(f"  [失败] 下载 {file_name}: {exc}")
                            download_failed_count += 1
                            self.ui_queue.put(("file_progress", file_path, self.file_progress_bytes.get(file_path, 0), file_size, 0.0, "失败"))

                raise_if_cancelled(self.cancel_event)
                failed_count = link_failed_count + download_failed_count
                if failed_count == 0:
                    cleanup_own_pan_share_dir(self.session, self.yun_data)
                message = f"所有任务处理完毕，成功 {download_ok_count} 个，失败 {failed_count} 个，文件保存至: {download_root}"
                print(f"\n{message}")
                self.ui_queue.put(("summary", f"成功 {download_ok_count} 个，失败 {failed_count} 个"))
                self.ui_queue.put(("download_finished",))
                self.ui_queue.put(("info", message))
            except DownloadCancelled:
                reason = self.download_stop_reason
                if reason == "pause":
                    print("\n[暂停] 下载已暂停，未完成的分片已保留，点击继续可续传。")
                    for file_info in selected_files:
                        path = file_info["path"]
                        if path not in completed_paths:
                            self.ui_queue.put(("file_status", path, "已暂停", 0.0))
                    if not self.closing:
                        self.ui_queue.put(("download_paused",))
                    return
                if reason == "reselect":
                    print("\n[重新选择] 下载已停止，未完成的分片已保留，可重新选择文件后继续。")
                    for file_info in selected_files:
                        path = file_info["path"]
                        if path not in completed_paths:
                            self.ui_queue.put(("file_status", path, "未选择", 0.0))
                    if not self.closing:
                        self.ui_queue.put(("download_reselect",))
                    return

                print("\n[取消] 下载任务已取消，未完成的分片已保留，下次可继续续传。")
                for file_info in selected_files:
                    path = file_info["path"]
                    if path not in completed_paths:
                        self.ui_queue.put(("file_status", path, "已停止", 0.0))
                if not self.closing:
                    self.ui_queue.put(("summary", "下载已取消"))
                    self.ui_queue.put(("download_finished",))
                return
            finally:
                USE_PROGRESS_BAR = old_progress

        self.run_worker(worker, "download")


class BaiduPanDownloaderApp:
    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.ui_queue = queue.Queue()
        self.session = None
        self.yun_data = None
        self.files_to_download = []
        self.file_item_map = {}
        self.file_selected_map = {}
        self.file_row_tag_map = {}
        self.file_progress_bytes = {}
        self.file_total_bytes = {}
        self.file_speed_bytes = {}
        self.selection_buttons = []
        self.worker = None
        self.cancel_event = threading.Event()
        self.active_task = None
        self.download_paused = False
        self.download_stop_reason = ""
        self.closing = False
        self.force_exit_timer = None
        self.busy = False

        self.root = tk.Tk()
        self.root.title("轻云链")
        self.root.geometry("1380x780")
        self.root.minsize(1180, 640)
        try:
            self.root.state("zoomed")
        except Exception:
            pass
        self.root.configure(bg="#f4f6f8")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.share_url_var = tk.StringVar()
        self.download_root_var = tk.StringVar(value=DOWNLOAD_ROOT)
        self.file_workers_var = tk.StringVar(value=str(MAX_WORKERS))
        self.part_workers_var = tk.StringVar(value=str(DOWNLOAD_PART_WORKERS))
        self.status_var = tk.StringVar(value="未登录" if not BDUSS else "已登录")
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
        ttk.Label(header, text="轻云链", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.summary_var, style="Subtle.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=2, rowspan=2, sticky="e")

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=(18, 16))
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="分享文本", style="Body.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        self.share_entry = ttk.Entry(panel, textvariable=self.share_url_var)
        self.share_entry.grid(row=0, column=1, columnspan=6, sticky="ew", pady=(0, 12))

        ttk.Label(panel, text="保存位置", style="Body.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        ttk.Entry(panel, textvariable=self.download_root_var).grid(row=1, column=1, columnspan=5, sticky="ew", pady=(0, 12))
        ttk.Button(panel, text="浏览", style="Ghost.TButton", command=self.choose_download_dir).grid(row=1, column=6, sticky="e", padx=(10, 0), pady=(0, 12))

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=2, column=0, columnspan=7, sticky="ew")
        actions.columnconfigure(10, weight=1)
        ttk.Label(actions, text="文件并发", style="Body.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Spinbox(
            actions,
            from_=1,
            to=MAX_FILE_WORKERS,
            width=6,
            textvariable=self.file_workers_var,
        ).grid(row=0, column=1, sticky="w", padx=(0, 18))
        ttk.Label(actions, text="单文件线程", style="Body.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Spinbox(
            actions,
            from_=1,
            to=DOWNLOAD_MAX_PART_WORKERS,
            width=6,
            textvariable=self.part_workers_var,
        ).grid(row=0, column=3, sticky="w", padx=(0, 22))
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
            text="提示：暂停或重新选择会停止当前连接；未完成的 .parts 分片会保留，继续下载会自动续传。",
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
            selectmode="none",
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
        self.append_log(f"[配置] 凭据文件: {CREDENTIALS_FILE}")
        if CREDENTIALS_LOAD_ERROR:
            self.append_log(f"[配置] 凭据读取失败: {CREDENTIALS_LOAD_ERROR}")
        elif BDUSS:
            self.append_log("[配置] 已读取 BDUSS。")
        else:
            self.append_log("[配置] 未读取到 BDUSS，解析前会打开网页登录。")

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
                self.append_log("[取消] 正在停止下载任务，未完成的分片会保留用于下次续传。")
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
        selected = self.filedialog.askdirectory(initialdir=self.download_root_var.get() or DOWNLOAD_ROOT)
        if selected:
            self.download_root_var.set(selected)

    def read_worker_settings(self) -> Tuple[int, int]:
        file_workers = clamp_int(self.file_workers_var.get(), MAX_WORKERS, 1, MAX_FILE_WORKERS)
        part_workers = clamp_int(self.part_workers_var.get(), DOWNLOAD_PART_WORKERS, 1, DOWNLOAD_MAX_PART_WORKERS)
        self.file_workers_var.set(str(file_workers))
        self.part_workers_var.set(str(part_workers))
        return file_workers, part_workers

    def get_selected_files(self) -> List[Dict]:
        return [file_info for file_info in self.files_to_download if self.file_selected_map.get(file_info["path"])]

    def update_selection_summary(self) -> None:
        total_files = len(self.files_to_download)
        selected_files = self.get_selected_files()
        selected_size = sum(int(item.get("size") or 0) for item in selected_files)
        total_size = sum(int(item.get("size") or 0) for item in self.files_to_download)
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
        if hasattr(self, "pause_button"):
            self.pause_button.configure(state="normal" if can_pause else "disabled")
        if hasattr(self, "resume_button"):
            self.resume_button.configure(state="normal" if can_resume else "disabled")
        if hasattr(self, "reselect_button"):
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
        for file_info in self.files_to_download:
            self.set_file_selected(file_info["path"], True)
        self.update_selection_summary()
        self.update_download_button_state()

    def clear_file_selection(self) -> None:
        for file_info in self.files_to_download:
            self.set_file_selected(file_info["path"], False)
        self.update_selection_summary()
        self.update_download_button_state()

    def invert_file_selection(self) -> None:
        for file_info in self.files_to_download:
            path = file_info["path"]
            self.set_file_selected(path, not self.file_selected_map.get(path, False))
        self.update_selection_summary()
        self.update_download_button_state()

    def on_file_tree_click(self, event) -> None:
        if self.busy:
            return
        region = self.file_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        item_id = self.file_tree.identify_row(event.y)
        if not item_id:
            return
        self.file_tree.focus(item_id)
        values = self.file_tree.item(item_id, "values")
        if len(values) >= 2:
            self.toggle_file_selection(str(values[1]))

    def on_file_tree_keyboard_toggle(self, event) -> str:
        if self.busy:
            return "break"
        item_id = self.file_tree.focus()
        if not item_id:
            return "break"
        values = self.file_tree.item(item_id, "values")
        if len(values) >= 2:
            self.toggle_file_selection(str(values[1]))
        return "break"

    def set_file_status(self, path: str, status: str, speed: float = 0.0) -> None:
        item_id = self.file_item_map.get(path)
        if not item_id:
            return
        values = list(self.file_tree.item(item_id, "values"))
        if len(values) < 6:
            values = [
                FILE_SELECTED_MARK if self.file_selected_map.get(path) else FILE_UNSELECTED_MARK,
                path,
                format_size(self.file_total_bytes.get(path, 0)),
                format_progress_text(self.file_progress_bytes.get(path, 0), self.file_total_bytes.get(path, 0)),
                "-",
                "",
            ]
        values[4] = format_speed(speed)
        values[5] = status
        self.file_speed_bytes[path] = max(0.0, float(speed or 0.0))
        self.file_tree.item(item_id, values=values)

    def pause_download(self) -> None:
        if not self.busy or self.active_task != "download" or self.cancel_event.is_set():
            return
        self.download_stop_reason = "pause"
        self.cancel_event.set()
        self.summary_var.set("正在暂停下载")
        self.append_log("[暂停] 正在停止当前连接，已下载的分片会保留。")
        self.update_download_button_state()

    def resume_download(self) -> None:
        if self.busy:
            return
        if not self.download_paused:
            return
        self.download()

    def reset_download_selection(self) -> None:
        for file_info in self.files_to_download:
            path = file_info["path"]
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
                self.append_log("[重新选择] 正在停止当前下载，已下载的分片会保留。")
            self.summary_var.set("正在停止下载，稍后可重新选择")
            self.update_download_button_state()
            return

        self.reset_download_selection()

    def set_busy(self, busy: bool, task_name: Optional[str] = None) -> None:
        self.busy = busy
        if busy:
            self.active_task = task_name
        else:
            self.active_task = None
        state = "disabled" if busy else "normal"
        self.parse_button.configure(state=state)
        self.update_download_button_state()

    def run_worker(self, target: Callable[[], None], task_name: str = "general") -> None:
        if self.busy:
            return
        self.set_busy(True, task_name)

        def wrapper():
            writer = QueueTextWriter(self.ui_queue)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    target()
            except DownloadCancelled as exc:
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
            elif kind == "progress":
                self.progress.configure(maximum=max(1, event[2]))
                self.progress_var.set(event[1])
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

    def show_files(self, files: List[Dict], dirs: List[Dict]) -> None:
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
        for index, file_info in enumerate(files):
            path = file_info["path"]
            size = int(file_info.get("size") or 0)
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
        self.update_download_button_state()

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

    def login(self) -> None:
        def worker():
            self.ui_queue.put(("summary", "等待网页登录"))
            ensure_login_credentials(force_login=True)
            self.ui_queue.put(("status", "已登录"))
            self.ui_queue.put(("summary", "登录态已保存"))

        self.run_worker(worker, "login")

    def parse_share(self) -> None:
        share_url = self.share_url_var.get().strip()
        if not share_url:
            self.messagebox.showwarning("缺少分享文本", "请先粘贴百度网盘分享链接或完整分享文本。")
            return

        def worker():
            if not BDUSS:
                self.ui_queue.put(("summary", "等待网页登录"))
                ensure_login_credentials()
                self.ui_queue.put(("status", "已登录"))

            surl, pwd = parse_share_link(share_url)
            self.ui_queue.put(("summary", "正在解析分享"))
            print("正在解析分享内容...")
            status_callback = lambda message: self.ui_queue.put(("summary", message))
            try:
                yun_data, session = get_yun_data(
                    surl,
                    pwd,
                    password_provider=require_password_from_link,
                    status_callback=status_callback,
                )
            except Exception as exc:
                if not is_likely_login_error(exc):
                    raise
                print(f"解析失败，可能是登录态失效: {exc}")
                ensure_login_credentials(force_login=True)
                self.ui_queue.put(("status", "已登录"))
                yun_data, session = get_yun_data(
                    surl,
                    pwd,
                    password_provider=require_password_from_link,
                    status_callback=status_callback,
                )

            self.ui_queue.put(("summary", "正在读取文件列表"))
            print("获取文件列表（包括子文件夹）...")
            all_files = get_file_list_recursive(yun_data, session)
            files = [item for item in all_files if not item["isdir"]]
            dirs = [item for item in all_files if item["isdir"]]
            self.session = session
            self.yun_data = yun_data
            self.files_to_download = files
            print(f"共发现 {len(files)} 个文件，{len(dirs)} 个文件夹")
            self.ui_queue.put(("files", files, dirs))

        self.run_worker(worker, "parse")

    def download(self) -> None:
        if not self.files_to_download or not self.session or not self.yun_data:
            self.messagebox.showwarning("没有文件", "请先解析分享文件列表。")
            return

        selected_files = self.get_selected_files()
        if not selected_files:
            self.messagebox.showwarning("没有选择文件", "请先在文件列表中选择需要下载的文件。")
            return

        download_root = self.download_root_var.get().strip() or DOWNLOAD_ROOT
        file_workers, part_workers = self.read_worker_settings()
        self.download_paused = False
        self.download_stop_reason = ""
        self.cancel_event.clear()
        for file_info in selected_files:
            self.set_file_status(file_info["path"], "等待", 0.0)

        def worker():
            global USE_PROGRESS_BAR
            old_progress = USE_PROGRESS_BAR
            USE_PROGRESS_BAR = False
            try:
                os.makedirs(download_root, exist_ok=True)
                self.ui_queue.put(("summary", f"正在下载 {len(selected_files)} 个文件，文件并发 {file_workers}，单文件线程 {part_workers}"))
                self.ui_queue.put(("overall_progress", 0))

                link_failed_count = 0
                download_failed_count = 0
                download_ok_count = 0
                completed_paths = set()
                link_lock = threading.Lock()

                def make_progress_callback(path: str):
                    def callback(current: int, total: int, speed: float, status: str) -> None:
                        self.ui_queue.put(("file_progress", path, current, total, speed, status))
                    return callback

                with ThreadPoolExecutor(max_workers=file_workers) as executor:
                    future_map = {}
                    for file_info in selected_files:
                        raise_if_cancelled(self.cancel_event)
                        file_path = file_info["path"]
                        rel_path = file_info["path"]
                        if rel_path.startswith("/"):
                            rel_path = rel_path[1:]
                        dir_part, file_name = os.path.split(rel_path)
                        if not file_name:
                            print(f"  [失败] 文件路径异常，无法确定文件名: {file_info['path']}")
                            link_failed_count += 1
                            self.ui_queue.put(("file_progress", file_path, 0, file_info.get("size") or 0, 0.0, "失败"))
                            continue

                        save_dir = os.path.join(download_root, dir_part)
                        future = executor.submit(
                            download_file_with_link_refresh,
                            self.session,
                            self.yun_data,
                            file_info,
                            save_dir,
                            file_name,
                            link_lock,
                            make_progress_callback(file_path),
                            part_workers,
                            self.cancel_event,
                        )
                        future_map[future] = (file_name, file_path, file_info.get("size") or 0, file_info)

                    for future in as_completed(future_map):
                        raise_if_cancelled(self.cancel_event)
                        file_name, file_path, file_size, file_info = future_map[future]
                        try:
                            future.result()
                            download_ok_count += 1
                            completed_paths.add(file_path)
                            cleanup_downloaded_own_pan_files(self.session, self.yun_data, file_info)
                        except DownloadCancelled:
                            print(f"  [取消] 下载任务已取消: {file_name}")
                            raise
                        except Exception as exc:
                            print(f"  [失败] 下载 {file_name}: {exc}")
                            download_failed_count += 1
                            self.ui_queue.put(("file_progress", file_path, self.file_progress_bytes.get(file_path, 0), file_size, 0.0, "失败"))

                raise_if_cancelled(self.cancel_event)
                failed_count = link_failed_count + download_failed_count
                if failed_count == 0:
                    cleanup_own_pan_share_dir(self.session, self.yun_data)
                message = f"所有任务处理完毕，成功 {download_ok_count} 个，失败 {failed_count} 个，文件保存至: {download_root}"
                print(f"\n{message}")
                self.ui_queue.put(("summary", f"成功 {download_ok_count} 个，失败 {failed_count} 个"))
                self.ui_queue.put(("download_finished",))
                self.ui_queue.put(("info", message))
            except DownloadCancelled:
                reason = self.download_stop_reason
                if reason == "pause":
                    print("\n[暂停] 下载已暂停，未完成的分片已保留，点击继续可续传。")
                    for file_info in selected_files:
                        path = file_info["path"]
                        if path not in completed_paths:
                            self.ui_queue.put(("file_status", path, "已暂停", 0.0))
                    if not self.closing:
                        self.ui_queue.put(("download_paused",))
                    return
                if reason == "reselect":
                    print("\n[重新选择] 下载已停止，未完成的分片已保留，可重新选择文件后继续。")
                    for file_info in selected_files:
                        path = file_info["path"]
                        if path not in completed_paths:
                            self.ui_queue.put(("file_status", path, "未选择", 0.0))
                    if not self.closing:
                        self.ui_queue.put(("download_reselect",))
                    return

                print("\n[取消] 下载任务已取消，未完成的分片已保留，下次可继续续传。")
                for file_info in selected_files:
                    path = file_info["path"]
                    if path not in completed_paths:
                        self.ui_queue.put(("file_status", path, "已停止", 0.0))
                if not self.closing:
                    self.ui_queue.put(("summary", "下载已取消"))
                    self.ui_queue.put(("download_finished",))
                return
            finally:
                USE_PROGRESS_BAR = old_progress

        self.run_worker(worker, "download")


def launch_tk_gui() -> None:
    try:
        app = BaiduPanDownloaderApp()
    except Exception as exc:
        print(f"启动窗口失败: {exc}")
        print("可以使用 --cli 参数切换到命令行模式。")
        return
    app.run()


def ensure_local_dependency_path() -> None:
    deps_dir = os.path.join(SCRIPT_DIR, ".build_deps")
    if os.path.isdir(deps_dir) and deps_dir not in sys.path:
        sys.path.insert(0, deps_dir)


def launch_gui() -> None:
    ensure_local_dependency_path()
    try:
        import flet as ft
    except Exception as exc:
        print(f"Flet 界面不可用，切换到 Tkinter 界面: {exc}")
        launch_tk_gui()
        return

    def target(page):
        FletPanDownloaderApp(page, ft)

    if hasattr(ft, "run"):
        ft.run(target, view=ft.AppView.FLET_APP)
    else:
        ft.app(target=target)


def main():
    if not BDUSS:
        try:
            print("未找到百度网盘登录态，正在打开网页登录页...")
            ensure_login_credentials()
        except Exception as e:
            print(f"网页登录失败: {e}")
            return

    share_url = input("请输入百度网盘分享链接: ").strip()
    try:
        surl, pwd = parse_share_link(share_url)
    except ValueError as e:
        print(f"链接格式错误: {e}")
        return

    print("正在解析分享内容...")
    try:
        yun_data, session = get_yun_data(surl, pwd, password_provider=require_password_from_link)
    except Exception as e:
        if not is_likely_login_error(e):
            print(f"解析失败: {e}")
            return
        print(f"解析失败，可能是登录态失效: {e}")
        try:
            print("正在重新打开网页登录页刷新登录态...")
            ensure_login_credentials(force_login=True)
            yun_data, session = get_yun_data(surl, pwd, password_provider=require_password_from_link)
        except Exception as retry_e:
            print(f"解析失败: {retry_e}")
            return

    print("获取文件列表（包括子文件夹）...")
    try:
        all_files = get_file_list_recursive(yun_data, session)
    except Exception as e:
        print(f"获取文件列表失败: {e}")
        return

    files_to_download = [f for f in all_files if not f["isdir"]]
    dirs = [f for f in all_files if f["isdir"]]
    print(f"共发现 {len(files_to_download)} 个文件，{len(dirs)} 个文件夹")

    if not files_to_download:
        print("没有可下载的文件。")
        return

    # 显示列表并确认
    for i, f in enumerate(files_to_download, 1):
        print(f"  {i}. {f['path']}  ({f['size'] / 1024 / 1024:.2f} MB)")
    confirm = input(f"\n确认下载以上 {len(files_to_download)} 个文件？(y/n): ").strip().lower()
    if confirm != "y":
        print("已取消下载。")
        return

    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

    # 使用线程池并发下载
    link_failed_count = 0
    download_failed_count = 0
    download_ok_count = 0
    link_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {}
        for file_info in files_to_download:
            rel_path = file_info["path"]
            if rel_path.startswith("/"):
                rel_path = rel_path[1:]
            dir_part, file_name = os.path.split(rel_path)
            if not file_name:
                print(f"  [失败] 文件路径异常，无法确定文件名: {file_info['path']}")
                link_failed_count += 1
                continue

            save_dir = os.path.join(DOWNLOAD_ROOT, dir_part)

            future = executor.submit(
                download_file_with_link_refresh,
                session,
                yun_data,
                file_info,
                save_dir,
                file_name,
                link_lock,
            )
            future_map[future] = (file_name, file_info)

        for future in as_completed(future_map):
            fname, file_info = future_map[future]
            try:
                future.result()
                download_ok_count += 1
                cleanup_downloaded_own_pan_files(session, yun_data, file_info)
            except Exception as e:
                print(f"  [失败] 下载 {fname}: {e}")
                download_failed_count += 1

    failed_count = link_failed_count + download_failed_count
    if failed_count == 0:
        cleanup_own_pan_share_dir(session, yun_data)
    print(f"\n所有任务处理完毕，成功 {download_ok_count} 个，失败 {failed_count} 个，文件保存至: {DOWNLOAD_ROOT}")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        main()
    else:
        launch_gui()
