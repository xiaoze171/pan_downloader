import os
import re
import sys
import json
import time
import base64
import shutil
import socket
import struct
import queue
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

# 下载保存根目录
DOWNLOAD_ROOT = os.path.join("D:\\", "下载", "baidu_download")

# 并发下载线程数（大文件建议单文件下载，避免被限流或代理断流）
MAX_WORKERS = 1

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
DOWNLOAD_LINK_REFRESH_RETRIES = 3
DOWNLOAD_USE_ENV_PROXY = False
BAIDU_API_USE_ENV_PROXY = False
SHARE_REQUEST_TIMEOUT = (10, 30)
SHARE_LIST_PAGE_SIZE = 1000
CLOUD_SAVE_ROOT = "/baidu_pan_downloader"
TRANSFER_SEARCH_RETRIES = 6
TRANSFER_SEARCH_DELAY = 2
BAIDU_PAN_HOME_URL = "https://pan.baidu.com/disk/main#/index?category=all"
BAIDU_LOGIN_URL = "https://passport.baidu.com/v2/?login&u=" + quote(BAIDU_PAN_HOME_URL, safe="")
LOGIN_TIMEOUT_SECONDS = 10 * 60
LOGIN_POLL_INTERVAL = 2


class NoRetryError(RuntimeError):
    """已知业务限制，重复请求不会自动恢复。"""


def load_credentials() -> Tuple[str, str]:
    """优先读取本地私密配置文件，其次读取当前进程环境变量。"""
    bduss = os.getenv("BAIDU_BDUSS", "").strip()
    stoken = os.getenv("BAIDU_STOKEN", "").strip()

    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            credentials = json.load(f)

        bduss = (
            credentials.get("BDUSS")
            or credentials.get("BDUSS_BFESS")
            or credentials.get("BAIDU_BDUSS")
            or bduss
        ).strip()
        stoken = (
            credentials.get("STOKEN")
            or credentials.get("STOKEN_BFESS")
            or credentials.get("BAIDU_STOKEN")
            or stoken
        ).strip()

    return bduss, stoken


def save_credentials(credentials: Dict[str, str]) -> None:
    data = {}
    for key in ("BDUSS", "STOKEN", "BAIDUID"):
        value = (credentials.get(key) or "").strip()
        if value:
            data[key] = value

    if not data.get("BDUSS"):
        raise RuntimeError("登录成功但没有获取到 BDUSS Cookie")

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


def wait_for_browser_debug(port: int, timeout: int = 15) -> None:
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
    last_error = None
    while time.time() < deadline:
        try:
            credentials = extract_login_credentials(get_cdp_cookies(port))
            if credentials.get("BDUSS"):
                return credentials
        except Exception as exc:
            last_error = exc
        time.sleep(LOGIN_POLL_INTERVAL)

    if last_error:
        raise RuntimeError(f"等待网页登录超时: {last_error}")
    raise RuntimeError("等待网页登录超时，未获取到 BDUSS Cookie")


def login_with_browser() -> Dict[str, str]:
    browser = find_edge_executable()
    port = find_available_port()
    profile_dir = tempfile.mkdtemp(prefix="baidu_pan_login_")
    process = None
    try:
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
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_browser_debug(port)
        print("请在弹出的 Edge 百度登录窗口完成登录，脚本会自动读取登录态并继续...")
        credentials = wait_for_login_credentials(port)
        save_credentials(credentials)
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
        return own_file

    print("  [提示] 转存成功，等待自己网盘索引更新...")
    own_file = wait_for_transferred_file(session, yun_data, file_info)
    if own_file:
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


def get_own_file_dlink(session: requests.Session, yun_data: Dict, fs_id: int) -> str:
    sign, timestamp = get_pan_download_sign(session, yun_data)
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
    if not is_api_success(data):
        errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
        raise RuntimeError(f"获取自己网盘下载链接失败: {errmsg}")

    dlink = extract_dlink(data.get("dlink") or data)
    if not dlink:
        raise RuntimeError("获取自己网盘下载链接失败: 响应中没有 dlink")
    return dlink


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


