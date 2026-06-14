import os
import re
import json
import time
import base64
import requests
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, Tuple, List, Dict

# ==================== 配置项 ====================
# 推荐通过 credentials.local.json 设置，避免在代码中暴露敏感信息
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.local.json")

# 下载保存根目录
DOWNLOAD_ROOT = os.path.join("D:\\", "Downloads", "baidu_download")

# 并发下载线程数（不宜过大，避免被封）
MAX_WORKERS = 5

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


class NoRetryError(RuntimeError):
    """已知业务限制，重复请求不会自动恢复。"""


def load_credentials() -> Tuple[str, str]:
    """优先读取本地私密配置文件，其次读取当前进程环境变量。"""
    bduss = os.getenv("BAIDU_BDUSS", "").strip()
    stoken = os.getenv("BAIDU_STOKEN", "").strip()

    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            credentials = json.load(f)

        bduss = (credentials.get("BDUSS") or credentials.get("BAIDU_BDUSS") or bduss).strip()
        stoken = (credentials.get("STOKEN") or credentials.get("BAIDU_STOKEN") or stoken).strip()

    return bduss, stoken


def build_headers(bduss: str, stoken: str) -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://pan.baidu.com/",
    }


BDUSS, STOKEN = load_credentials()
HEADERS = build_headers(BDUSS, STOKEN)


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
                    print(f"  [!] 第 {i + 1}/{times} 次尝试失败: {e}，{delay}秒后重试...")
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


def parse_share_link(url: str) -> Tuple[str, Optional[str]]:
    """提取分享链接中的 surl 和提取码"""
    if "pan.baidu.com/s/" not in url:
        raise ValueError("不是有效的百度网盘分享链接")
    match = re.search(r"/s/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise ValueError("无法提取分享ID")
    surl = match.group(1)

    # 提取密码（支持 ?pwd=、&pwd= 或 # 后带密码等情况）
    pwd = None
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    if "pwd" in query_params:
        pwd = query_params["pwd"][0]
    elif parsed.fragment:
        # 有些链接密码在 # 后面，如 #vjv2
        pwd = parsed.fragment.strip()
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
        timeout=30,
    )

    try:
        result = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"提取码验证接口返回非 JSON，HTTP {resp.status_code}") from exc

    if result.get("errno") != 0:
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
    resp = session.get("https://pan.baidu.com/share/tplconfig", params=params, timeout=30)
    data = resp.json()
    if data.get("errno") != 0:
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


def get_yun_data(surl: str, pwd: Optional[str] = None) -> Tuple[Dict, requests.Session]:
    """获取分享页面的 yunData 并返回 session"""
    base_url = f"https://pan.baidu.com/s/{surl}"
    if pwd:
        base_url += f"?pwd={pwd}"

    session = requests.Session()
    session.headers.update(HEADERS)
    set_login_cookies(session, BDUSS, STOKEN)

    password_verified = False
    prompted_for_pwd = False

    while True:
        resp = session.get(base_url, allow_redirects=True, timeout=30)
        html = resp.text

        if is_extract_code_page(html):
            if password_verified:
                raise RuntimeError("提取码已验证，但页面仍要求输入提取码；可能 Cookie 失效、提取码错误或分享链接异常")

            if not pwd and not prompted_for_pwd:
                pwd = input("页面需要提取码，请手动输入: ").strip()
                prompted_for_pwd = True
            if not pwd:
                raise RuntimeError("未提供提取码")

            sekey = verify_share_password(session, surl, pwd)
            password_verified = True
            base_url = f"https://pan.baidu.com/s/{surl}"
            continue  # 重新请求

        # 提取 yunData
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
        tplconfig = get_share_tplconfig(session, surl, ["sign", "timestamp", "public"])
        for key in ("sign", "timestamp", "public"):
            if tplconfig.get(key) is not None:
                yun_data[key] = tplconfig[key]
        return yun_data, session


def get_file_list_recursive(yun_data: Dict, session: requests.Session, path: str = "/") -> List[Dict]:
    """递归获取所有文件和文件夹信息"""
    file_list = []
    shareid = yun_data["shareid"]
    uk = yun_data.get("share_uk") or yun_data["uk"]

    @retry(times=3)
    def fetch_dir(current_path: str):
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
            "num": "1000",
            "page": "1",
            "dir": current_path,
            "root": "1" if current_path == "/" else "0",
        }
        if yun_data.get("sekey"):
            params["sekey"] = yun_data["sekey"]

        resp = session.get("https://pan.baidu.com/share/list", params=params, timeout=30)
        data = resp.json()
        if data.get("errno") != 0:
            errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
            raise RuntimeError(f"获取文件列表失败: {errmsg}")

        entries = data.get("list") or data.get("data", {}).get("list", [])
        for entry in entries:
            name = entry["server_filename"]
            isdir = entry.get("isdir", 0) == 1
            fs_id = entry["fs_id"]
            size = int(entry.get("size") or 0)
            rel_path = entry.get("path") or os.path.join(current_path, name).replace("\\", "/")

            file_list.append({
                "path": rel_path,
                "name": name,
                "fs_id": fs_id,
                "isdir": isdir,
                "size": size,
            })
            if isdir:
                fetch_dir(rel_path)

    fetch_dir(path)
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
    if data.get("errno") != 0:
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
        if data.get("errno") != 0:
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
    if data.get("errno") != 0:
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
    if data.get("errno") != 0:
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
        raise RuntimeError("自己网盘中未找到同名同大小文件，请先转存或释放空间后再试")

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


