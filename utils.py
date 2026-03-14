"""
utils.py - shared utilities
"""
import ctypes
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import requests

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

# File I/O lock for append/filter/remove helpers.
_file_lock = threading.Lock()

# Delay after successful HTTP request to reduce server pressure.
REQUEST_DELAY: float = 0.2

# Default number of parallel workers for ThreadPoolExecutor.
DEFAULT_MAX_WORKERS: int = 32

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Thread-local session holder for thread-safe connection pooling.
_session_local = threading.local()

# ---------------------------------------------------------------------------
# 카테고리 관련 상수·헬퍼
# ---------------------------------------------------------------------------

VALID_CATEGORIES: frozenset[str] = frozenset([
    "공지사항", "이벤트", "갤러리", "유니버스", "아발론서고",
    "쿠폰", "아발론 이벤트", "Special", "가이드", "확률 정보",
])


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


SIZE_W_RE = re.compile(r"/size/w\d+", re.IGNORECASE)


def clean_url(url: str) -> str:
    """Normalize URL for dedup: remove /size/wN and trailing slash."""
    url = SIZE_W_RE.sub("", url)
    return url.rstrip("/")


def date_to_folder(date_str: str) -> str:
    """'YYYY-MM-DD' -> 'YYYY/MM'."""
    parts = date_str.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return date_str or "unknown"


def load_image_map(filepath: Path) -> dict[str, str]:
    """Load image_map.tsv into {clean_url: relative_path}."""
    image_map: dict[str, str] = {}
    if not filepath.exists():
        return image_map
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip()
        if not row:
            continue
        parts = row.split("\t", 1)
        if len(parts) != 2:
            continue
        image_map[parts[0].strip()] = parts[1].strip()
    return image_map


def load_done_file(filepath: Path) -> dict[str, str]:
    """Load done_md.txt / done_html.txt into {slug: post_url}."""
    done: dict[str, str] = {}
    if filepath.exists():
        for line in filepath.read_text(encoding="utf-8").splitlines():
            row = line.strip()
            if not row:
                continue
            parts = row.split("\t", 1)
            if len(parts) == 2 and parts[1].strip():
                done[parts[0].strip()] = parts[1].strip()
    return done


def load_failed_post_urls(filepath: Path) -> set[str]:
    """Return first-column post URLs from failed log file."""
    failed: set[str] = set()
    if not filepath.exists():
        return failed
    for line in filepath.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip():
            failed.add(parts[0].strip())
    return failed


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
    url: str, method: str = "GET", timeout: int = 20, **kwargs
) -> "requests.Response | None":
    """
    Retry request up to 3 times with backoff (1s, 2s).
    Return response on success, None on failure.
    Stop immediately on 404/410.
    """
    delays = [1, 2]
    for attempt in range(3):
        try:
            resp = get_session().request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (404, 410):
                return None
            if attempt < 2:
                time.sleep(delays[attempt])
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ):
            if attempt < 2:
                time.sleep(delays[attempt])
        except Exception:
            if attempt < 2:
                time.sleep(delays[attempt])
    return None


def load_posts(filepath: str | Path) -> list[tuple[str, str]]:
    """
    Load all_posts.txt / custom_posts.txt
    Each line: URL<TAB>YYYY-MM-DD
    """
    posts = []
    path = Path(filepath)
    if not path.exists():
        print(f"[warning] file not found: {filepath}")
        return posts
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                posts.append((parts[0].strip(), parts[1].strip()))
            elif len(parts) == 1:
                posts.append((parts[0].strip(), ""))
    return posts


