import json
import os
import re
import sys
import time
from html import unescape
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


configure_console_encoding()


@dataclass
class CloudFile:
    file_id: str
    name: str
    path: str
    size: int = 0
    is_folder: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)
    drive_id: str = ""
    fid_token: str = ""


@dataclass
class DownloadConfig:
    output_dir: str
    user_agent: str = DEFAULT_USER_AGENT
    referer: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)
    retries: int = 5
    retry_delay: float = 2.0
    chunk_size: int = 1024 * 1024
    connect_timeout: int = 15
    read_timeout: int = 120
    overwrite: bool = False
    progress_callback: Optional[Callable[[int, int, float, str], None]] = None
    cancel_event: Any = None
    show_progress: bool = True


class DownloadCancelled(RuntimeError):
    pass


def add_project_root_to_path(current_file: str) -> None:
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(current_file)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def load_credentials(credentials_file: str, env_keys: Sequence[str], defaults: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    data: Dict[str, str] = dict(defaults or {})
    if os.path.exists(credentials_file):
        with open(credentials_file, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Credentials file must be a JSON object: {credentials_file}")
        for key, value in loaded.items():
            data[str(key)] = str(value or "").strip()

    for key in env_keys:
        value = os.getenv(key, "").strip()
        if value:
            data[key] = value
    return data


def extract_password(text: str) -> str:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(text.strip())
    query = parse_qs(parsed.query)
    for key in ("pwd", "password", "passcode", "pass_code", "code"):
        if query.get(key):
            return query[key][0].strip()

    patterns = (
        r"(?:pwd|password|passcode|pass_code|code)[=:：\s]+([A-Za-z0-9]{4,})",
        r"(?:提取码|访问码|密码)[：:\s]*([A-Za-z0-9]{4,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def parse_json_response(response: requests.Response, provider: str) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(response.text[:500])

    if response.status_code >= 400:
        raise RuntimeError(f"{provider} API error {response.status_code}: {json.dumps(data, ensure_ascii=False)}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{provider} API returned non-object JSON: {json.dumps(data, ensure_ascii=False)}")
    return data


def first_url(value: Any) -> str:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return unescape(value.strip())
    if isinstance(value, dict):
        for key in (
            "download_url",
            "downloadUrl",
            "web_content_link",
            "url",
            "cdn_url",
            "internal_url",
            "video_url",
        ):
            url = first_url(value.get(key))
            if url:
                return url
        for item in value.values():
            url = first_url(item)
            if url:
                return url
    if isinstance(value, list):
        for item in value:
            url = first_url(item)
            if url:
                return url
    return ""


def first_key(value: Any, keys: Tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key):
                return str(value[key])
        for item in value.values():
            found = first_key(item, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = first_key(item, keys)
            if found:
                return found
    return ""


def safe_join(root: str, relative_path: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", relative_path) if p and p not in (".", "..")]
    return os.path.abspath(os.path.join(root, *parts))


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def print_files(files: Iterable[CloudFile]) -> None:
    for index, file in enumerate(files, start=1):
        print(f"{index:>3}. {file.path}  {format_size(file.size)}")


def parse_selection(selection: str, count: int) -> List[int]:
    text = (selection or "").strip()
    if not text or text.lower() in ("all", "*"):
        return list(range(count))

    indexes = set()
    for part in re.split(r"[,，\s]+", text):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            for value in range(start, end + 1):
                indexes.add(value - 1)
        else:
            indexes.add(int(part) - 1)

    invalid = [i + 1 for i in indexes if i < 0 or i >= count]
    if invalid:
        raise ValueError(f"Selection index out of range: {invalid}")
    return sorted(indexes)


def selected_files(files: Sequence[CloudFile], selection: str) -> List[CloudFile]:
    return [files[index] for index in parse_selection(selection, len(files))]


def run_jobs(files: Sequence[CloudFile], worker_count: int, job: Callable[[CloudFile], None]) -> None:
    workers = max(1, int(worker_count or 1))
    if workers == 1 or len(files) <= 1:
        for file in files:
            job(file)
        return

    failures: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(job, file): file for file in files}
        for future in as_completed(future_map):
            file = future_map[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{file.path}: {exc}")
                print(f"Failed: {file.path}: {exc}")
    if failures:
        raise RuntimeError("Some files failed:\n" + "\n".join(failures))


def download_file_with_resolver(
    file: CloudFile,
    resolve_url: Callable[[], str],
    config: DownloadConfig,
) -> None:
    target = safe_join(config.output_dir, file.path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target) and not config.overwrite:
        existing_size = os.path.getsize(target)
        if file.size <= 0 or existing_size == file.size:
            print(f"Skip existing: {target}")
            return

    temp = target + ".part"
    last_error: Optional[Exception] = None
    url = ""
    for attempt in range(1, config.retries + 1):
        try:
            if not url or attempt > 1:
                url = resolve_url()
            _download_once(url, target, temp, config, file.size)
            return
        except DownloadCancelled:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= config.retries:
                break
            print(f"Download failed ({attempt}/{config.retries}), retrying: {exc}")
            _sleep_or_cancel(config, config.retry_delay)
            url = ""

    raise RuntimeError(f"Download failed after {config.retries} attempts: {file.path}: {last_error}")


def _download_once(url: str, target: str, temp: str, config: DownloadConfig, declared_size: int) -> None:
    _raise_if_cancelled(config)
    if ".m3u8" in url.lower():
        _download_hls_once(url, target, temp, config)
        return

    downloaded = os.path.getsize(temp) if os.path.exists(temp) else 0
    headers = {"User-Agent": config.user_agent}
    if config.referer:
        headers["Referer"] = config.referer
    headers.update({key: value for key, value in config.extra_headers.items() if value})
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"

    session = requests.Session()
    session.trust_env = False
    with session.get(
        url,
        headers=headers,
        stream=True,
        timeout=(config.connect_timeout, config.read_timeout),
    ) as response:
        if response.status_code == 416 and downloaded:
            if declared_size <= 0 or downloaded == declared_size:
                os.replace(temp, target)
                return
            if os.path.exists(temp):
                os.remove(temp)
            raise RuntimeError(f"Server refused resume for incomplete file: got {downloaded} bytes, expected {declared_size}")
        if downloaded and response.status_code != 206:
            downloaded = 0
            response.close()
            if os.path.exists(temp):
                os.remove(temp)
            return _download_once(url, target, temp, config, declared_size)
        if response.status_code >= 400:
            error_text = response.text[:500].replace("\r", " ").replace("\n", " ")
            if "require login" in error_text or "auth expired" in error_text:
                raise RuntimeError(f"Download server rejected the request: login expired. {error_text}")
            raise RuntimeError(f"Download server returned HTTP {response.status_code}: {error_text}")

        response_size = _response_body_size(response)
        if _looks_like_tiny_placeholder(response, response_size, declared_size):
            content_type = response.headers.get("Content-Type", "unknown")
            raise RuntimeError(
                f"Download server returned only {response_size} bytes ({content_type}) for a {declared_size}-byte file. "
                "The provider likely blocked this large-file direct download or returned a preview/placeholder response."
            )
        if _looks_like_error_document(response, response_size, declared_size):
            error_text = response.text[:500].replace("\r", " ").replace("\n", " ")
            raise RuntimeError(f"Download server returned an error document: {error_text}")

        total = _response_total_size(response, downloaded, declared_size)
        initial_downloaded = downloaded
        start_time = time.monotonic()
        progress = (
            tqdm(total=total, initial=downloaded, unit="B", unit_scale=True, desc=os.path.basename(target))
            if config.show_progress and tqdm
            else None
        )
        try:
            with open(temp, "ab" if downloaded else "wb") as f:
                for chunk in response.iter_content(chunk_size=config.chunk_size):
                    _raise_if_cancelled(config)
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if config.progress_callback:
                            elapsed = max(0.001, time.monotonic() - start_time)
                            speed = max(0, downloaded - initial_downloaded) / elapsed
                            config.progress_callback(downloaded, total, speed, "下载中")
                        if progress:
                            progress.update(len(chunk))
        finally:
            if progress:
                progress.close()

    if declared_size > 0 and os.path.getsize(temp) < declared_size:
        raise RuntimeError(f"Incomplete file: got {os.path.getsize(temp)} bytes, expected {declared_size}")
    os.replace(temp, target)


def _download_hls_once(url: str, target: str, temp: str, config: DownloadConfig) -> None:
    _raise_if_cancelled(config)
    if os.path.exists(temp):
        os.remove(temp)
    headers = {"User-Agent": config.user_agent}
    if config.referer:
        headers["Referer"] = config.referer
    headers.update({key: value for key, value in config.extra_headers.items() if value})

    session = requests.Session()
    session.trust_env = False
    segments = _load_hls_segments(session, url, headers, None)
    progress = tqdm(total=len(segments), unit="seg", desc=os.path.basename(target)) if config.show_progress and tqdm else None
    try:
        with open(temp, "wb") as f:
            for segment_url in segments:
                _raise_if_cancelled(config)
                with session.get(segment_url, headers=headers, stream=True, timeout=(config.connect_timeout, config.read_timeout)) as response:
                    if response.status_code >= 400:
                        error_text = response.text[:500].replace("\r", " ").replace("\n", " ")
                        raise RuntimeError(f"HLS segment returned HTTP {response.status_code}: {error_text}")
                    for chunk in response.iter_content(chunk_size=config.chunk_size):
                        _raise_if_cancelled(config)
                        if chunk:
                            f.write(chunk)
                if progress:
                    progress.update(1)
    finally:
        if progress:
            progress.close()
    os.replace(temp, target)


def _load_hls_segments(session: requests.Session, url: str, headers: Dict[str, str], parent_url: Optional[str]) -> List[str]:
    request_headers = dict(headers)
    if parent_url:
        request_headers["Referer"] = parent_url
    with session.get(url, headers=request_headers, timeout=(15, 60)) as response:
        if response.status_code >= 400:
            error_text = response.text[:500].replace("\r", " ").replace("\n", " ")
            raise RuntimeError(f"HLS playlist returned HTTP {response.status_code}: {error_text}")
        playlist = response.text

    lines = [line.strip() for line in playlist.splitlines() if line.strip()]
    nested = [urljoin(url, line) for line in lines if not line.startswith("#") and ".m3u8" in line.lower()]
    if nested:
        return _load_hls_segments(session, nested[-1], headers, url)

    segments = [urljoin(url, line) for line in lines if not line.startswith("#")]
    if not segments:
        raise RuntimeError("HLS playlist does not contain media segments")
    return segments


def _response_total_size(response: requests.Response, downloaded: int, declared_size: int) -> int:
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        return int(match.group(1))
    length = int(response.headers.get("Content-Length") or 0)
    if length:
        return downloaded + length
    return max(downloaded, declared_size)


def _response_body_size(response: requests.Response) -> int:
    length = response.headers.get("Content-Length") or response.headers.get("content-length") or ""
    try:
        return int(length)
    except ValueError:
        return 0


def _looks_like_error_document(response: requests.Response, response_size: int, declared_size: int) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    disposition = response.headers.get("Content-Disposition", "").lower()
    if "attachment" in disposition:
        return False
    if not any(kind in content_type for kind in ("xml", "html", "json", "text")):
        return False
    return declared_size <= 0 or response_size <= 1024 * 1024 or response_size < declared_size


def _looks_like_tiny_placeholder(response: requests.Response, response_size: int, declared_size: int) -> bool:
    if declared_size <= 1024 * 1024 or response_size <= 0:
        return False
    if response_size > 1024 * 1024 or response_size >= declared_size:
        return False
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type.startswith(("image/", "text/", "application/json", "application/xml")):
        return True
    return declared_size // max(response_size, 1) >= 1000


def _raise_if_cancelled(config: DownloadConfig) -> None:
    cancel_event = getattr(config, "cancel_event", None)
    if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
        raise DownloadCancelled("Download cancelled")


def _sleep_or_cancel(config: DownloadConfig, seconds: float) -> None:
    cancel_event = getattr(config, "cancel_event", None)
    if cancel_event is not None and hasattr(cancel_event, "wait"):
        if cancel_event.wait(max(0.0, seconds)):
            raise DownloadCancelled("Download cancelled")
        return
    time.sleep(seconds)
