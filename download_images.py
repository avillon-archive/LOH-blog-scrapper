"""Image downloader for Lord of Heroes blog posts."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import mimetypes
from pathlib import Path
import re
import threading
import time
import urllib.parse

from bs4 import BeautifulSoup

from utils import (
    DEFAULT_MAX_WORKERS,
    append_line,
    clean_url,
    date_to_folder,
    ensure_utf8_console,
    eta_str,
    fetch_with_retry,
    filter_file_lines,
    load_failed_post_urls,
    load_image_map,
    load_posts,
    remove_lines_by_prefix,
)

ROOT_DIR = Path(__file__).parent / "loh_blog"
IMAGES_DIR = ROOT_DIR / "images"
DONE_FILE = ROOT_DIR / "downloaded_urls.txt"
DONE_POSTS_FILE = ROOT_DIR / "done_posts_images.txt"  # 이미지 완료 포스트 URL 목록
FAILED_FILE = ROOT_DIR / "failed_images.txt"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"
THUMB_HASH_FILE = ROOT_DIR / "thumbnail_hashes.txt"  # 썸네일 해시 캐시

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
DL_KEYWORDS = {"다운로드", "download", "다운", "받기", "저장"}
RESOLUTION_RE = re.compile(r"\d+\s*[xX×]\s*\d+")
BLOG_HOST = "blog-ko.lordofheroes.com"
GDRIVE_HOSTS = {"drive.google.com", "docs.google.com", "lh3.googleusercontent.com"}
COMMUNITY_CDN_HOST = "community-ko-cdn.lordofheroes.com"
WAYBACK_CDX_API = "https://web.archive.org/cdx/search/cdx"

# ---------------------------------------------------------------------------
# 스레드 안전을 위한 잠금
# ---------------------------------------------------------------------------

# seen_urls / og_hashes / image_map 갱신 및 파일 저장을 원자적으로 처리
_dl_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Wayback CDX 캐시
# ---------------------------------------------------------------------------

_wayback_cache: dict[str, str | None] = {}
_wayback_events: dict[str, threading.Event] = {}  # 진행 중인 CDX 요청 추적
_wayback_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 이미지 실패 이력 관리 (3-tuple: post_url, img_url, reason)
# ---------------------------------------------------------------------------


class ImageFailedLog:
    """download_images.py 전용 실패 이력 관리.

    utils.FailedLog 는 2-tuple(post_url, reason) 기반이므로,
    img_url 을 포함하는 3-tuple 구조를 위해 독립 클래스로 구현한다.
    lock 내부에서 캐시 갱신, lock 외부에서 파일 기록 패턴을 동일하게 적용한다.
    """

    def __init__(self, filepath: Path, lock: threading.Lock) -> None:
        self._filepath = filepath
        self._lock = lock
        self._cache: set[tuple[str, str, str]] | None = None

    def _load(self) -> set[tuple[str, str, str]]:
        entries: set[tuple[str, str, str]] = set()
        if not self._filepath.exists():
            return entries
        for line in self._filepath.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                post_url = parts[0].strip()
                img_url = parts[1].strip()
                reason = parts[2].strip()
                if post_url and reason:
                    entries.add((post_url, img_url, reason))
        return entries

    def record(self, post_url: str, img_url: str, reason: str) -> None:
        """실패 항목을 기록한다. 동일 3-tuple은 중복 기록하지 않는다."""
        key = (post_url, img_url, reason)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            if key in self._cache:
                return
            self._cache.add(key)
        append_line(self._filepath, f"{post_url}\t{img_url}\t{reason}")

    def remove(self, post_url: str, reason: str | None = None) -> None:
        if not self._filepath.exists():
            return
        prefix = post_url + "\t"
        if reason is None:
            remove_lines_by_prefix(self._filepath, prefix)
            with self._lock:
                if self._cache is None:
                    self._cache = self._load()
                self._cache = {e for e in self._cache if e[0] != post_url}
        else:
            def _keep(line: str) -> bool:
                if not line.startswith(prefix):
                    return True
                parts = line.split("\t")
                return (parts[2].strip() if len(parts) >= 3 else "") != reason
            filter_file_lines(self._filepath, _keep)
            with self._lock:
                if self._cache is None:
                    self._cache = self._load()
                self._cache = {e for e in self._cache if not (e[0] == post_url and e[2] == reason)}

    def load_post_urls(self) -> set[str]:
        return load_failed_post_urls(self._filepath)


_failed_log = ImageFailedLog(FAILED_FILE, _dl_lock)


# ---------------------------------------------------------------------------
# 데이터클래스
# ---------------------------------------------------------------------------


@dataclass
class PostProcessResult:
    ok: int
    fail: int
    post_fetch_ok: bool


# ---------------------------------------------------------------------------
# 해시 유틸
# ---------------------------------------------------------------------------


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_hash_index(folder: Path) -> set[str]:
    hashes: set[str] = set()
    if not folder.exists():
        return hashes
    for file_path in folder.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            hashes.add(_sha256_bytes(file_path.read_bytes()))
        except OSError:
            continue
    return hashes


def _load_or_build_og_hashes() -> set[str]:
    """
    썸네일 해시를 캐시 파일(thumbnail_hashes.txt)에서 로드.
    캐시가 없으면 thumbnails 폴더를 스캔해 해시를 계산하고 캐시 파일 생성. (최초 1회)
    """
    if THUMB_HASH_FILE.exists():
        hashes = set(THUMB_HASH_FILE.read_text(encoding="utf-8").splitlines())
        hashes.discard("")
        return hashes
    # 최초 실행: 전체 스캔 후 캐시 저장
    hashes = _build_hash_index(IMAGES_DIR / "thumbnails")
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_HASH_FILE.write_text("\n".join(hashes) + ("\n" if hashes else ""), encoding="utf-8")
    return hashes


# ---------------------------------------------------------------------------
# seen 키 헬퍼
# ---------------------------------------------------------------------------


def _seen_scope(utype: str) -> str:
    return "thumb" if utype == "og_image" else "main"


def _seen_key(utype: str, url: str) -> str:
    return f"{_seen_scope(utype)}:{clean_url(url)}"


def _is_community_cdn(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() == COMMUNITY_CDN_HOST


def _normalized_link_key(url: str) -> str:
    """Wayback 재작성을 고려한 URL 정규화 (링크 매칭용)."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = urllib.parse.unquote(parsed.path or "")
    if path != "/":
        path = path.rstrip("/")
    return f"{host}{path}"