def append_line(filepath: Path, line: str) -> None:
    """Append one line to file (thread-safe)."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def filter_file_lines(filepath: Path, keep_fn: Callable[[str], bool]) -> None:
    """Filter lines by predicate (thread-safe, in-place)."""
    if not filepath.exists():
        return
    with _file_lock:
        lines = filepath.read_text(encoding="utf-8").splitlines()
        new_lines = [line for line in lines if keep_fn(line)]
        filepath.write_text(
            "\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8"
        )


def remove_lines_by_prefix(filepath: Path, prefix: str) -> None:
    """Remove lines that start with prefix (thread-safe, in-place)."""
    filter_file_lines(filepath, lambda line: not line.startswith(prefix))


def eta_str(done: int, total: int, start_time: float) -> str:
    """Return progress + elapsed time string."""
    elapsed = time.time() - start_time
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)
    pct = done / total * 100 if total else 0.0
    return f"[{done:5d}/{total} | {pct:5.1f}% | Elapsed {h:02d}:{m:02d}:{s:02d}]"


# ---------------------------------------------------------------------------
# 공통 실패 이력 관리
# ---------------------------------------------------------------------------


class FailedLog:
    """파일 기반 실패 이력 (post_url, reason)을 스레드 안전하게 관리.

    download_md.py 및 download_html.py 에서 공통으로 사용된다.
    download_images.py 는 3-tuple (post_url, img_url, reason) 구조를 사용하므로
    별도 구현을 유지한다.
    """

    def __init__(self, filepath: Path, lock: threading.Lock) -> None:
        self._filepath = filepath
        self._lock = lock
        self._cache: set[tuple[str, str]] | None = None

    def _load(self) -> set[tuple[str, str]]:
        entries: set[tuple[str, str]] = set()
        if not self._filepath.exists():
            return entries
        for line in self._filepath.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                entries.add((parts[0].strip(), parts[1].strip()))
        return entries

    def record(self, post_url: str, reason: str) -> None:
        """실패 항목을 기록한다. 동일 (post_url, reason) 쌍은 중복 기록하지 않는다."""
        key = (post_url, reason)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            if key in self._cache:
                return
            self._cache.add(key)
        append_line(self._filepath, f"{post_url}\t{reason}")

    def remove(self, post_url: str) -> None:
        """post_url 에 해당하는 모든 실패 항목을 삭제한다."""
        remove_lines_by_prefix(self._filepath, post_url + "\t")
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            self._cache = {k for k in self._cache if k[0] != post_url}

    def load_post_urls(self) -> set[str]:
        """실패 목록의 post_url 집합을 반환한다."""
        return load_failed_post_urls(self._filepath)


# ---------------------------------------------------------------------------
# slug 충돌 해소 + 텍스트 파일 저장
# ---------------------------------------------------------------------------


def write_text_unique(
    target_dir: Path,
    slug: str,
    suffix: str,
    content: str,
    done_map: dict[str, str],
    done_urls: set[str],
    post_url: str,
    lock: threading.Lock,
    done_file: Path,
    force_overwrite: bool = False,
) -> str | None:
    """slug 충돌을 해소하며 텍스트 파일을 저장하고 done 상태를 갱신한다.

    download_md.py 와 download_html.py 에서 동일하게 사용되는
    "잠금 외부 탐색 → 잠금 내부 확정·쓰기" 패턴을 공통화한다.

    1단계(잠금 외부): 동일 내용 파일 탐색 (I/O 집중 구간)
    2단계(잠금 내부): 최종 경로 확정·쓰기·done 상태 갱신

    Args:
        force_overwrite: True 이면 동일 slug 파일이 존재하고 내용이 다를 때
                         _2 suffix 없이 기존 파일을 덮어쓴다.

    Returns:
        실제 저장(또는 기존 일치) 파일의 slug 문자열.
        post_url 이 already-done 상태이면 None.
    """
    # 빠른 비잠금 확인
    if post_url in done_urls:
        return None

    # ── 1단계: 충돌 탐색 (잠금 외부) ──────────────────────────────────
    path = target_dir / f"{slug}{suffix}"
    next_idx = 2
    if force_overwrite:
        # force_overwrite: 기존 slug 경로를 그대로 사용 (덮어쓰기 대상)
        pass
    else:
        while path.exists():
            try:
                if path.read_text(encoding="utf-8") == content:
                    break  # 동일 내용 → 이 경로를 후보로 사용
            except OSError:
                pass
            path = target_dir / f"{slug}_{next_idx}{suffix}"
            next_idx += 1

    # ── 2단계: 경로 확정·쓰기·상태 갱신 (잠금 내부) ───────────────────
    actual_slug: str | None = None
    with lock:
        if post_url in done_urls:
            return None  # 다른 스레드가 먼저 완료

        if path.exists():
            try:
                on_disk = path.read_text(encoding="utf-8")
            except OSError:
                on_disk = None

            if on_disk == content:
                # 동일 내용 → 중복으로 처리
                actual_slug = path.stem
                if actual_slug not in done_map:
                    done_map[actual_slug] = post_url
                done_urls.add(post_url)
            elif force_overwrite:
                # force_overwrite: 내용이 다르면 기존 파일 덮어쓰기
                path.write_text(content, encoding="utf-8")
                actual_slug = path.stem
                done_map[actual_slug] = post_url
                done_urls.add(post_url)
            else:
                # 선점됨 → 잠금 내에서 빈 슬롯 탐색 후 쓰기
                while path.exists():
                    path = target_dir / f"{slug}_{next_idx}{suffix}"
                    next_idx += 1
                path.write_text(content, encoding="utf-8")
                actual_slug = path.stem
                done_map[actual_slug] = post_url
                done_urls.add(post_url)
        else:
            path.write_text(content, encoding="utf-8")
            actual_slug = path.stem
            done_map[actual_slug] = post_url
            done_urls.add(post_url)

    # 잠금 해제 후 파일 I/O
    if actual_slug is not None:
        append_line(done_file, f"{actual_slug}\t{post_url}")
    return actual_slug


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
# 지연 flush 파일 버퍼 (download_images.py 고빈도 I/O 최적화)
# ---------------------------------------------------------------------------


class LineBuffer:
    """스레드 안전한 지연 flush 파일 버퍼.

    append_line 호출을 메모리에 누적하다가 flush_every 건 이상 쌓이면
    자동으로 파일에 일괄 기록한다. 프로세스 종료 전 flush_all()을 반드시
    호출해야 미기록 데이터 유실을 방지할 수 있다.

    download_images.py 의 고빈도 파일(downloaded_urls.txt, image_map.tsv 등)에
    사용하기 위해 설계됐다. 모듈 수준 append_line 과 달리 _file_lock 을 경유하지
    않으므로 _state_lock / _save_lock 과 경합하지 않는다.
    """

    def __init__(self, filepath: Path, flush_every: int = 100) -> None:
        self._filepath = filepath
        self._flush_every = flush_every
        self._buf: list[str] = []
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        """줄을 버퍼에 추가한다. 버퍼가 flush_every 건을 초과하면 자동 flush."""
        with self._lock:
            self._buf.append(line)
            if len(self._buf) >= self._flush_every:
                self._flush_locked()

    def flush_all(self) -> None:
        """버퍼에 남은 모든 줄을 파일에 기록한다. run 종료 시 반드시 호출."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """_lock 보유 상태에서 호출해야 한다."""
        if not self._buf:
            return
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(self._filepath, "a", encoding="utf-8") as f:
            f.write("\n".join(self._buf) + "\n")
        self._buf.clear()


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
        posts = [(url, date) for url, date in posts if url in fail_posts]
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

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(process_fn, url, date): (url, date)
            for url, date in posts
        }
        for future in as_completed(future_to_post):
            post_url, _ = future_to_post[future]
            try:
                success = future.result()
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

    print(f"\n[{label} 완료] 성공={ok_count}, 실패={fail_count}")