def get_pan_download_sign(session: requests.Session, yun_data: Dict) -> Tuple[str, int]:
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
        errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
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

    if txt_name != original_name and current_name == original_name:
        old_path = own_file["path"]
        new_path = rename_own_file(session, yun_data, old_path, txt_name)
        own_file = own_file.copy()
        own_file["path"] = new_path
        own_file["server_filename"] = txt_name
        restore_info = {"path": new_path, "newname": original_name}
        print(f"  [重命名] 自己网盘文件临时改为: {new_path}")

    return own_file, restore_info


def restore_own_file_name(session: requests.Session, yun_data: Dict, restore_info: Optional[Dict]) -> None:
    if not restore_info:
        return
    try:
        restored_path = rename_own_file(session, yun_data, restore_info["path"], restore_info["newname"])
        print(f"  [恢复] 自己网盘文件名已恢复: {restored_path}")
    except Exception as exc:
        print(f"  [警告] 自己网盘文件名恢复失败: {exc}")


def get_download_link(
    session: requests.Session,
    yun_data: Dict,
    file_info: Dict,
    prefer_embedded: bool = True,
) -> Tuple[str, Optional[Dict]]:
    dlink = extract_dlink(file_info.get("dlink"))
    if prefer_embedded and dlink:
        return dlink, None

    try:
        return get_dlink(session, yun_data, file_info["fs_id"]), None
    except NoRetryError as exc:
        print(f"  [提示] 分享直链不可用: {exc}")
        print("  [提示] 尝试从已转存到自己网盘的同名文件下载...")

    own_file, restore_info = prepare_own_file_for_download(session, yun_data, file_info)
    print(f"  [匹配] 自己网盘文件: {own_file.get('path')}")
    return get_own_file_dlink(session, yun_data, own_file["fs_id"]), restore_info


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
            errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
            if is_api_errno(data, -20):
                raise NoRetryError("下载接口要求验证码；当前脚本无法自动通过百度验证码")
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


def probe_download_size(session: requests.Session, dlink: str, headers: Dict[str, str], declared_size: int) -> int:
    probe_headers = headers.copy()
    probe_headers["Range"] = "bytes=0-0"
    try:
        resp = session.get(dlink, headers=probe_headers, stream=True, timeout=60)
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


def download_file(
    session: requests.Session,
    dlink: str,
    save_path: str,
    file_name: str,
    file_size: int,
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None,
):
    """带断点续传的文件下载"""
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
                    start = current
                    end = min(start + DOWNLOAD_RANGE_SIZE - 1, file_size - 1)
                    chunk_headers = headers.copy()
                    chunk_headers["Range"] = f"bytes={start}-{end}"

                    resp = session.get(dlink, headers=chunk_headers, stream=True, timeout=60)
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