def get_download_link(session: requests.Session, yun_data: Dict, file_info: Dict) -> Tuple[str, Optional[Dict]]:
    try:
        return get_dlink(session, yun_data, file_info["fs_id"]), None
    except NoRetryError as exc:
        print(f"  [提示] 分享直链不可用: {exc}")
        print("  [提示] 尝试从已转存到自己网盘的同名文件下载...")

    own_file, restore_info = prepare_own_file_for_download(session, yun_data, file_info)
    print(f"  [匹配] 自己网盘文件: {own_file.get('path')}")
    return get_own_file_dlink(session, yun_data, own_file["fs_id"]), restore_info


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
        if data.get("errno") != 0:
            errmsg = data.get("errmsg") or data.get("show_msg") or data.get("errno")
            if data.get("errno") == -20:
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


def download_file(session: requests.Session, dlink: str, save_path: str, file_name: str, file_size: int):
    """带断点续传的文件下载"""
    full_path = os.path.join(save_path, file_name)
    os.makedirs(save_path, exist_ok=True)

    # 文件已完整，跳过
    if os.path.exists(full_path) and os.path.getsize(full_path) == file_size:
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

    headers = HEADERS.copy()
    headers["Referer"] = "https://pan.baidu.com/disk/home"
    headers["User-Agent"] = DOWNLOAD_USER_AGENT

    print(f"  [下载] {file_name} ({file_size / 1024 / 1024:.2f} MB)", end="")
    if downloaded_bytes > 0:
        print(f"，从 {downloaded_bytes} 字节处续传", end="")
    print()

    @retry(times=3)
    def do_download():
        current = os.path.getsize(full_path) if os.path.exists(full_path) else 0
        if current > file_size:
            os.remove(full_path)
            current = 0

        mode = "ab" if current > 0 else "wb"
        progress = None
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
                    if resp.status_code not in (200, 206):
                        resp.raise_for_status()
                    if resp.status_code != 206 and not (start == 0 and end == file_size - 1):
                        raise RuntimeError(f"服务器未按分段返回数据: HTTP {resp.status_code}")

                    written = 0
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            current += len(chunk)
                            if progress:
                                progress.update(len(chunk))

                    expected = end - start + 1
                    if resp.status_code == 206 and written != expected:
                        raise RuntimeError(f"分段下载不完整: 预期 {expected}，实际 {written}")
            finally:
                if progress:
                    progress.close()

        actual_size = os.path.getsize(full_path)
        if actual_size != file_size:
            raise RuntimeError(f"文件大小校验失败: 预期 {file_size}，实际 {actual_size}")

    do_download()
    print(f"  [完成] {file_name}")


def main():
    if not BDUSS or not STOKEN:
        print(f"请先在 {CREDENTIALS_FILE} 中设置 BDUSS 和 STOKEN")
        return

    share_url = input("请输入百度网盘分享链接: ").strip()
    try:
        surl, pwd = parse_share_link(share_url)
    except ValueError as e:
        print(f"链接格式错误: {e}")
        return

    print("正在解析分享内容...")
    try:
        yun_data, session = get_yun_data(surl, pwd)
    except Exception as e:
        print(f"解析失败: {e}")
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
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {}
        for file_info in files_to_download:
            rel_path = file_info["path"]
            if rel_path.startswith("/"):
                rel_path = rel_path[1:]
            dir_part, file_name = os.path.split(rel_path)
            save_dir = os.path.join(DOWNLOAD_ROOT, dir_part)

            # 先获取 dlink（必须串行，因为受限于 API 频率）
            try:
                dlink, restore_info = get_download_link(session, yun_data, file_info)
            except Exception as e:
                print(f"  [失败] 获取下载链接 {file_name}: {e}")
                continue

            future = executor.submit(download_file, session, dlink, save_dir, file_name, file_info["size"])
            future_map[future] = (file_name, restore_info)

        for future in as_completed(future_map):
            fname, restore_info = future_map[future]
            try:
                future.result()
            except Exception as e:
                print(f"  [失败] 下载 {fname}: {e}")
            finally:
                restore_own_file_name(session, yun_data, restore_info)

    print(f"\n所有任务处理完毕，文件保存至: {DOWNLOAD_ROOT}")


if __name__ == "__main__":
    main()