# ---------------------------------------------------------------------------
# 파일명 유틸
# ---------------------------------------------------------------------------


def _ext_from_mime(mime: str) -> str:
    ext = mimetypes.guess_extension(mime.split(";")[0].strip())
    if ext == ".jpe":
        ext = ".jpg"
    return ext or ".bin"


def _basename(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return Path(path).name or ""


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name[:200] or "image"


def save_image(content: bytes, filename: str, folder: Path) -> str:
    """bytes를 folder/filename에 저장하고 충돌을 해소한다.

    - 동일 내용의 파일이 이미 존재하면 해당 파일명을 반환한다.
    - 새로 저장하면 실제 저장된 파일명을 반환한다.
    - None을 반환하지 않으므로 호출 측에서 항상 유효한 경로를 얻는다.
    """
    folder.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(filename)
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".bin"

    target = folder / filename
    idx = 2
    while target.exists():
        if target.read_bytes() == content:
            return target.name  # 동일 내용 기존 파일명 반환
        target = folder / f"{stem}_{idx}{suffix}"
        idx += 1

    target.write_bytes(content)
    return target.name


# ---------------------------------------------------------------------------
# image_map 헬퍼
# ---------------------------------------------------------------------------


def record_image_map(
    clean_url_key: str,
    relative_path: str,
    image_map: dict[str, str],
    filepath: Path,
):
    """image_map 딕셔너리와 파일에 항목을 추가한다 (중복 방지)."""
    if not clean_url_key or not relative_path:
        return
    existing = image_map.get(clean_url_key)
    if existing == relative_path:
        return
    image_map[clean_url_key] = relative_path
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    append_line(filepath, f"{clean_url_key}\t{relative_path}")


# ---------------------------------------------------------------------------
# done / failed 파일 헬퍼
# ---------------------------------------------------------------------------


def _load_done_post_urls(filepath: Path) -> set[str]:
    """이미지 수집이 완료된 포스트 URL 집합을 반환한다."""
    if not filepath.exists():
        return set()
    return {
        line.strip()
        for line in filepath.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _parse_done_line_to_main_url(row: str) -> str | None:
    if row.startswith("thumb:"):
        return None
    if row.startswith("main:"):
        return row.split(":", 1)[1].strip() or None
    return row.strip() or None


def load_seen(filepath: Path) -> set[str]:
    seen: set[str] = set()
    if filepath.exists():
        for line in filepath.read_text(encoding="utf-8").splitlines():
            row = line.strip()
            if not row:
                continue
            if row.startswith("main:") or row.startswith("thumb:"):
                seen.add(row)
            else:
                seen.add(f"main:{row}")
    return seen


def record_seen(seen_key: str, seen_urls: set[str], filepath: Path):
    """seen_urls 집합에 키를 추가하고 파일에 기록한다 (잠금 외부에서 호출)."""
    seen_urls.add(seen_key)
    append_line(filepath, seen_key)


def _load_failed_image_entries() -> set[tuple[str, str, str]]:
    """하위 호환용 래퍼 (내부 사용 전용)."""
    return _failed_log._load()  # type: ignore[attr-defined]


def record_failed(post_url: str, img_url: str, reason: str) -> None:
    """_failed_log.record 의 모듈 수준 래퍼."""
    _failed_log.record(post_url, img_url, reason)


def remove_from_failed(post_url: str, reason: str | None = None) -> None:
    """_failed_log.remove 의 모듈 수준 래퍼."""
    _failed_log.remove(post_url, reason)


# ---------------------------------------------------------------------------
# backfill (--backfill-map 옵션)
# ---------------------------------------------------------------------------


def backfill_image_map():
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    image_map = load_image_map(IMAGE_MAP_FILE)

    _thumb_dir = (IMAGES_DIR / "thumbnails").resolve()
    files = [
        f
        for f in IMAGES_DIR.rglob("*")
        if f.is_file()
        and f.suffix.lower() in IMG_EXTS
        and _thumb_dir not in f.resolve().parents
    ]

    by_name: dict[str, list[Path]] = {}
    for f in files:
        by_name.setdefault(f.name, []).append(f)

    added = 0
    if DONE_FILE.exists():
        for line in DONE_FILE.read_text(encoding="utf-8").splitlines():
            url = _parse_done_line_to_main_url(line.strip())
            if not url:
                continue
            key = clean_url(url)
            if key in image_map:
                continue
            base = _basename(url)
            if not base:
                continue
            candidates = by_name.get(base, [])
            if not candidates:
                stem = Path(base).stem
                suffix = Path(base).suffix
                pattern = re.compile(rf"^{re.escape(stem)}(?:_\d+)?{re.escape(suffix)}$")
                for name, name_paths in by_name.items():
                    if pattern.match(name):
                        candidates.extend(name_paths)
            if not candidates:
                continue
            chosen = sorted(candidates, key=lambda x: str(x))[0]
            rel = chosen.relative_to(ROOT_DIR).as_posix()
            record_image_map(key, rel, image_map, IMAGE_MAP_FILE)
            added += 1

    print(f"[MAP] backfill added={added} total={len(image_map)}")


# ---------------------------------------------------------------------------
# Wayback 헬퍼
# ---------------------------------------------------------------------------


def _wayback_oldest(url: str) -> str | None:
    """Wayback CDX API에서 가장 오래된 200 스냅샷 URL을 반환한다.

    동일 URL에 대해 여러 스레드가 동시에 CDX 요청을 보내는 것을 방지하기 위해
    이벤트 기반 대기를 사용한다. 먼저 도달한 스레드가 fetch를 수행하고,
    나중에 도달한 스레드는 완료 이벤트를 기다린 뒤 캐시에서 결과를 읽는다.
    """
    with _wayback_cache_lock:
        if url in _wayback_cache:
            return _wayback_cache[url]
        if url in _wayback_events:
            # 다른 스레드가 이미 fetch 중 → 완료 대기
            event = _wayback_events[url]
            do_fetch = False
        else:
            # 이 스레드가 fetch를 담당
            event = threading.Event()
            _wayback_events[url] = event
            do_fetch = True

    if not do_fetch:
        event.wait()
        with _wayback_cache_lock:
            return _wayback_cache.get(url)

    # fetch 수행 (잠금 외부 – 네트워크 I/O)
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original",
        "filter": "statuscode:200",
        "limit": "1",
    }
    result: str | None = None
    try:
        resp = fetch_with_retry(WAYBACK_CDX_API, params=params, timeout=15)
        if resp is not None:
            try:
                rows = resp.json()
                if isinstance(rows, list) and len(rows) >= 2:
                    first = rows[1]
                    if isinstance(first, list) and len(first) >= 2:
                        timestamp = str(first[0]).strip()
                        original = str(first[1]).strip()
                        if timestamp and original:
                            result = f"https://web.archive.org/web/{timestamp}/{original}"
            except Exception:
                pass
    finally:
        with _wayback_cache_lock:
            _wayback_cache[url] = result
            _wayback_events.pop(url, None)
        event.set()
    return result


def _add_im(wayback_url: str) -> str:
    return re.sub(r"(/web/\d+)/", r"\1im_/", wayback_url)


# Wayback 재작성 URL 패턴: /web/{timestamp}[modifier]/original_url
_WAYBACK_ORIGINAL_RE = re.compile(
    r"^https?://web\.archive\.org/web/\d+[a-z_]*/(.+)$", re.IGNORECASE
)


def _original_url_from_wayback(url: str) -> str:
    """Wayback 재작성 URL에서 원본 URL을 추출한다. 일반 URL이면 그대로 반환한다."""
    m = _WAYBACK_ORIGINAL_RE.match(url)
    return m.group(1) if m else url


def _filename_from_cd(cd: str) -> str:
    if not cd:
        return ""
    utf8_match = re.search(r"filename\*\s*=\s*([^']+)''(.+)", cd, re.IGNORECASE)
    if utf8_match:
        try:
            return urllib.parse.unquote(utf8_match.group(2).strip())
        except Exception:
            pass
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return ""


def _is_image_ct(content_type: str) -> bool:
    return content_type.lower().startswith("image/")


def _response_to_image(
    resp,
    *,
    allow_ext_fallback: bool = False,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    if not resp or not getattr(resp, "content", None):
        return None
    content = resp.content
    if len(content) < min_bytes:
        return None
    content_type = resp.headers.get("Content-Type", "")
    is_image_type = _is_image_ct(content_type)
    has_image_ext = Path(urllib.parse.urlparse(resp.url).path).suffix.lower() in IMG_EXTS
    if not is_image_type and not (allow_ext_fallback and has_image_ext):
        return None
    return (
        content,
        resp.url,
        content_type,
        resp.headers.get("Content-Disposition", ""),
    )


def _fetch_image(
    url: str,
    *,
    allow_ext_fallback: bool = False,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    resp = fetch_with_retry(url, allow_redirects=True)
    return _response_to_image(resp, allow_ext_fallback=allow_ext_fallback, min_bytes=min_bytes)


def _fetch_wayback_image(
    url: str,
    *,
    allow_ext_fallback: bool = False,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    wayback_url = _wayback_oldest(url)
    if not wayback_url:
        return None
    return _fetch_image(
        _add_im(wayback_url),
        allow_ext_fallback=allow_ext_fallback,
        min_bytes=min_bytes,
    )


def _fetch_wayback_post_soup(
    post_url: str,
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> tuple[BeautifulSoup, str] | None:
    if post_soup_cache is not None and post_url in post_soup_cache:
        return post_soup_cache[post_url]

    wayback_post = _wayback_oldest(post_url)
    if not wayback_post:
        if post_soup_cache is not None:
            post_soup_cache[post_url] = None
        return None

    resp = fetch_with_retry(wayback_post, allow_redirects=True)
    if not resp:
        if post_soup_cache is not None:
            post_soup_cache[post_url] = None
        return None

    resp.encoding = resp.apparent_encoding or "utf-8"
    parsed = (BeautifulSoup(resp.text, "lxml"), wayback_post)
    if post_soup_cache is not None:
        post_soup_cache[post_url] = parsed
    return parsed


def _fetch_wayback_gdrive_from_post(
    post_url: str,
    original_img_url: str,
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> tuple[bytes, str, str, str] | None:
    soup_with_base = _fetch_wayback_post_soup(post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base

    # 수집 단계에서 탐색한 original_img_url 기준으로 매칭해
    # 포스트에 gdrive 이미지가 여러 개인 경우에도 정확한 이미지만 반환한다.
    target_key = _normalized_link_key(original_img_url)

    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src:
            continue
        candidate_url = urllib.parse.urljoin(wayback_post, src)
        # Wayback이 src를 /web/{ts}/https://lh3.googleusercontent.com/... 형태로
        # 재작성하므로, 원본 URL을 추출한 뒤 hostname을 파싱해 GDRIVE_HOSTS와 비교한다.
        # 이렇게 하면 수집 단계(collect_image_urls)의 hostname in GDRIVE_HOSTS 조건과
        # 완전히 동일한 기준을 유지할 수 있다.
        original_url = _original_url_from_wayback(candidate_url)
        hostname = (urllib.parse.urlparse(original_url).hostname or "").lower()
        if hostname not in GDRIVE_HOSTS:
            continue
        if _normalized_link_key(original_url) != target_key:
            continue
        # Wayback HTML 래퍼가 아닌 이미지 바이너리를 직접 가져오기 위해 im_ 적용
        image = _fetch_image(_add_im(candidate_url))
        if image:
            return image
    return None


def _fetch_wayback_img_from_post(
    post_url: str,
    original_img_url: str,
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> tuple[bytes, str, str, str] | None:
    """Wayback 포스트 스냅샷에서 original_img_url과 URL이 일치하는 img/og:image를 탐색해 다운로드한다.

    img 타입: <img src> 태그에서 _normalized_link_key 완전 일치로 후보를 찾는다.
    og_image 타입: <meta property="og:image"> 도 함께 탐색한다.
    """
    soup_with_base = _fetch_wayback_post_soup(post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base

    target_key = _normalized_link_key(original_img_url)
    if not target_key:
        return None

    # og:image meta → img 태그 순으로 후보 수집
    candidates: list[str] = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        candidates.append(urllib.parse.urljoin(wayback_post, og["content"]))
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if src:
            candidates.append(urllib.parse.urljoin(wayback_post, src))

    for candidate_url in candidates:
        if _normalized_link_key(candidate_url) == target_key:
            image = _fetch_image(_add_im(candidate_url))
            if image:
                return image

    return None


def _fetch_wayback_linked_from_post(
    post_url: str,
    original_link: str,
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> tuple[bytes, str, str, str] | None:
    soup_with_base = _fetch_wayback_post_soup(post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base
    target_key = _normalized_link_key(original_link)
    if not target_key:
        # 비정상 URL이면 모든 앵커를 순회하는 오작동을 방지.
        return None

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        absolute_href = urllib.parse.urljoin(wayback_post, href)
        href_key = _normalized_link_key(absolute_href)
        if target_key != href_key:
            continue
        image = _fetch_image(_add_im(absolute_href))
        if image:
            return image

    return None


# ---------------------------------------------------------------------------
# 이미지 URL 수집
# ---------------------------------------------------------------------------


def collect_image_urls(soup: BeautifulSoup, post_url: str) -> list[tuple[str, str]]:
    """(url, type) 리스트를 반환한다."""
    results: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _add(url: str, utype: str):
        if not url or not url.startswith("http"):
            return
        scope = "thumb" if utype == "og_image" else "main"
        key = (scope, clean_url(url))
        if key in seen_keys:
            return
        seen_keys.add(key)
        results.append((url, utype))

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        _add(og["content"], "og_image")

    # 본문 범위를 가능한 좁게 잡아 author bio 등 비본문 이미지를 배제한다.
    # Ghost 구조: .gh-content > .post-content > article > main 순으로 좁은 범위 우선.
    content_tag = (
        soup.select_one(".gh-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.find("main")
        or soup
    )

    for img in content_tag.find_all("img"):
        if "author-profile-image" in (img.get("class") or []):
            continue
        if img.find_parent("div", class_="author-card"):
            continue
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        abs_src = urllib.parse.urljoin(post_url, src)
        parsed_src = urllib.parse.urlparse(abs_src)
        hostname = (parsed_src.hostname or "").lower()
        path_ext = Path(parsed_src.path).suffix.lower()
        if hostname in GDRIVE_HOSTS:
            _add(abs_src, "gdrive")
        elif "/content/images/" in parsed_src.path and hostname == BLOG_HOST:
            # 자사 블로그 호스트의 Ghost 콘텐츠 이미지만 수집
            _add(abs_src, "img")
        elif hostname == COMMUNITY_CDN_HOST and path_ext in IMG_EXTS:
            _add(abs_src, "img")

    for anchor in content_tag.find_all("a", href=True):
        if anchor.find_parent("div", class_="author-card"):
            continue
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:")):
            continue
        abs_href = urllib.parse.urljoin(post_url, href)
        parsed = urllib.parse.urlparse(abs_href)
        if parsed.hostname in GDRIVE_HOSTS:
            _add(abs_href, "gdrive")
            continue
        path_ext = Path(parsed.path).suffix.lower()
        if path_ext in IMG_EXTS:
            _add(abs_href, "linked_direct")
            continue
        anchor_text = anchor.get_text(strip=True)
        if any(keyword in anchor_text.lower() for keyword in DL_KEYWORDS) or RESOLUTION_RE.search(
            anchor_text
        ):
            _add(abs_href, "linked_keyword")

    return results


# ---------------------------------------------------------------------------
# 파일명 결정
# ---------------------------------------------------------------------------


def _determine_filename(
    utype: str,
    original_href: str,
    final_url: str,
    content_type: str,
    cd_header: str,
    idx: int,
) -> str:
    cd_name = _filename_from_cd(cd_header)

    if utype in ("img", "og_image"):
        final_base = _basename(final_url)
        if final_base:
            return final_base
        original_base = _basename(original_href)
        if original_base:
            return original_base
        return f"image_{idx}{_ext_from_mime(content_type)}"

    if utype in ("linked_direct", "linked_keyword"):
        if cd_name:
            return cd_name
        final_base = _basename(final_url)
        if final_base and Path(final_base).suffix.lower() in IMG_EXTS:
            return final_base
        original_base = _basename(original_href)
        if original_base and Path(original_base).suffix.lower() in IMG_EXTS:
            return original_base
        return f"image_{idx}{_ext_from_mime(content_type)}"

    # gdrive
    if cd_name:
        return cd_name
    parsed = urllib.parse.urlparse(original_href)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("name", "title"):
        if key in query and query[key]:
            return query[key][0]
    digest = hashlib.md5(original_href.encode()).hexdigest()[:12]
    return f"{digest}{_ext_from_mime(content_type)}"


# ---------------------------------------------------------------------------
# 단일 이미지 다운로드 (스레드 안전)
# ---------------------------------------------------------------------------


def download_one_image(
    img_url: str,
    utype: str,
    post_url: str,
    folder: Path,
    idx: int,
    seen_urls: set[str],
    og_hashes: set[str],
    image_map: dict[str, str],
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> bool:
    seen_key = _seen_key(utype, img_url)

    # 빠른 비잠금 확인 (최적화; save_image의 내용 동일성 검사로 중복 저장 방지)
    if seen_key in seen_urls:
        return True

    # ── 다운로드 단계 (잠금 외부 – 네트워크 I/O) ──────────────────────────
    payload: tuple[bytes, str, str, str] | None = None

    if utype in ("img", "og_image"):
        payload = _fetch_image(img_url)
        if payload is None:
            payload = _fetch_wayback_image(img_url)
        if payload is None:
            payload = _fetch_wayback_img_from_post(post_url, img_url, post_soup_cache)

    elif utype == "gdrive":
        payload = _fetch_image(img_url, min_bytes=500)
        if payload is None:
            payload = _fetch_wayback_image(img_url, min_bytes=1)
        if payload is None:
            payload = _fetch_wayback_gdrive_from_post(post_url, img_url, post_soup_cache)

    elif utype == "linked_keyword":
        payload = _fetch_image(img_url)
        if payload is None:
            payload = _fetch_wayback_image(img_url)
        if payload is None:
            payload = _fetch_wayback_linked_from_post(post_url, img_url, post_soup_cache)

    elif utype == "linked_direct":
        payload = _fetch_image(img_url, allow_ext_fallback=True)
        if payload is None and _is_community_cdn(img_url):
            payload = _fetch_wayback_image(img_url, allow_ext_fallback=True)

    if payload is None:
        record_failed(post_url, img_url, "download_failed")
        return False

    content, final_url, content_type, content_disposition = payload

    # ── 파일 저장 및 상태 갱신 (잠금 내부) ──────────────────────────────
    filename = _determine_filename(
        utype, img_url, final_url, content_type, content_disposition, idx
    )

    with _dl_lock:
        # 잠금 획득 후 재확인 (다른 스레드가 먼저 처리했을 수 있음)
        if seen_key in seen_urls:
            return True

        if utype == "og_image":
            content_hash = _sha256_bytes(content)
            if content_hash not in og_hashes:
                # 해시가 새로운 경우에만 디스크에 저장
                save_image(content, _safe_filename(filename), folder)
                og_hashes.add(content_hash)
                append_line(THUMB_HASH_FILE, content_hash)
            # 중복 해시인 경우 파일 저장은 건너뛰지만 seen 기록은 항상 남김
            seen_urls.add(seen_key)
        else:
            # save_image는 항상 실제 저장(또는 기존) 파일명을 반환한다.
            saved_name = save_image(content, _safe_filename(filename), folder)
            key = clean_url(img_url)
            if key not in image_map:
                rel = (folder / saved_name).relative_to(ROOT_DIR).as_posix()
                record_image_map(key, rel, image_map, IMAGE_MAP_FILE)
            seen_urls.add(seen_key)

    append_line(DONE_FILE, seen_key)
    return True


# ---------------------------------------------------------------------------
# 포스트 단위 처리
# ---------------------------------------------------------------------------


def process_post(
    post_url: str,
    post_date: str,
    seen_urls: set[str],
    og_hashes: set[str],
    image_map: dict[str, str],
    done_post_urls: set[str],
) -> PostProcessResult:
    # 이미 모든 이미지가 완료된 포스트는 HTTP fetch 없이 즉시 스킵
    if post_url in done_post_urls:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)

    resp = fetch_with_retry(post_url)
    if resp is None:
        record_failed(post_url, "", "fetch_post_failed")
        return PostProcessResult(ok=0, fail=1, post_fetch_ok=False)

    soup = BeautifulSoup(resp.text, "lxml")
    images = collect_image_urls(soup, post_url)

    folder = IMAGES_DIR / date_to_folder(post_date)
    thumbnail_folder = IMAGES_DIR / "thumbnails"

    ok = 0
    fail = 0
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}
    for idx, (img_url, utype) in enumerate(images, start=1):
        target_folder = thumbnail_folder if utype == "og_image" else folder
        if download_one_image(
            img_url,
            utype,
            post_url,
            target_folder,
            idx,
            seen_urls,
            og_hashes,
            image_map,
            post_soup_cache=post_soup_cache,
        ):
            ok += 1
        else:
            fail += 1

    # 포스트 내 모든 이미지가 성공한 경우 완료로 기록 (이미지가 없는 포스트 포함)
    if fail == 0:
        done_post_urls.add(post_url)
        append_line(DONE_POSTS_FILE, post_url)

    return PostProcessResult(ok=ok, fail=fail, post_fetch_ok=True)


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def run_images(posts: list[tuple[str, str]], retry_mode: bool = False):
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    seen_urls = load_seen(DONE_FILE)
    og_hashes = _load_or_build_og_hashes()
    image_map = load_image_map(IMAGE_MAP_FILE)
    done_post_urls = _load_done_post_urls(DONE_POSTS_FILE)

    if retry_mode:
        if not FAILED_FILE.exists():
            print("[이미지] 실패 파일이 없습니다.")
            return
        fail_posts = load_failed_post_urls(FAILED_FILE)
        posts = [(url, date) for url, date in posts if url in fail_posts]
        print(f"[이미지] 재처리 대상: {len(posts)}개 포스트")
        if not posts:
            print("[이미지] 재처리 대상이 없습니다.")
            return

    total = len(posts)
    # 대상 수가 100개 이하면 10개 단위, 초과면 50개 단위로 진행도 출력
    report_interval = 10 if total <= 100 else 50
    start = time.time()
    total_ok = 0
    total_fail = 0
    completed = 0
    counter_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=DEFAULT_MAX_WORKERS) as executor:
        future_to_post = {
            executor.submit(process_post, url, date, seen_urls, og_hashes, image_map, done_post_urls): (url, date)
            for url, date in posts
        }
        for future in as_completed(future_to_post):
            post_url, _ = future_to_post[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"  [오류] {post_url}: {exc}")
                result = PostProcessResult(ok=0, fail=1, post_fetch_ok=False)

            with counter_lock:
                total_ok += result.ok
                total_fail += result.fail
                completed += 1
                cur_completed = completed

            if retry_mode and result.post_fetch_ok:
                remove_from_failed(post_url, reason="fetch_post_failed")
                if result.fail == 0 and result.ok > 0:
                    remove_from_failed(post_url, reason="download_failed")

            if cur_completed % report_interval == 0 or cur_completed == total:
                print(f"  {eta_str(cur_completed, total, start)} 성공={total_ok} 실패={total_fail}")

    print(f"\n[이미지 완료] 성공={total_ok}, 실패={total_fail}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Image downloader")
    parser.add_argument("--retry", action="store_true", help="Retry failed list")
    parser.add_argument("--posts", default=str(ROOT_DIR / "all_posts.txt"), help="Posts list file")
    parser.add_argument("--backfill-map", action="store_true", help="Backfill image_map.tsv")
    args = parser.parse_args()

    if args.backfill_map:
        backfill_image_map()
    else:
        posts = load_posts(args.posts)
        run_images(posts, retry_mode=args.retry)