def download_file_with_link_refresh(
    link_session: requests.Session,
    yun_data: Dict,
    file_info: Dict,
    save_dir: str,
    file_name: str,
    link_lock: Optional[threading.Lock] = None,
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None,
) -> None:
    restore_infos = []
    last_error = None
    full_path = os.path.join(save_dir, file_name)

    def existing_size() -> int:
        return os.path.getsize(full_path) if os.path.exists(full_path) else 0

    def emit_status(status: str) -> None:
        if progress_callback:
            progress_callback(existing_size(), int(file_info.get("size") or 0), 0.0, status)

    try:
        for attempt in range(1, DOWNLOAD_LINK_REFRESH_RETRIES + 1):
            restore_info = None
            try:
                emit_status("获取链接")
                if link_lock:
                    with link_lock:
                        dlink, restore_info = get_download_link(
                            link_session,
                            yun_data,
                            file_info,
                            prefer_embedded=(attempt == 1),
                        )
                else:
                    dlink, restore_info = get_download_link(
                        link_session,
                        yun_data,
                        file_info,
                        prefer_embedded=(attempt == 1),
                    )
                restore_infos.append(restore_info)

                download_session = clone_session(link_session, trust_env=DOWNLOAD_USE_ENV_PROXY)
                download_file(download_session, dlink, save_dir, file_name, file_info["size"], progress_callback)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= DOWNLOAD_LINK_REFRESH_RETRIES:
                    emit_status("失败")
                    raise
                emit_status("重试")
                print(f"  [提示] 下载中断，刷新下载链接后续传 {file_name}: {exc}")
                time.sleep(2)
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
        self.file_progress_bytes = {}
        self.file_total_bytes = {}
        self.worker = None
        self.busy = False

        self.root = tk.Tk()
        self.root.title("百度网盘分享下载")
        self.root.geometry("1180x760")
        self.root.minsize(980, 660)
        self.root.configure(bg="#f5f7fb")

        self.share_url_var = tk.StringVar()
        self.download_root_var = tk.StringVar(value=DOWNLOAD_ROOT)
        self.status_var = tk.StringVar(value="未登录" if not BDUSS else "已登录")
        self.summary_var = tk.StringVar(value="等待解析分享")
        self.progress_var = tk.DoubleVar(value=0)

        self.configure_style()
        self.build_ui()
        self.share_entry.focus_set()
        self.root.after(100, self.process_queue)

    def configure_style(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("App.TFrame", background="#f5f7fb")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("Header.TLabel", background="#f5f7fb", foreground="#172033", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Subtle.TLabel", background="#f5f7fb", foreground="#5f6b7a", font=("Microsoft YaHei UI", 10))
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#172033", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Body.TLabel", background="#ffffff", foreground="#27364a", font=("Microsoft YaHei UI", 10))
        style.configure("Status.TLabel", background="#edf6ff", foreground="#1d5fd1", font=("Microsoft YaHei UI", 10, "bold"), padding=(10, 5))
        style.configure("TEntry", padding=8, fieldbackground="#ffffff", bordercolor="#d7deea", lightcolor="#d7deea", darkcolor="#d7deea")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8), background="#2563eb", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9bb7ee")])
        style.configure("Ghost.TButton", font=("Microsoft YaHei UI", 10), padding=(14, 8), background="#eef2f7", foreground="#27364a")
        style.map("Ghost.TButton", background=[("active", "#e2e8f0"), ("disabled", "#f1f5f9")])
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=30, background="#ffffff", fieldbackground="#ffffff", borderwidth=0)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), background="#eef2f7", foreground="#334155")
        style.configure("Horizontal.TProgressbar", troughcolor="#e9eef6", background="#2563eb", bordercolor="#e9eef6", lightcolor="#2563eb", darkcolor="#2563eb")

    def build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        outer = ttk.Frame(self.root, style="App.TFrame", padding=24)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="百度网盘分享下载", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(3, weight=1)

        ttk.Label(panel, text="分享文本", style="Body.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 10))
        self.share_entry = ttk.Entry(panel, textvariable=self.share_url_var)
        self.share_entry.grid(row=0, column=1, columnspan=4, sticky="ew", pady=(0, 10))

        ttk.Label(panel, text="保存位置", style="Body.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 10))
        ttk.Entry(panel, textvariable=self.download_root_var).grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Button(panel, text="浏览", style="Ghost.TButton", command=self.choose_download_dir).grid(row=1, column=4, sticky="e", padx=(10, 0), pady=(0, 10))

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=2, column=0, columnspan=5, sticky="ew")
        actions.columnconfigure(4, weight=1)
        self.login_button = ttk.Button(actions, text="登录 / 刷新", style="Ghost.TButton", command=self.login)
        self.login_button.grid(row=0, column=0, padx=(0, 10))
        self.parse_button = ttk.Button(actions, text="解析文件", style="Primary.TButton", command=self.parse_share)
        self.parse_button.grid(row=0, column=1, padx=(0, 10))
        self.download_button = ttk.Button(actions, text="开始下载", style="Primary.TButton", command=self.download, state="disabled")
        self.download_button.grid(row=0, column=2, padx=(0, 10))
        ttk.Label(actions, textvariable=self.summary_var, style="Body.TLabel").grid(row=0, column=4, sticky="e")

        content = ttk.Frame(outer, style="App.TFrame")
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=5)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        files_panel = ttk.Frame(content, style="Panel.TFrame", padding=14)
        files_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        files_panel.rowconfigure(1, weight=1)
        files_panel.columnconfigure(0, weight=1)
        ttk.Label(files_panel, text="文件列表", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))

        tree_frame = ttk.Frame(files_panel, style="Panel.TFrame")
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.file_tree = ttk.Treeview(
            tree_frame,
            columns=("path", "size", "progress", "speed", "status"),
            show="headings",
            selectmode="browse",
        )
        self.file_tree.heading("path", text="路径")
        self.file_tree.heading("size", text="大小")
        self.file_tree.heading("progress", text="进度")
        self.file_tree.heading("speed", text="速度")
        self.file_tree.heading("status", text="状态")
        self.file_tree.column("path", width=390, anchor="w", stretch=True)
        self.file_tree.column("size", width=95, anchor="e", stretch=False)
        self.file_tree.column("progress", width=175, anchor="center", stretch=False)
        self.file_tree.column("speed", width=95, anchor="e", stretch=False)
        self.file_tree.column("status", width=80, anchor="center", stretch=False)
        self.file_tree.grid(row=0, column=0, sticky="nsew")
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
            bg="#0f172a",
            fg="#dbeafe",
            insertbackground="#dbeafe",
            relief="flat",
            padx=12,
            pady=10,
            font=("Consolas", 9),
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(footer, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")

    def run(self) -> None:
        self.root.mainloop()

    def choose_download_dir(self) -> None:
        selected = self.filedialog.askdirectory(initialdir=self.download_root_var.get() or DOWNLOAD_ROOT)
        if selected:
            self.download_root_var.set(selected)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.login_button.configure(state=state)
        self.parse_button.configure(state=state)
        self.download_button.configure(state="disabled" if busy or not self.files_to_download else "normal")

    def run_worker(self, target: Callable[[], None]) -> None:
        if self.busy:
            return
        self.set_busy(True)

        def wrapper():
            writer = QueueTextWriter(self.ui_queue)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    target()
            except Exception as exc:
                self.ui_queue.put(("error", str(exc)))
            finally:
                writer.flush()
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
            elif kind == "files":
                self.show_files(event[1], event[2])
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
        self.file_progress_bytes = {}
        self.file_total_bytes = {}
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        for file_info in files:
            path = file_info["path"]
            size = int(file_info.get("size") or 0)
            item_id = self.file_tree.insert(
                "",
                "end",
                values=(path, format_size(size), format_progress_text(0, size), "-", "等待"),
            )
            self.file_item_map[path] = item_id
            self.file_progress_bytes[path] = 0
            self.file_total_bytes[path] = size
        total_size = sum(int(item.get("size") or 0) for item in files)
        self.summary_var.set(f"{len(files)} 个文件，{len(dirs)} 个文件夹，合计 {format_size(total_size)}")
        self.progress.configure(maximum=100)
        self.progress_var.set(0)
        self.download_button.configure(state="normal" if files and not self.busy else "disabled")

    def update_file_progress(self, path: str, current: int, total: int, speed: float, status: str) -> None:
        item_id = self.file_item_map.get(path)
        if not item_id:
            return

        current = max(0, int(current or 0))
        total = max(int(total or 0), current)
        self.file_progress_bytes[path] = current
        self.file_total_bytes[path] = total

        values = list(self.file_tree.item(item_id, "values"))
        if len(values) < 5:
            values = [path, format_size(total), "", "", ""]
        values[1] = format_size(total)
        values[2] = format_progress_text(current, total)
        values[3] = format_speed(speed)
        values[4] = status
        self.file_tree.item(item_id, values=values)

        total_bytes = sum(max(1, value) for value in self.file_total_bytes.values())
        done_bytes = sum(min(self.file_progress_bytes.get(path, 0), self.file_total_bytes.get(path, 0)) for path in self.file_total_bytes)
        overall_percent = 100.0 if total_bytes <= 0 else max(0.0, min(100.0, done_bytes * 100.0 / total_bytes))
        self.progress.configure(maximum=100)
        self.progress_var.set(overall_percent)

    def login(self) -> None:
        def worker():
            self.ui_queue.put(("summary", "等待网页登录"))
            ensure_login_credentials(force_login=True)
            self.ui_queue.put(("status", "已登录"))
            self.ui_queue.put(("summary", "登录态已保存"))

        self.run_worker(worker)

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

        self.run_worker(worker)

    def download(self) -> None:
        if not self.files_to_download or not self.session or not self.yun_data:
            self.messagebox.showwarning("没有文件", "请先解析分享文件列表。")
            return

        download_root = self.download_root_var.get().strip() or DOWNLOAD_ROOT

        def worker():
            global USE_PROGRESS_BAR
            old_progress = USE_PROGRESS_BAR
            USE_PROGRESS_BAR = False
            try:
                os.makedirs(download_root, exist_ok=True)
                self.ui_queue.put(("summary", "正在下载"))
                self.ui_queue.put(("overall_progress", 0))

                link_failed_count = 0
                download_failed_count = 0
                download_ok_count = 0
                link_lock = threading.Lock()

                def make_progress_callback(path: str):
                    def callback(current: int, total: int, speed: float, status: str) -> None:
                        self.ui_queue.put(("file_progress", path, current, total, speed, status))
                    return callback

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    future_map = {}
                    for file_info in self.files_to_download:
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
                        )
                        future_map[future] = (file_name, file_path, file_info.get("size") or 0)

                    for future in as_completed(future_map):
                        file_name, file_path, file_size = future_map[future]
                        try:
                            future.result()
                            download_ok_count += 1
                        except Exception as exc:
                            print(f"  [失败] 下载 {file_name}: {exc}")
                            download_failed_count += 1
                            self.ui_queue.put(("file_progress", file_path, self.file_progress_bytes.get(file_path, 0), file_size, 0.0, "失败"))

                failed_count = link_failed_count + download_failed_count
                message = f"所有任务处理完毕，成功 {download_ok_count} 个，失败 {failed_count} 个，文件保存至: {download_root}"
                print(f"\n{message}")
                self.ui_queue.put(("summary", f"成功 {download_ok_count} 个，失败 {failed_count} 个"))
                self.ui_queue.put(("info", message))
            finally:
                USE_PROGRESS_BAR = old_progress

        self.run_worker(worker)


def launch_gui() -> None:
    try:
        app = BaiduPanDownloaderApp()
    except Exception as exc:
        print(f"启动窗口失败: {exc}")
        print("可以使用 --cli 参数切换到命令行模式。")
        return
    app.run()


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
            future_map[future] = file_name

        for future in as_completed(future_map):
            fname = future_map[future]
            try:
                future.result()
                download_ok_count += 1
            except Exception as e:
                print(f"  [失败] 下载 {fname}: {e}")
                download_failed_count += 1

    failed_count = link_failed_count + download_failed_count
    print(f"\n所有任务处理完毕，成功 {download_ok_count} 个，失败 {failed_count} 个，文件保存至: {DOWNLOAD_ROOT}")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        main()
    else:
        launch_gui()
