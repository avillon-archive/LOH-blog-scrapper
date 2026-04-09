"""
utils.py - shared utilities
"""
import ctypes
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import requests

from config import (  # noqa: E402 — 중앙 설정에서 re-export
    BLOG_HOST,
    BLOG_RATE_LIMIT,
    BLOG_RATE_LIMIT_SMALL,
    DEFAULT_MAX_WORKERS,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    RETRY_DELAYS,
    ROOT_DIR,
    VALID_CATEGORIES,
)

# log_io.py re-export — 기존 `from utils import X` 호환성 유지
from log_io import (  # noqa: F401
    FailedLog,
    LineBuffer,
    append_line,
    csv_line,
    filter_file_lines,
    flush_all_buffers,
    load_done_file,
    load_failed_post_urls,
    load_image_map,
    load_posts,
    load_stale,
    remove_lines_by_prefix,
    write_text_unique,
)

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
shutdown_event = threading.Event()          # 전역 중단 신호


class _TokenBucket:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, rate: float, burst: int = 2) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst, self._tokens + (now - self._last) * self._rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


_blog_rate_limiter = _TokenBucket(BLOG_RATE_LIMIT)


def set_blog_rate_limit(rate: float) -> None:
    """배치 크기에 따라 rate limit 을 동적으로 변경한다."""
    global _blog_rate_limiter
    _blog_rate_limiter = _TokenBucket(rate)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Thread-local session holder for thread-safe connection pooling.
_session_local = threading.local()


def extract_category(soup: "BeautifulSoup") -> str:
    """첫 번째 article:tag meta 태그의 content 값을 반환한다.

    값이 VALID_CATEGORIES에 속하지 않으면 ""를 반환한다.
    여러 article:tag가 있을 경우 DOM 순서상 첫 번째만 사용한다.
    """
    tag = soup.find("meta", property="article:tag")
    if tag:
        value = (tag.get("content") or "").strip()
        if value in VALID_CATEGORIES:
            return value
    return ""


SIZE_W_RE = re.compile(r"/size/w\d+(?:h\d+)?", re.IGNORECASE)
_CLOVERGAMES_PREVIEW_RE = re.compile(
    r"(cdn\.clovergames\.io/image/loh/[a-z]{2})/p/",
)


def _strip_ref_param(url: str) -> str:
    """URL에서 ref 쿼리 파라미터를 제거한다 (Ghost CMS 참조 추적용)."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.query:
        return url
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "ref" not in params:
        return url
    params.pop("ref")
    new_query = urllib.parse.urlencode(params, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def clean_url(url: str) -> str:
    """Normalize URL for dedup: ref 제거, /size/wN 제거, /p/→/o/, trailing slash."""
    url = _strip_ref_param(url)
    url = SIZE_W_RE.sub("", url)
    url = _CLOVERGAMES_PREVIEW_RE.sub(r"\1/o/", url)
    return url.rstrip("/")


def date_to_folder(date_str: str) -> str:
    """'YYYY-MM-DD' -> 'YYYY/MM'."""
    parts = date_str.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return date_str or "unknown"




def get_session() -> requests.Session:
    """Return a thread-local requests.Session."""
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        })
        _session_local.session = session
    return session


def ensure_utf8_console():
    """Force UTF-8 I/O for Windows console."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def fetch_with_retry(
    url: str, method: str = "GET", timeout: int = DEFAULT_TIMEOUT, **kwargs
) -> "requests.Response | None":
    """
    Retry request up to MAX_RETRIES times with backoff (RETRY_DELAYS).
    Return response on success, None on failure.
    Stop immediately on 404/410.
    HTTP 429 는 Retry-After 를 존중하며 retry 횟수를 소모하지 않는다.
    블로그 도메인 요청은 토큰 버킷 rate limiter 를 거친다.
    """
    delays = RETRY_DELAYS
    max_retries = MAX_RETRIES
    is_blog = urllib.parse.urlparse(url).hostname == BLOG_HOST
    attempt = 0
    while attempt < max_retries:
        if is_blog:
            _blog_rate_limiter.acquire()
        try:
            resp = get_session().request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (404, 410):
                return None
            if status == 429:
                retry_after = e.response.headers.get("Retry-After", "")
                try:
                    wait_secs = min(int(retry_after), 120)
                except (ValueError, TypeError):
                    wait_secs = min(5 * (attempt + 1), 60)
                print(f"  [429] Rate limited: {url}, waiting {wait_secs}s")
                time.sleep(wait_secs)
                continue  # 429 는 attempt 를 소모하지 않음
            if attempt < max_retries - 1:
                time.sleep(delays[attempt])
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ):
            if attempt < max_retries - 1:
                time.sleep(delays[attempt])
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(delays[attempt])
        attempt += 1
    return None




def eta_str(done: int, total: int, start_time: float) -> str:
    """Return progress + elapsed time string."""
    elapsed = time.time() - start_time
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)
    pct = done / total * 100 if total else 0.0
    return f"[{done:5d}/{total} | {pct:5.1f}% | Elapsed {h:02d}:{m:02d}:{s:02d}]"




# ---------------------------------------------------------------------------
# URL → slug 헬퍼 (download_md / download_html 공통)
# ---------------------------------------------------------------------------

def url_to_slug(post_url: str) -> str:
    """URL 마지막 경로 세그먼트를 slug로 반환한다 (최대 120자)."""
    path = urllib.parse.urlparse(post_url).path
    parts = [part for part in path.split("/") if part]
    raw = parts[-1] if parts else re.sub(r"[^\w-]", "_", post_url)
    return raw[:120]




# ---------------------------------------------------------------------------
# 병렬 파이프라인 실행 헬퍼 (download_md / download_html 공통)
# ---------------------------------------------------------------------------


def run_pipeline(
    posts: list[tuple[str, str]],
    process_fn: Callable[[str, str], bool],
    failed_log: FailedLog,
    retry_mode: bool,
    label: str,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    """ThreadPoolExecutor 기반 병렬 파이프라인 실행 헬퍼.

    Args:
        posts:       (url, date) 리스트.
        process_fn:  (url, date) -> bool. True=성공, False=실패.
        failed_log:  FailedLog 인스턴스 (retry 필터링·삭제에 사용).
        retry_mode:  True 이면 실패 목록 기준으로 posts 를 필터링한다.
        label:       로그 출력용 레이블 (예: "MD", "HTML").
        max_workers: 병렬 워커 수.
    """
    if retry_mode:
        fail_posts = failed_log.load_post_urls()
        posts = [(url, date, *rest) for url, date, *rest in posts if url in fail_posts]
        print(f"[{label}] 재처리 대상: {len(posts)}개 포스트")
        if not posts:
            print(f"[{label}] 재처리 대상이 없습니다.")
            return

    total = len(posts)
    # 대상 수가 100개 이하면 10개 단위, 초과면 50개 단위로 진행도 출력
    report_interval = 10 if total <= 100 else 50
    start = time.time()
    ok_count = 0
    fail_count = 0
    completed = 0
    counter_lock = threading.Lock()

    cancelled_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(process_fn, url, date): (url, date)
            for url, date, *_ in posts
        }
        for future in as_completed(future_to_post):
            post_url, _ = future_to_post[future]
            try:
                success = future.result()
            except CancelledError:
                cancelled_count += 1
                continue
            except Exception as exc:
                print(f"  [오류] {post_url}: {exc}")
                success = False

            with counter_lock:
                if success:
                    ok_count += 1
                else:
                    fail_count += 1
                completed += 1
                cur_completed = completed

            if success and retry_mode:
                failed_log.remove(post_url)

            if cur_completed % report_interval == 0 or cur_completed == total:
                print(f"  {eta_str(cur_completed, total, start)} 성공={ok_count} 실패={fail_count}")

            if shutdown_event.is_set():
                for f in future_to_post:
                    f.cancel()
                break

    if shutdown_event.is_set():
        print(f"\n[{label} 중단] 성공={ok_count}, 실패={fail_count}, 취소={total - completed - cancelled_count}")
    else:
        print(f"\n[{label} 완료] 성공={ok_count}, 실패={fail_count}")


# ---------------------------------------------------------------------------
# HTML 인덱스 & 캐시 읽기 (파이프라인 간 HTML 재활용)
# ---------------------------------------------------------------------------


def build_html_index(html_dir: Path, done_file: Path) -> dict[str, Path]:
    """done_html.txt 와 html 디렉토리를 스캔하여 {post_url: html_path} 매핑을 반환한다."""
    done_map = load_done_file(done_file)  # {slug: url}
    slug_to_path: dict[str, Path] = {}
    for html_file in html_dir.rglob("*.html"):
        slug_to_path[html_file.stem] = html_file
    index: dict[str, Path] = {}
    for slug, url in done_map.items():
        if slug in slug_to_path:
            index[url] = slug_to_path[slug]
    return index


def fetch_post_html(url: str, html_index: "dict[str, Path] | None" = None) -> str | None:
    """html_index 에서 로컬 파일을 먼저 확인하고, 없으면 서버에서 fetch 한다."""
    if html_index is not None and url in html_index:
        path = html_index[url]
        if path.exists():
            return path.read_text(encoding="utf-8")
    resp = fetch_with_retry(url)
    return resp.text if resp is not None else None
