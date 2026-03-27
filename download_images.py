"""Image downloader for Lord of Heroes blog posts."""

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import difflib
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import threading
import time
from typing import NamedTuple
import urllib.parse

from bs4 import BeautifulSoup, Tag

from utils import (
    BLOG_HOST,
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    LineBuffer,
    append_line,
    SIZE_W_RE,
    clean_url,
    date_to_folder,
    ensure_utf8_console,
    eta_str,
    extract_category,
    fetch_post_html,
    fetch_with_retry,
    filter_file_lines,
    load_failed_post_urls,
    load_image_map,
    load_posts,
    remove_lines_by_prefix,
)
IMAGES_DIR = ROOT_DIR / "images"
DONE_FILE = ROOT_DIR / "downloaded_urls.txt"
DONE_POSTS_FILE = ROOT_DIR / "done_posts_images.txt"  # 이미지 완료 포스트 URL 목록
FAILED_FILE = ROOT_DIR / "failed_images.txt"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"
THUMB_HASH_FILE = ROOT_DIR / "thumbnail_hashes.txt"  # 썸네일 SHA-256 해시 캐시 (레거시)
IMG_HASH_FILE = ROOT_DIR / "image_hashes.tsv"  # 통합 이미지 해시 캐시 (hash\trel_path)
MULTILANG_LOG_FILE = IMAGES_DIR / "multilang_fallback.tsv"  # 다국어 폴백 성공 로그
MULTILANG_INDEX_CACHE = ROOT_DIR / "multilang_sitemap_index.json"  # EN/JA 사이트맵 인덱스 캐시

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
DOWNLOADABLE_EXTS = IMG_EXTS | ARCHIVE_EXTS
DL_KEYWORDS = {
    "다운로드", "download", "다운", "받기", "저장",
    "고화질 이미지", "고화질", "이미지", "원본",
}
RESOLUTION_RE = re.compile(r"\d+\s*[xX×]\s*\d+")
GDRIVE_HOSTS = {"drive.google.com", "docs.google.com", "lh3.googleusercontent.com"}
COMMUNITY_CDN_HOST = "community-ko-cdn.lordofheroes.com"
WAYBACK_CDX_API = "https://web.archive.org/cdx/search/cdx"

# linked_keyword 수집 시 건너뛸 도메인 (다운로드 대상이 아닌 외부 링크)
_SKIP_LINK_HOSTS = {"forms.gle", "forms.google.com", "play.google.com", "apps.apple.com",
                    "go.onelink.me"}

# --retry 시 비이미지 다운로드 링크 감지용 키워드 (앵커 주변 소제목/강조)
_NON_IMAGE_CONTEXT_KEYWORDS = {"bgm", "ost", "음악", "사운드트랙", "soundtrack"}

# 다국어 블로그 Wayback 폴백용 상수
MULTILANG_BLOG_HOSTS = {
    "en": "blog-en.lordofheroes.com",
    "ja": "blog-ja.lordofheroes.com",
}
MULTILANG_EARLIEST_DATE = {"en": "2020-10-20", "ja": "2021-01-15"}
_KO_SUFFIX_RE = re.compile(r"(?i)_ko(?=[\._\-])")
_LANG_SUFFIX_MAP = {"en": "_EN", "ja": "_JP"}

# Kakao PF 폴백용 상수
KAKAO_PF_PROFILE = "_YXZqxb"
KAKAO_PF_API = f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_PF_PROFILE}/posts"
KAKAO_PF_INDEX_FILE = ROOT_DIR / "kakao_pf_index.json"
KAKAO_PF_LOG_FILE = IMAGES_DIR / "kakao_pf_log.tsv"
FALLBACK_REPORT_FILE = ROOT_DIR / "fallback_report.csv"


class KakaoPFPost(NamedTuple):
    id: int
    title: str
    published_at: int  # Unix ms
    media_urls: list[str]

# ---------------------------------------------------------------------------
# 스레드 안전을 위한 잠금
# ---------------------------------------------------------------------------

# ImageFailedLog 내부 캐시 전용 락 (기존 _dl_lock 역할 유지)
_dl_lock = threading.Lock()

# seen_urls / img_hashes / image_map 딕셔너리 갱신 전용 (in-memory, 극히 빠름)
_state_lock = threading.Lock()

# save_image() 파일명 충돌 해소 전용 (디스크 I/O 직렬화)
_save_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 고빈도 파일용 LineBuffer (append_line 대신 사용해 파일 syscall 대폭 감소)
# ---------------------------------------------------------------------------

# 이미지 URL 완료 이력 (이미지당 1회)
_done_buf = LineBuffer(DONE_FILE)
# image_map.tsv 갱신 (이미지당 1회)
_map_buf = LineBuffer(IMAGE_MAP_FILE)
# 통합 이미지 해시 캐시 (이미지당 1회)
_img_hash_buf = LineBuffer(IMG_HASH_FILE)
# 포스트 완료 이력 (포스트당 1회)
_done_posts_buf = LineBuffer(DONE_POSTS_FILE)
# 다국어 폴백 성공 로그 (이미지당 1회)
_multilang_log_buf = LineBuffer(MULTILANG_LOG_FILE)
# Kakao PF 폴백 성공 로그 (이미지당 1회)
_kakao_pf_log_buf = LineBuffer(KAKAO_PF_LOG_FILE)

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
        key = (post_url, img_url, reason)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            if key in self._cache:
                return
            self._cache.add(key)
        append_line(self._filepath, f"{post_url}\t{img_url}\t{reason}")

    def remove(self, post_url: str, reason: str | None = None,
               img_url: str | None = None) -> None:
        if not self._filepath.exists():
            return
        prefix = post_url + "\t"
        if img_url is not None:
            # 특정 img_url 엔트리만 제거
            def _keep(line: str) -> bool:
                if not line.startswith(prefix):
                    return True
                parts = line.split("\t")
                return (parts[1].strip() if len(parts) >= 2 else "") != img_url
            filter_file_lines(self._filepath, _keep)
            with self._lock:
                if self._cache is None:
                    self._cache = self._load()
                self._cache = {e for e in self._cache if not (e[0] == post_url and e[1] == img_url)}
        elif reason is None:
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

    def remove_batch(self, post_url: str, img_urls: set[str]) -> None:
        """post_url에 속하는 여러 img_url 엔트리를 한 번의 파일 I/O로 제거한다."""
        if not img_urls or not self._filepath.exists():
            return
        prefix = post_url + "\t"

        def _keep(line: str) -> bool:
            if not line.startswith(prefix):
                return True
            parts = line.split("\t")
            return (parts[1].strip() if len(parts) >= 2 else "") not in img_urls
        filter_file_lines(self._filepath, _keep)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            self._cache = {e for e in self._cache
                           if not (e[0] == post_url and e[1] in img_urls)}

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
    ok_saved: int = 0
    ok_original: int = 0
    ok_multilang: int = 0
    ok_kakao: int = 0
    ok_dedup: int = 0
    succeeded_urls: list[str] = field(default_factory=list)


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
    """레거시: 썸네일 해시 set 로드 (마이그레이션용)."""
    if THUMB_HASH_FILE.exists():
        hashes = set(THUMB_HASH_FILE.read_text(encoding="utf-8").splitlines())
        hashes.discard("")
        return hashes
    return set()


def _load_or_build_img_hashes() -> tuple[dict[str, str], set[str]]:
    """통합 이미지 해시 캐시를 로드한다.

    Returns:
        (img_hashes, thumb_hashes):
            img_hashes  – dict[sha256_hex, rel_path]  모든 이미지의 해시→경로
            thumb_hashes – set[sha256_hex]  썸네일(og_image)인 해시 집합
    """
    img_hashes: dict[str, str] = {}
    thumb_hashes: set[str] = set()

    if IMG_HASH_FILE.exists():
        for line in IMG_HASH_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                h = parts[0].strip()
                rel = parts[1].strip()
                is_thumb = parts[2].strip() == "T" if len(parts) >= 3 else False
                if h and rel:
                    img_hashes[h] = rel
                    if is_thumb:
                        thumb_hashes.add(h)
        return img_hashes, thumb_hashes

    # 캐시 미존재 → 기존 이미지 폴더를 스캔해 빌드
    old_thumb_dir = (IMAGES_DIR / "thumbnails").resolve()
    # 레거시 썸네일 해시 로드 (마이그레이션)
    legacy_thumb_hashes = _load_or_build_og_hashes()

    if IMAGES_DIR.exists():
        for file_path in IMAGES_DIR.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in DOWNLOADABLE_EXTS:
                continue
            try:
                h = _sha256_bytes(file_path.read_bytes())
                rel = file_path.relative_to(ROOT_DIR).as_posix()
                if h not in img_hashes:
                    img_hashes[h] = rel
                # 레거시 썸네일 폴더이거나 레거시 해시에 존재하면 썸네일
                is_in_thumb_dir = old_thumb_dir in file_path.resolve().parents
                if is_in_thumb_dir or h in legacy_thumb_hashes:
                    thumb_hashes.add(h)
            except OSError:
                continue

    # 캐시 파일 작성
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{h}\t{rel}\t{'T' if h in thumb_hashes else ''}"
        for h, rel in img_hashes.items()
    ]
    IMG_HASH_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return img_hashes, thumb_hashes


# ---------------------------------------------------------------------------
# seen 키 헬퍼
# ---------------------------------------------------------------------------


def _clean_img_url(url: str) -> str:
    """이미지 URL 정규화: ref 파라미터 제거 + clean_url."""
    return clean_url(_strip_ref_param(url))


def _seen_scope(utype: str) -> str:
    return "thumb" if utype == "og_image" else "main"


def _seen_key(utype: str, url: str) -> str:
    return f"{_seen_scope(utype)}:{_clean_img_url(url)}"


def _is_community_cdn(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() == COMMUNITY_CDN_HOST


def _normalized_link_key(url: str) -> str:
    """Wayback 재작성을 고려한 URL 정규화 (링크 매칭용)."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = urllib.parse.unquote(parsed.path or "")
    path = SIZE_W_RE.sub("", path)
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
    name = Path(path).name or ""
    return urllib.parse.unquote(name) if name else ""


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name[:200] or "image"


def save_image(content: bytes, filename: str, folder: Path) -> str:
    """bytes를 folder/filename에 저장하고 충돌을 해소한다.

    - 동일 내용의 파일이 이미 존재하면 해당 파일명을 반환한다.
    - 새로 저장하면 실제 저장된 파일명을 반환한다.
    - None을 반환하지 않으므로 호출 측에서 항상 유효한 경로를 얻는다.

    충돌 검사는 크기 비교(cheap) → 바이트 비교(expensive) 순서로 수행해
    불필요한 전체 파일 읽기를 최소화한다.
    """
    folder.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(filename)
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".bin"
    content_len = len(content)

    target = folder / filename
    idx = 2
    while target.exists():
        try:
            # 크기가 다르면 즉시 다음 후보로 이동 (전체 읽기 불필요)
            if target.stat().st_size == content_len and target.read_bytes() == content:
                return target.name
        except OSError:
            pass
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
    """image_map 딕셔너리와 파일에 항목을 추가한다 (중복 방지).

    backfill_image_map 전용. download_one_image 는 _state_lock + _map_buf 를 직접 사용.
    """
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


def _load_done_post_urls(filepath: Path) -> dict[str, int]:
    """이미지 수집이 완료된 포스트 URL → 이미지 수 딕셔너리를 반환한다."""
    if not filepath.exists():
        return {}
    result: dict[str, int] = {}
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        url = parts[0].strip()
        count = int(parts[1]) if len(parts) >= 2 and parts[1].strip().isdigit() else 0
        result[url] = count
    return result


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


def record_failed(post_url: str, img_url: str, reason: str) -> None:
    """_failed_log.record 의 모듈 수준 래퍼."""
    _failed_log.record(post_url, img_url, reason)


def remove_from_failed(post_url: str, reason: str | None = None,
                       img_url: str | None = None) -> None:
    """_failed_log.remove 의 모듈 수준 래퍼."""
    _failed_log.remove(post_url, reason, img_url=img_url)


def remove_from_failed_batch(post_url: str, img_urls: set[str]) -> None:
    """_failed_log.remove_batch 의 모듈 수준 래퍼."""
    _failed_log.remove_batch(post_url, img_urls)


# ---------------------------------------------------------------------------
# backfill (--backfill-map 옵션)
# ---------------------------------------------------------------------------


def backfill_image_map() -> None:
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    image_map = load_image_map(IMAGE_MAP_FILE)

    files = [
        f
        for f in IMAGES_DIR.rglob("*")
        if f.is_file()
        and f.suffix.lower() in DOWNLOADABLE_EXTS
        and "thumbnails" not in f.relative_to(IMAGES_DIR).parts
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
            key = _clean_img_url(url)
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


def _wayback_oldest(url: str) -> str | None:
    """Wayback CDX API에서 가장 오래된 200 스냅샷 URL을 반환한다.

    동일 URL에 대해 여러 스레드가 동시에 CDX 요청을 보내는 것을 방지하기 위해
    이벤트 기반 대기를 사용한다. 먼저 도달한 스레드가 fetch를 수행하고,
    나중에 도달한 스레드는 완료 이벤트를 기다린 뒤 캐시에서 결과를 읽는다.
    """
    url = _strip_ref_param(url)
    with _wayback_cache_lock:
        if url in _wayback_cache:
            return _wayback_cache[url]
        if url in _wayback_events:
            event = _wayback_events[url]
            do_fetch = False
        else:
            event = threading.Event()
            _wayback_events[url] = event
            do_fetch = True

    if not do_fetch:
        event.wait()
        with _wayback_cache_lock:
            return _wayback_cache.get(url)

    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode",
        "limit": "5",
    }
    result: str | None = None
    try:
        resp = fetch_with_retry(WAYBACK_CDX_API, params=params, timeout=15)
        if resp is not None:
            try:
                rows = resp.json()
                if isinstance(rows, list) and len(rows) >= 2:
                    for row in rows[1:]:
                        if not isinstance(row, list) or len(row) < 3:
                            continue
                        status = str(row[2]).strip()
                        if not status.startswith(("2", "3")):
                            continue
                        timestamp = str(row[0]).strip()
                        original = str(row[1]).strip()
                        if timestamp and original:
                            result = f"https://web.archive.org/web/{timestamp}/{original}"
                            break
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
        name = match.group(1).strip().strip('"')
        # requests는 HTTP 헤더를 latin-1로 디코딩하므로, raw UTF-8이 포함된 경우 복원
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return name
    return ""


def _is_image_ct(content_type: str) -> bool:
    return content_type.lower().startswith("image/")


_ARCHIVE_CONTENT_TYPES = {
    "application/zip", "application/x-zip-compressed",
    "application/x-rar-compressed", "application/x-7z-compressed",
    "application/gzip", "application/x-tar",
    "application/octet-stream",
}


def _is_archive_ct(content_type: str) -> bool:
    return content_type.lower().split(";")[0].strip() in _ARCHIVE_CONTENT_TYPES


def _response_to_image(
    resp,
    *,
    allow_ext_fallback: bool = False,
    allow_archive: bool = False,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    if not resp or not getattr(resp, "content", None):
        return None
    content = resp.content
    if len(content) < min_bytes:
        return None
    content_type = resp.headers.get("Content-Type", "")
    is_image_type = _is_image_ct(content_type)
    url_ext = Path(urllib.parse.urlparse(resp.url).path).suffix.lower()
    has_image_ext = url_ext in IMG_EXTS
    is_acceptable = (is_image_type
                     or (allow_ext_fallback and has_image_ext)
                     or (allow_archive and (_is_archive_ct(content_type) or url_ext in ARCHIVE_EXTS)))
    if not is_acceptable:
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
    allow_archive: bool = False,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    resp = fetch_with_retry(url, allow_redirects=True)
    return _response_to_image(resp, allow_ext_fallback=allow_ext_fallback,
                              allow_archive=allow_archive, min_bytes=min_bytes)


def _fetch_wayback_image(
    url: str,
    *,
    allow_ext_fallback: bool = False,
    allow_archive: bool = False,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    wayback_url = _wayback_oldest(url)
    if not wayback_url:
        return None

    fetch_kwargs = dict(allow_ext_fallback=allow_ext_fallback,
                        allow_archive=allow_archive, min_bytes=min_bytes)

    # Wayback redirect 대응: redirect를 따라가 원본 대상 URL을 추출,
    # 원본을 직접 fetch 시도 (Wayback im_ 경유보다 빠름).
    resp = fetch_with_retry(wayback_url, allow_redirects=True)
    if resp is not None:
        # Wayback 응답 자체가 유효한 이미지/아카이브이면 그대로 반환
        direct = _response_to_image(resp, **fetch_kwargs)
        if direct is not None:
            return direct
        # redirect 발생 시 원본 대상 URL 추출 → 직접 fetch → 실패 시 대상의 Wayback 시도
        original_target = _original_url_from_wayback(resp.url)
        if original_target and _normalized_link_key(original_target) != _normalized_link_key(url):
            result = _fetch_image(original_target, **fetch_kwargs)
            if result is not None:
                return result
            # redirect 대상 URL의 Wayback 스냅샷 시도 (원본이 현재 죽었을 수 있음)
            target_wayback = _wayback_oldest(original_target)
            if target_wayback:
                result = _fetch_image(_add_im(target_wayback), **fetch_kwargs)
                if result is not None:
                    return result

    # 폴백: Wayback im_ 경유
    return _fetch_image(_add_im(wayback_url), **fetch_kwargs)


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
        image = _fetch_image(_add_im(candidate_url))
        if image:
            return image
    return None


def _get_content_tag(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """Ghost CMS 본문 컨테이너를 반환한다 (좁은 범위 우선)."""
    return (
        soup.select_one(".gh-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.find("main")
        or soup
    )


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
    *,
    allow_archive: bool = False,
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
        image = _fetch_image(_add_im(absolute_href), allow_archive=allow_archive)
        if image:
            return image

    return None


# ---------------------------------------------------------------------------
# 다국어 Wayback 폴백 헬퍼
# ---------------------------------------------------------------------------


def _multilang_image_url_candidates(img_url: str) -> list[tuple[str, str]]:
    """이미지 URL의 호스트를 EN/JA로 교체하고 _KO 접미사를 치환한 후보 목록을 반환한다."""
    parsed = urllib.parse.urlparse(img_url)
    hostname = (parsed.hostname or "").lower()

    candidates: list[tuple[str, str]] = []
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        if hostname == BLOG_HOST:
            new_host = lang_host
        elif hostname == COMMUNITY_CDN_HOST:
            # EN/JA는 community CDN 대신 블로그 호스트를 사용
            new_host = lang_host
        else:
            continue

        new_url = parsed._replace(netloc=new_host).geturl()
        suffix = _LANG_SUFFIX_MAP[lang]
        if _KO_SUFFIX_RE.search(new_url):
            replaced = _KO_SUFFIX_RE.sub(suffix, new_url)
            candidates.append((replaced, lang))
        else:
            candidates.append((new_url, lang))

    return candidates


def _load_multilang_cache() -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """캐시 파일에서 date_index와 meta를 로드. 없으면 ({}, {}) 반환."""
    import json

    if not MULTILANG_INDEX_CACHE.exists():
        return {}, {}
    try:
        with open(MULTILANG_INDEX_CACHE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        meta = raw.pop("_meta", {})
        index = {d: [(u, l) for u, l in entries] for d, entries in raw.items()}
        return index, meta
    except Exception:
        return {}, {}


def _save_multilang_cache(
    date_index: dict[str, list[tuple[str, str]]], meta: dict[str, str]
) -> None:
    import json

    raw: dict = {d: entries for d, entries in date_index.items()}
    raw["_meta"] = meta
    with open(MULTILANG_INDEX_CACHE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)


def _build_multilang_date_index() -> dict[str, list[tuple[str, str]]]:
    """EN/JA 사이트맵을 직접 가져와 {date: [(post_url, lang), ...]} 인덱스를 구축한다.

    캐시 파일이 있으면 각 언어 사이트맵의 최신 날짜를 비교해
    변경이 없는 언어는 건너뛴다.
    """
    from build_posts_list import fetch_newest_single_sitemap_date, parse_sitemap

    # 1) 캐시 로드
    cached_index, cached_meta = _load_multilang_cache()

    # 2) 각 언어별 최신 날짜 비교
    need_refresh: dict[str, bool] = {}
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        sitemap_url = f"https://{lang_host}/sitemap-posts.xml"
        remote_date = fetch_newest_single_sitemap_date(sitemap_url)
        cached_date = cached_meta.get(f"{lang}_latest", "")

        if remote_date and cached_date == remote_date:
            print(f"  [{lang.upper()}] 최신 상태 ({cached_date}), 갱신 불필요")
        else:
            need_refresh[lang] = True
            if remote_date:
                print(f"  [{lang.upper()}] 갱신 필요 (캐시: {cached_date or '없음'} → 원격: {remote_date})")
            else:
                print(f"  [{lang.upper()}] 원격 날짜 확인 실패, 전체 fetch 시도")

    # 3) 모두 최신이면 캐시 반환
    if not need_refresh and cached_index:
        return cached_index

    # 4) 변경된 언어만 fetch, 나머지는 캐시 유지
    date_index: dict[str, list[tuple[str, str]]] = (
        {k: list(v) for k, v in cached_index.items()} if cached_index else {}
    )
    new_meta = dict(cached_meta)

    # 갱신 대상 언어의 기존 항목 제거
    for lang in need_refresh:
        date_index = {
            d: [(u, l) for u, l in entries if l != lang]
            for d, entries in date_index.items()
        }
        date_index = {d: entries for d, entries in date_index.items() if entries}

    # 새로 fetch
    for lang in need_refresh:
        lang_host = MULTILANG_BLOG_HOSTS[lang]
        sitemap_url = f"https://{lang_host}/sitemap-posts.xml"

        resp = fetch_with_retry(sitemap_url, allow_redirects=True, timeout=30)
        if not resp:
            print(f"  [{lang.upper()}] 사이트맵 fetch 실패, 건너뜀")
            continue

        resp.encoding = resp.apparent_encoding or "utf-8"
        try:
            entries = parse_sitemap(resp.text)
        except Exception as exc:
            print(f"  [{lang.upper()}] 사이트맵 파싱 실패: {exc}")
            continue

        count = 0
        latest = ""
        for post_url, date in entries:
            if date:
                date_index.setdefault(date, []).append((post_url, lang))
                count += 1
                if date > latest:
                    latest = date
        new_meta[f"{lang}_latest"] = latest
        print(f"  [{lang.upper()}] {count}개 포스트 인덱싱 완료")

    # 5) 캐시 저장
    _save_multilang_cache(date_index, new_meta)
    return date_index


def _multilang_post_url_candidates(
    ko_post_url: str,
    post_date: str,
    date_index: dict[str, list[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """KO 포스트 URL에 대응하는 EN/JA 포스트 URL 후보를 반환한다."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    # 1순위: slug 기반 (blog-ko → blog-en/blog-ja 단순 치환)
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        if post_date and post_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
            continue
        slug_url = ko_post_url.replace(BLOG_HOST, lang_host)
        if slug_url != ko_post_url and slug_url not in seen:
            seen.add(slug_url)
            candidates.append((slug_url, lang))

    # 2순위: date 기반 (같은 날짜의 포스트 검색)
    if post_date and post_date in date_index:
        for alt_url, lang in date_index[post_date]:
            if post_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
                continue
            if alt_url not in seen:
                seen.add(alt_url)
                candidates.append((alt_url, lang))

    return candidates


def _fetch_wayback_img_by_position(
    alt_post_url: str,
    idx: int,
    utype: str,
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> tuple[bytes, str, str, str] | None:
    """Wayback 포스트 스냅샷에서 idx(1-based) 위치의 이미지를 다운로드한다.

    collect_image_urls()는 BLOG_HOST(blog-ko) 전용이라 EN/JA 호스트 이미지를 인식
    못하므로, 여기서는 본문 내 모든 <img> 태그를 직접 순회한다.
    """
    soup_with_base = _fetch_wayback_post_soup(alt_post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base

    # og:image 처리
    if utype == "og_image":
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            target_url = urllib.parse.urljoin(wayback_post, og["content"])
            payload = _fetch_image(_add_im(target_url))
            if payload:
                return payload
        return None

    # 본문 이미지 수집 (호스트 무관하게 모든 <img> 태그)
    content_tag = _get_content_tag(soup)
    img_urls: list[str] = []
    for img in content_tag.find_all("img"):
        if "author-profile-image" in (img.get("class") or []):
            continue
        if img.find_parent("div", class_="author-card"):
            continue
        src = img.get("src") or img.get("data-src") or ""
        if src:
            img_urls.append(urllib.parse.urljoin(wayback_post, src))

    # idx는 1-based — KO 포스트에서의 순서와 대응
    target_idx = idx - 1
    if target_idx < 0 or target_idx >= len(img_urls):
        return None

    target_url = img_urls[target_idx]
    payload = _fetch_image(_add_im(target_url))
    if payload:
        return payload
    # Wayback URL에서 원본 추출 후 직접 fetch
    original = _original_url_from_wayback(target_url)
    return _fetch_image(original) if original != target_url else None


def _fetch_multilang_wayback_image(
    post_url: str,
    img_url: str,
    post_date: str,
    utype: str,
    idx: int,
    date_index: dict[str, list[tuple[str, str]]],
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
) -> tuple[bytes, str, str, str, str] | None:
    """다국어 블로그 Wayback 스냅샷에서 이미지를 탐색하는 통합 폴백 함수.

    Returns:
        성공 시 (content, final_url, content_type, cd, fallback_post_url) 5-tuple.
        fallback_post_url은 폴백에 사용된 포스트 URL (직접 CDX 성공 시 빈 문자열).
    """
    # 조기 종료: 모든 언어의 earliest date보다 이전이면 시도 불필요
    if post_date and all(
        post_date < earliest for earliest in MULTILANG_EARLIEST_DATE.values()
    ):
        return None

    # ── Phase A: URL/파일명 기반 매칭 ──────────────────────────────────
    img_candidates = _multilang_image_url_candidates(img_url)
    for candidate_img_url, lang in img_candidates:
        if post_date and post_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
            continue
        payload = _fetch_wayback_image(candidate_img_url)
        if payload:
            return (*payload, "")

    # Phase A-2: 포스트 HTML에서 URL 매칭
    post_candidates = _multilang_post_url_candidates(post_url, post_date, date_index)
    for alt_post_url, lang in post_candidates:
        # 언어별 변환된 img_url로 기존 함수 재사용
        lang_img_candidates = [u for u, l in img_candidates if l == lang]
        for candidate_img_url in lang_img_candidates:
            if utype in ("img", "og_image"):
                payload = _fetch_wayback_img_from_post(
                    alt_post_url, candidate_img_url, post_soup_cache
                )
            elif utype == "gdrive":
                payload = _fetch_wayback_gdrive_from_post(
                    alt_post_url, candidate_img_url, post_soup_cache
                )
            elif utype == "linked_keyword":
                payload = _fetch_wayback_linked_from_post(
                    alt_post_url, candidate_img_url, post_soup_cache
                )
            else:
                payload = None
            if payload:
                return (*payload, alt_post_url)

    # ── Phase B: Position 기반 매칭 ────────────────────────────────────
    for alt_post_url, lang in post_candidates:
        payload = _fetch_wayback_img_by_position(
            alt_post_url, idx, utype, post_soup_cache
        )
        if payload:
            return (*payload, alt_post_url)

    return None


# ---------------------------------------------------------------------------
# Kakao PF 폴백 헬퍼
# ---------------------------------------------------------------------------


def _build_kakao_pf_index() -> dict[str, list[KakaoPFPost]]:
    """Kakao PF 게시글을 페이지네이션하며 {date: [KakaoPFPost, ...]} 인덱스를 구축한다.

    JSON 캐시 파일이 있으면 로드 후 새 포스트만 추가 fetch 한다.
    """
    from datetime import datetime, timezone, timedelta

    KST = timezone(timedelta(hours=9))

    def _ms_to_date(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=KST).strftime("%Y-%m-%d")

    def _extract_media_urls(media_list: list[dict]) -> list[str]:
        urls: list[str] = []
        for m in media_list:
            if m.get("type") == "image" and m.get("url"):
                urls.append(m["url"])
            elif m.get("type") == "link":
                for img in m.get("images") or []:
                    if img.get("url"):
                        urls.append(img["url"])
                        break  # link 당 첫 번째 이미지만
        return urls

    # ── 캐시 로드 ─────────────────────────────────────────────────────────
    cached_posts: list[dict] = []
    last_sort: str | None = None

    if KAKAO_PF_INDEX_FILE.exists():
        try:
            data = json.loads(KAKAO_PF_INDEX_FILE.read_text(encoding="utf-8"))
            cached_posts = data.get("posts", [])
            last_sort = data.get("last_sort")
            print(f"  Kakao PF 캐시 로드: {len(cached_posts)}개 포스트")
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  Kakao PF 캐시 파싱 실패 ({exc}), 전체 재수집")
            cached_posts = []
            last_sort = None

    # ── 새 포스트 fetch ───────────────────────────────────────────────────
    new_posts: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        params: dict[str, str] = {"includePinnedPost": "true"}
        if cursor:
            params["since"] = cursor
        try:
            resp = fetch_with_retry(KAKAO_PF_API, params=params, timeout=15)
        except Exception:
            resp = None
        if not resp:
            print(f"  Kakao PF API 요청 실패 (page {page}), 중단")
            break

        try:
            body = resp.json()
        except Exception:
            print(f"  Kakao PF JSON 파싱 실패 (page {page}), 중단")
            break

        items = body.get("items", [])
        if not items:
            break

        for item in items:
            sort_val = str(item.get("sort", ""))
            # 캐시에 이미 있는 포스트에 도달하면 중단
            if last_sort and sort_val <= last_sort:
                items = []  # 아래 break 조건 트리거
                break
            new_posts.append({
                "id": item["id"],
                "title": item.get("title", ""),
                "published_at": item.get("published_at", 0),
                "media_urls": _extract_media_urls(item.get("media") or []),
                "sort": sort_val,
            })

        if not items or not body.get("has_next"):
            break

        cursor = str(items[-1].get("sort", "")) if items else None
        if not cursor:
            break
        page += 1
        time.sleep(0.2)  # rate limiting

    if new_posts:
        print(f"  Kakao PF 새 포스트: {len(new_posts)}개 수집")

    # ── 캐시 병합 및 저장 ─────────────────────────────────────────────────
    all_raw = new_posts + cached_posts
    # sort 기준 내림차순 정렬 (최신 먼저)
    all_raw.sort(key=lambda p: p.get("sort", ""), reverse=True)
    # 중복 제거 (id 기준)
    seen_ids: set[int] = set()
    deduped: list[dict] = []
    for p in all_raw:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            deduped.append(p)

    # 캐시 파일 저장
    new_last_sort = deduped[0]["sort"] if deduped else last_sort
    try:
        KAKAO_PF_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        KAKAO_PF_INDEX_FILE.write_text(
            json.dumps(
                {"last_sort": new_last_sort, "posts": deduped},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"  Kakao PF 캐시 저장 실패: {exc}")

    # ── date → [KakaoPFPost] 인덱스 구축 ─────────────────────────────────
    date_index: dict[str, list[KakaoPFPost]] = {}
    for p in deduped:
        pub = p.get("published_at", 0)
        if not pub:
            continue
        date_str = _ms_to_date(pub)
        entry = KakaoPFPost(
            id=p["id"],
            title=p.get("title", ""),
            published_at=pub,
            media_urls=p.get("media_urls", []),
        )
        date_index.setdefault(date_str, []).append(entry)

    return date_index


def _match_kakao_pf_post(
    candidates: list[KakaoPFPost],
    blog_title: str,
) -> KakaoPFPost | None:
    """같은 날짜의 Kakao PF 후보 중 블로그 제목과 가장 유사한 포스트를 선택한다."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    best: KakaoPFPost | None = None
    best_ratio = 0.0
    for kp in candidates:
        ratio = difflib.SequenceMatcher(None, blog_title, kp.title).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = kp

    # 유사도가 너무 낮으면 skip (0.3 미만)
    if best_ratio < 0.3:
        return None
    return best


def _fetch_kakao_pf_image(
    post_url: str,
    img_url: str,
    post_date: str,
    utype: str,
    idx: int,
    kakao_pf_index: dict[str, list[KakaoPFPost]],
    blog_title: str = "",
) -> tuple[bytes, str, str, str, str] | None:
    """Kakao PF 포스트에서 이미지를 탐색하는 폴백 함수.

    Returns:
        성공 시 (content, final_url, content_type, cd, kakao_permalink) 5-tuple.
    """
    candidates = kakao_pf_index.get(post_date)
    if not candidates:
        return None

    kp = _match_kakao_pf_post(candidates, blog_title)
    if not kp:
        return None

    # 이미지 매칭: og_image → 첫 번째, img → position 기반
    if not kp.media_urls:
        return None

    if utype == "og_image":
        target_url = kp.media_urls[0]
    elif utype == "img":
        # idx는 1-based, og_image가 있으면 idx-1이 media_urls 인덱스
        # 하지만 Kakao PF에서는 og_image가 별도로 없으므로 idx-1 사용
        media_idx = idx - 1
        if media_idx < 0 or media_idx >= len(kp.media_urls):
            return None
        target_url = kp.media_urls[media_idx]
    else:
        # gdrive, linked_keyword 등은 position 기반으로 시도
        media_idx = idx - 1
        if media_idx < 0 or media_idx >= len(kp.media_urls):
            return None
        target_url = kp.media_urls[media_idx]

    payload = _fetch_image(target_url)
    if payload is None:
        return None

    permalink = f"http://pf.kakao.com/{KAKAO_PF_PROFILE}/{kp.id}"
    return (*payload, permalink)


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
        url = _clean_img_url(url)
        scope = "thumb" if utype == "og_image" else "main"
        key = (scope, url)
        if key in seen_keys:
            return
        seen_keys.add(key)
        results.append((url, utype))

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        _add(og["content"], "og_image")

    content_tag = _get_content_tag(soup)

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
            path_lower = parsed.path.lower()
            if "/spreadsheets/" in path_lower or "/forms/" in path_lower:
                continue
            _add(abs_href, "gdrive")
            continue
        if parsed.hostname in _SKIP_LINK_HOSTS:
            continue
        path_ext = Path(parsed.path).suffix.lower()
        if path_ext in DOWNLOADABLE_EXTS:
            _add(abs_href, "linked_direct")
            continue
        anchor_text = anchor.get_text(strip=True)
        if any(keyword in anchor_text.lower() for keyword in DL_KEYWORDS) or RESOLUTION_RE.search(
            anchor_text
        ):
            _add(abs_href, "linked_keyword")

    return results


def _detect_non_image_urls(soup: BeautifulSoup, post_url: str) -> set[str]:
    """주변 컨텍스트에서 BGM 등 비이미지 키워드가 감지된 다운로드 URL을 반환한다."""
    skip_urls: set[str] = set()
    content_tag = _get_content_tag(soup)
    for anchor in content_tag.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:")):
            continue
        abs_href = urllib.parse.urljoin(post_url, href)
        parsed = urllib.parse.urlparse(abs_href)
        if parsed.hostname not in GDRIVE_HOSTS:
            continue
        # 앵커 텍스트에서 비이미지 키워드 검색
        anchor_text = anchor.get_text(strip=True).lower()
        if any(kw in anchor_text for kw in _NON_IMAGE_CONTEXT_KEYWORDS):
            skip_urls.add(_clean_img_url(abs_href))
            continue
        # 가장 가까운 앞쪽 heading(h1-h6)에서 비이미지 키워드 검색
        for prev in anchor.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6"]):
            text = prev.get_text(strip=True).lower()
            if any(kw in text for kw in _NON_IMAGE_CONTEXT_KEYWORDS):
                skip_urls.add(_clean_img_url(abs_href))
            break  # 가장 가까운 heading 하나만
    return skip_urls


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
        if final_base and Path(final_base).suffix.lower() in DOWNLOADABLE_EXTS:
            return final_base
        original_base = _basename(original_href)
        if original_base and Path(original_base).suffix.lower() in DOWNLOADABLE_EXTS:
            return original_base
        return f"image_{idx}{_ext_from_mime(content_type)}"

    # gdrive: Content-Disposition → query param(name/title) → MD5 해시 순으로 폴백
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
# 기존 폴백 이미지 일괄 rename (출처 접두사 부여)
# ---------------------------------------------------------------------------


def _rename_fallback_images() -> int:
    """기존 폴백 로그에 기록된 이미지 파일에 출처 접두사가 없으면 rename한다.

    Returns:
        rename된 파일 수.
    """
    renamed = 0
    for log_file, default_tag in [
        (KAKAO_PF_LOG_FILE, "[Kakao]"),
        (MULTILANG_LOG_FILE, None),
    ]:
        if not log_file.exists():
            continue
        lines = log_file.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                new_lines.append(line)
                continue
            rel_path, post_url, source = parts[0], parts[1], parts[2]

            basename = Path(rel_path).name
            if basename.startswith("["):
                new_lines.append(line)
                continue

            tag = default_tag if default_tag else _source_tag(source)
            if not tag:
                new_lines.append(line)
                continue

            old_path = ROOT_DIR / rel_path
            new_name = f"{tag} {basename}"
            new_path = old_path.parent / _safe_filename(new_name)
            if old_path.exists() and not new_path.exists():
                old_path.rename(new_path)
                new_rel = new_path.relative_to(ROOT_DIR).as_posix()
                new_lines.append(f"{new_rel}\t{post_url}\t{source}")
                renamed += 1
            else:
                new_lines.append(line)

        log_file.write_text("\n".join(new_lines) + ("\n" if new_lines else ""),
                            encoding="utf-8")
    return renamed



# ---------------------------------------------------------------------------
# Alt 이미지 보충 (--retry-multilang / --retry-kakaopf)
# ---------------------------------------------------------------------------


def _supplement_alt_images(
    mode: str,
    posts: list[tuple[str, str]],
    html_index: "dict[str, Path] | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    """한쪽 폴백만 성공한 이미지에 반대쪽 alt를 보충한다.

    Args:
        mode: "multilang" (KakaoPF 성공 → multilang alt 보충)
              또는 "kakaopf" (multilang 성공 → KakaoPF alt 보충).
    """

    if mode == "multilang":
        src_log = KAKAO_PF_LOG_FILE
        dst_log_buf = _multilang_log_buf
        label = "multilang"
    elif mode == "kakaopf":
        src_log = MULTILANG_LOG_FILE
        dst_log_buf = _kakao_pf_log_buf
        label = "KakaoPF"
    else:
        print(f"[보충] 알 수 없는 모드: {mode}")
        return

    if not src_log.exists():
        print(f"[보충] 소스 로그 파일 없음: {src_log}")
        return

    # 소스 로그에서 post_url → [rel_path, ...] 매핑
    log_entries: dict[str, list[str]] = {}
    for line in src_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel_path, post_url, _source = parts[0], parts[1], parts[2]
        log_entries.setdefault(post_url, []).append(rel_path)

    if not log_entries:
        print(f"[보충] 소스 로그에 항목 없음")
        return

    # posts에서 date 매핑
    post_date_map = {url: date for url, date in posts}
    target_posts = [(url, post_date_map.get(url, "")) for url in log_entries
                    if url in post_date_map]

    if not target_posts:
        print(f"[보충] 대상 포스트 없음")
        return

    print(f"[보충] {label} alt 보충 대상: {len(target_posts)}개 포스트")

    # 보충 인덱스 구축
    if mode == "multilang":
        print(f"[보충] 다국어 사이트맵 인덱스 구축 중...")
        multilang_date_index = _build_multilang_date_index()
        kakao_pf_index: dict[str, list[KakaoPFPost]] = {}
    else:
        print(f"[보충] Kakao PF 인덱스 구축 중...")
        multilang_date_index: dict[str, list[tuple[str, str]]] = {}
        kakao_pf_index = _build_kakao_pf_index()
        if kakao_pf_index:
            total_kp = sum(len(v) for v in kakao_pf_index.values())
            print(f"  Kakao PF 인덱스: {len(kakao_pf_index)}일, 총 {total_kp}개 포스트")

    start = time.time()
    total_supplemented = 0
    completed = 0

    def _process_one_post(post_url: str, post_date: str) -> int:
        """단일 포스트의 alt 보충. 보충 성공 수를 반환한다."""
        html_text = fetch_post_html(post_url, html_index)
        if html_text is None:
            return 0

        soup = BeautifulSoup(html_text, "lxml")
        images = collect_image_urls(soup, post_url)

        blog_title = ""
        if mode == "kakaopf":
            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                blog_title = title_tag.string.strip()
            else:
                h1 = soup.find("h1")
                if h1:
                    blog_title = h1.get_text(strip=True)

        category = extract_category(soup)
        date_folder = date_to_folder(post_date)
        if category:
            folder = IMAGES_DIR / category / date_folder
        else:
            folder = IMAGES_DIR / date_folder

        logged_rels = set(log_entries.get(post_url, []))
        post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}
        count = 0

        for idx, (img_url, utype) in enumerate(images, start=1):
            if utype == "linked_direct":
                continue

            # 이 이미지에 해당하는 로그 항목이 있는지 확인
            # 로그의 rel_path와 실제 저장 경로를 비교하기 어려우므로
            # 포스트 내 모든 이미지에 대해 보충 시도
            if mode == "multilang":
                result = _fetch_multilang_wayback_image(
                    post_url, img_url, post_date, utype, idx,
                    multilang_date_index, post_soup_cache,
                )
            else:
                result = _fetch_kakao_pf_image(
                    post_url, img_url, post_date, utype, idx,
                    kakao_pf_index, blog_title=blog_title,
                )

            if result is None:
                continue

            alt_content = result[0]
            alt_source = result[4]
            tag = _source_tag(alt_source)
            alt_filename = _determine_filename(
                utype, img_url, result[1], result[2], result[3], idx
            )
            alt_rel = _save_alternative_image(alt_content, alt_filename, folder,
                                              source_tag=tag)
            if alt_rel:
                dst_log_buf.add(f"{alt_rel}\t{post_url}\t{alt_source}")
                count += 1

        return count

    report_interval = 10 if len(target_posts) <= 100 else 50
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(_process_one_post, url, date): url
            for url, date in target_posts
        }
        for future in as_completed(future_to_post):
            try:
                count = future.result()
            except Exception as exc:
                post_url = future_to_post[future]
                print(f"  [오류] {post_url}: {exc}")
                count = 0
            total_supplemented += count
            completed += 1
            if completed % report_interval == 0 or completed == len(target_posts):
                elapsed = time.time() - start
                print(f"  {completed}/{len(target_posts)} "
                      f"({elapsed:.0f}s) 보충={total_supplemented}")

    dst_log_buf.flush_all()
    print(f"\n[보충 완료] {label} alt {total_supplemented}개 이미지 보충")


# ---------------------------------------------------------------------------
# 단일 이미지 다운로드 (스레드 안전)
# ---------------------------------------------------------------------------


def _source_tag(source_url: str) -> str:
    """폴백 소스 URL로부터 출처 접두사를 결정한다."""
    if source_url.startswith("http://pf.kakao.com/"):
        return "[Kakao]"
    if "blog-en" in source_url:
        return "[EN]"
    if "blog-ja" in source_url:
        return "[JA]"
    return ""


def _save_alternative_image(
    content: bytes,
    filename: str,
    folder: Path,
    source_tag: str = "",
) -> str | None:
    """alternative 이미지를 primary와 같은 폴더에 저장하고 상대 경로를 반환한다."""
    folder.mkdir(parents=True, exist_ok=True)
    if source_tag:
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        filename = f"{source_tag} {stem}{suffix}"
    safe_name = _safe_filename(filename)
    with _save_lock:
        saved = save_image(content, safe_name, folder)
    return (folder / saved).relative_to(ROOT_DIR).as_posix()


def download_one_image(
    img_url: str,
    utype: str,
    post_url: str,
    folder: Path,
    idx: int,
    seen_urls: set[str],
    img_hashes: dict[str, str],
    image_map: dict[str, str],
    thumb_hashes: set[str],
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] | None = None,
    *,
    post_date: str = "",
    blog_title: str = "",
    retry_mode: bool = False,
    multilang_date_index: dict[str, list[tuple[str, str]]] | None = None,
    kakao_pf_index: dict[str, list[KakaoPFPost]] | None = None,
) -> str:
    """이미지 1건 다운로드. 성공 방식을 나타내는 문자열을 반환한다.

    Returns:
        "already"  — 이미 다운로드됨 (seen_urls 히트)
        "original" — 원본/KO Wayback으로 성공
        "multilang"— 다국어 Wayback 폴백 성공
        "kakao"    — KakaoPF 폴백 성공
        ""         — 실패
    """
    seen_key = _seen_key(utype, img_url)

    # 빠른 비잠금 확인
    if seen_key in seen_urls:
        return "already"

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
        payload = _fetch_image(img_url, allow_archive=True)
        if payload is None:
            payload = _fetch_wayback_image(img_url, allow_archive=True)
        if payload is None:
            payload = _fetch_wayback_linked_from_post(post_url, img_url, post_soup_cache,
                                                     allow_archive=True)

    elif utype == "linked_direct":
        payload = _fetch_image(img_url, allow_ext_fallback=True, allow_archive=True)
        if payload is None and _is_community_cdn(img_url):
            payload = _fetch_wayback_image(img_url, allow_ext_fallback=True, allow_archive=True)

    # ── Kakao PF 폴백 (retry 모드 전용) ──────────────────────────────────
    kakao_pf_result: tuple[bytes, str, str, str, str] | None = None
    if payload is None and retry_mode and kakao_pf_index and utype != "linked_direct":
        kakao_pf_result = _fetch_kakao_pf_image(
            post_url, img_url, post_date, utype, idx,
            kakao_pf_index, blog_title=blog_title,
        )

    # ── 다국어 Wayback 폴백 (retry 모드 전용) ────────────────────────────
    multilang_result: tuple[bytes, str, str, str, str] | None = None
    if payload is None and retry_mode and multilang_date_index and utype != "linked_direct":
        multilang_result = _fetch_multilang_wayback_image(
            post_url, img_url, post_date, utype, idx,
            multilang_date_index, post_soup_cache,
        )

    # ── 폴백 결과 선택 ────────────────────────────────────────────────────
    primary_source: str | None = None
    alt_result: tuple[bytes, str, str, str, str] | None = None
    alt_source: str | None = None

    if payload is None and kakao_pf_result and multilang_result:
        # 둘 다 성공 → 파일 크기 큰 쪽이 primary, 작은 쪽이 alternative
        kp_size = len(kakao_pf_result[0])
        ml_size = len(multilang_result[0])
        if kp_size >= ml_size:
            payload = kakao_pf_result[:4]
            primary_source = kakao_pf_result[4]  # kakao permalink
            alt_result = multilang_result
            alt_source = multilang_result[4]  # multilang post url
        else:
            payload = multilang_result[:4]
            primary_source = multilang_result[4]
            alt_result = kakao_pf_result
            alt_source = kakao_pf_result[4]  # kakao permalink
    elif payload is None and kakao_pf_result:
        payload = kakao_pf_result[:4]
        primary_source = kakao_pf_result[4]
    elif payload is None and multilang_result:
        payload = multilang_result[:4]
        primary_source = multilang_result[4]

    if payload is None:
        record_failed(post_url, img_url, "download_failed")
        return ""

    content, final_url, content_type, content_disposition = payload
    filename = _determine_filename(
        utype, img_url, final_url, content_type, content_disposition, idx
    )
    # 폴백 소스인 경우 파일명에 출처 접두사 추가
    if primary_source is not None:
        tag = _source_tag(primary_source)
        if tag:
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            filename = f"{tag} {stem}{suffix}"
    safe_name = _safe_filename(filename)

    # ── Phase 1: in-memory 상태 예약 (_state_lock, 극히 빠름) ────────────
    should_save = False
    img_key: str | None = None
    content_hash = _sha256_bytes(content)

    with _state_lock:
        # 잠금 획득 후 재확인 (다른 스레드가 먼저 처리했을 수 있음)
        if seen_key in seen_urls:
            return "already"

        existing_rel = img_hashes.get(content_hash)
        if existing_rel is not None:
            # 해시 중복: 같은 콘텐츠가 이미 저장됨 → 저장 생략, 경로만 매핑
            seen_urls.add(seen_key)
            img_key = _clean_img_url(img_url)
            if img_key not in image_map:
                image_map[img_key] = existing_rel
                _map_buf.add(f"{img_key}\t{existing_rel}")
            should_save = False
        else:
            seen_urls.add(seen_key)
            img_key = _clean_img_url(img_url)
            if utype == "og_image":
                thumb_hashes.add(content_hash)
            should_save = True

    # ── Phase 2: 파일 저장 (_save_lock, _state_lock 해제 후) ────────────
    if should_save:
        with _save_lock:
            saved_name = save_image(content, safe_name, folder)
        rel = (folder / saved_name).relative_to(ROOT_DIR).as_posix()
        is_thumb = utype == "og_image"
        # img_hashes 및 image_map 갱신
        with _state_lock:
            img_hashes[content_hash] = rel
            if img_key not in image_map:  # type: ignore[operator]
                image_map[img_key] = rel   # type: ignore[index]
                _map_buf.add(f"{img_key}\t{rel}")
        _img_hash_buf.add(f"{content_hash}\t{rel}\t{'T' if is_thumb else ''}")

    _done_buf.add(seen_key)
    primary_rel = rel if should_save else existing_rel  # type: ignore[possibly-undefined]

    # ── Alternative 이미지 저장 ───────────────────────────────────────────
    if alt_result is not None:
        alt_content = alt_result[0]
        alt_hash = _sha256_bytes(alt_content)
        # primary와 동일 해시이면 skip
        if alt_hash != content_hash:
            alt_filename = _determine_filename(
                utype, img_url, alt_result[1], alt_result[2], alt_result[3], idx
            )
            alt_tag = _source_tag(alt_source) if alt_source else ""
            alt_rel = _save_alternative_image(alt_content, alt_filename, folder,
                                              source_tag=alt_tag)
            # alt 로그 기록
            if alt_rel and alt_source:
                if alt_source.startswith("http://pf.kakao.com/"):
                    _kakao_pf_log_buf.add(f"{alt_rel}\t{post_url}\t{alt_source}\t{img_url}")
                else:
                    _multilang_log_buf.add(f"{alt_rel}\t{post_url}\t{alt_source}\t{img_url}")

    # ── 폴백 성공 로그 기록 ───────────────────────────────────────────────
    if primary_source is not None and primary_rel:
        if kakao_pf_result and primary_source == kakao_pf_result[4]:
            # primary가 Kakao PF
            _kakao_pf_log_buf.add(f"{primary_rel}\t{post_url}\t{primary_source}\t{img_url}")
        elif multilang_result and primary_source == multilang_result[4]:
            # primary가 multilang
            _multilang_log_buf.add(f"{primary_rel}\t{post_url}\t{primary_source}\t{img_url}")

    # 성공 방식 판별 (해시 중복으로 저장 생략된 경우 "dup")
    if not should_save:
        return "dup"
    if primary_source is None:
        return "original"
    if primary_source.startswith("http://pf.kakao.com/"):
        return "kakao"
    return "multilang"


# ---------------------------------------------------------------------------
# 포스트 단위 처리
# ---------------------------------------------------------------------------


def process_post(
    post_url: str,
    post_date: str,
    seen_urls: set[str],
    img_hashes: dict[str, str],
    image_map: dict[str, str],
    thumb_hashes: set[str],
    done_post_urls: dict[str, int],
    html_index: "dict[str, Path] | None" = None,
    *,
    retry_mode: bool = False,
    multilang_date_index: dict[str, list[tuple[str, str]]] | None = None,
    kakao_pf_index: dict[str, list[KakaoPFPost]] | None = None,
) -> PostProcessResult:
    # 이미 모든 이미지가 완료된 포스트 스킵
    if post_url in done_post_urls and not retry_mode:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)

    html_text = fetch_post_html(post_url, html_index)
    if html_text is None:
        if post_url in done_post_urls:
            return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)
        record_failed(post_url, "", "fetch_post_failed")
        return PostProcessResult(ok=0, fail=1, post_fetch_ok=False)

    soup = BeautifulSoup(html_text, "lxml")
    images = collect_image_urls(soup, post_url)

    # retry 모드: 이미지 수가 동일하면 스킵
    if post_url in done_post_urls and len(images) == done_post_urls[post_url]:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)

    # 블로그 포스트 제목 추출 (Kakao PF 매칭용)
    blog_title = ""
    if retry_mode and kakao_pf_index:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            blog_title = title_tag.string.strip()
        else:
            h1 = soup.find("h1")
            if h1:
                blog_title = h1.get_text(strip=True)

    # 비이미지 다운로드 링크(BGM 등) 감지 → 다운로드 건너뛰기
    non_image_urls = _detect_non_image_urls(soup, post_url)
    if retry_mode:
        for skip_url in non_image_urls:
            remove_from_failed(post_url, img_url=skip_url)

    # 카테고리 추출 → 저장 경로 결정
    category = extract_category(soup)
    date_folder = date_to_folder(post_date)
    folder = IMAGES_DIR / (category or "etc") / date_folder

    ok = fail = ok_saved = ok_original = ok_multilang = ok_kakao = ok_dedup = 0
    succeeded_urls: list[str] = []
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}
    for idx, (img_url, utype) in enumerate(images, start=1):
        if _clean_img_url(img_url) in non_image_urls:
            continue
        how = download_one_image(
            img_url,
            utype,
            post_url,
            folder,
            idx,
            seen_urls,
            img_hashes,
            image_map,
            thumb_hashes,
            post_soup_cache=post_soup_cache,
            post_date=post_date,
            blog_title=blog_title,
            retry_mode=retry_mode,
            multilang_date_index=multilang_date_index,
            kakao_pf_index=kakao_pf_index,
        )
        if how:
            ok += 1
            succeeded_urls.append(img_url)
            if how == "dup":
                ok_dedup += 1
            elif how == "already":
                pass  # 이미 다운로드된 URL
            else:
                ok_saved += 1
                if how == "original":
                    ok_original += 1
                elif how == "multilang":
                    ok_multilang += 1
                elif how == "kakao":
                    ok_kakao += 1
        else:
            fail += 1

    # 포스트 내 모든 이미지가 성공한 경우 완료로 기록 (이미지가 없는 포스트 포함)
    if fail == 0:
        done_post_urls[post_url] = len(images)
        _done_posts_buf.add(f"{post_url}\t{len(images)}")

    return PostProcessResult(ok=ok, fail=fail, post_fetch_ok=True,
                             ok_saved=ok_saved, ok_original=ok_original,
                             ok_multilang=ok_multilang, ok_kakao=ok_kakao,
                             ok_dedup=ok_dedup,
                             succeeded_urls=succeeded_urls)


def _generate_fallback_csv() -> int:
    """Kakao PF / 다국어 Wayback 폴백 로그를 읽어 CSV 리포트를 생성한다.

    Returns:
        CSV에 기록된 행 수.
    """
    rows: list[list[str]] = []
    for log_file, fallback_type in [
        (KAKAO_PF_LOG_FILE, "kakao"),
        (MULTILANG_LOG_FILE, "multilang"),
    ]:
        if not log_file.exists():
            continue
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            saved_path = parts[0]
            post_url = parts[1]
            source_url = parts[2]
            original_img_url = parts[3] if len(parts) >= 4 else ""
            rows.append([post_url, original_img_url, fallback_type, saved_path, source_url])

    if not rows:
        return 0

    rows.sort(key=lambda r: (r[0], r[2], r[3]))
    with open(FALLBACK_REPORT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["post_url", "original_img_url", "fallback_type", "saved_path", "source_url"])
        writer.writerows(rows)

    return len(rows)


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def run_images(
    posts: list[tuple[str, str]],
    retry_mode: bool = False,
    retry_multilang: bool = False,
    retry_kakaopf: bool = False,
    force_download: bool = False,
    html_index: "dict[str, Path] | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
):
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # 기존 폴백 이미지에 출처 접두사 부여
    fb_renamed = _rename_fallback_images()
    if fb_renamed:
        print(f"[이미지] 기존 폴백 이미지 {fb_renamed}개 rename (출처 접두사 추가)")

    # Alt 이미지 보충 모드
    if retry_multilang:
        _supplement_alt_images("multilang", posts, html_index, max_workers)
    if retry_kakaopf:
        _supplement_alt_images("kakaopf", posts, html_index, max_workers)
    if retry_multilang or retry_kakaopf:
        return

    seen_urls = set() if force_download else load_seen(DONE_FILE)
    img_hashes, thumb_hashes = _load_or_build_img_hashes()
    image_map = load_image_map(IMAGE_MAP_FILE)
    done_post_urls: dict[str, int] = {} if (force_download or retry_mode) else _load_done_post_urls(DONE_POSTS_FILE)

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

    # 다국어 Wayback 폴백 (retry 모드에서만 활성화)
    multilang_date_index: dict[str, list[tuple[str, str]]] = {}
    if retry_mode:
        print("[이미지] 다국어 Wayback 폴백 활성화: EN/JA 사이트맵 인덱스 구축 중...")
        multilang_date_index = _build_multilang_date_index()

    # Kakao PF 폴백 (retry 모드에서만 활성화)
    kakao_pf_index: dict[str, list[KakaoPFPost]] = {}
    if retry_mode:
        print("[이미지] Kakao PF 폴백 활성화: 게시글 인덱스 구축 중...")
        kakao_pf_index = _build_kakao_pf_index()
        if kakao_pf_index:
            total_kp = sum(len(v) for v in kakao_pf_index.values())
            print(f"  Kakao PF 인덱스: {len(kakao_pf_index)}일, 총 {total_kp}개 포스트")

    total = len(posts)
    # 대상 수가 100개 이하면 10개 단위, 초과면 50개 단위로 진행도 출력
    report_interval = 10 if total <= 100 else 50
    start = time.time()
    total_ok = 0
    total_saved = 0
    total_fail = 0
    total_original = 0
    total_multilang = 0
    total_kakao = 0
    total_dedup = 0
    completed = 0
    counter_lock = threading.Lock()

    print(f"\n{'━' * 60}")
    print(f"[이미지] 다운로드 시작: {total}개 포스트")
    print(f"{'━' * 60}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(
                process_post, url, date, seen_urls, img_hashes, image_map,
                thumb_hashes, done_post_urls,
                html_index=html_index,
                retry_mode=retry_mode,
                multilang_date_index=multilang_date_index,
                kakao_pf_index=kakao_pf_index,
            ): (url, date)
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
                total_saved += result.ok_saved
                total_fail += result.fail
                total_original += result.ok_original
                total_multilang += result.ok_multilang
                total_kakao += result.ok_kakao
                total_dedup += result.ok_dedup
                completed += 1
                cur_completed = completed

            if retry_mode and result.post_fetch_ok:
                remove_from_failed(post_url, reason="fetch_post_failed")
                if result.succeeded_urls:
                    remove_from_failed_batch(post_url, set(result.succeeded_urls))

            if cur_completed % report_interval == 0 or cur_completed == total:
                eta = eta_str(cur_completed, total, start)
                existing = total_ok - total_saved - total_dedup
                if retry_mode:
                    print(f"  {eta} 저장={total_saved} "
                          f"(원본={total_original} multilang={total_multilang} "
                          f"kakao={total_kakao}) 중복={total_dedup} "
                          f"기존={existing} 실패={total_fail}")
                else:
                    print(f"  {eta} 저장={total_saved} 중복={total_dedup} "
                          f"기존={existing} 실패={total_fail}")

    # 모든 worker 완료 후 버퍼 잔량을 파일에 flush
    _done_buf.flush_all()
    _map_buf.flush_all()
    _img_hash_buf.flush_all()
    _done_posts_buf.flush_all()
    _multilang_log_buf.flush_all()
    _kakao_pf_log_buf.flush_all()

    # 폴백 CSV 리포트 생성 (retry 시)
    if retry_mode:
        csv_count = _generate_fallback_csv()
        if csv_count:
            print(f"[이미지] 폴백 리포트: {FALLBACK_REPORT_FILE} ({csv_count}건)")

    existing = total_ok - total_saved - total_dedup
    if retry_mode:
        print(f"\n[이미지 완료] 저장={total_saved} "
              f"(원본={total_original} multilang={total_multilang} "
              f"kakao={total_kakao}) 중복={total_dedup} "
              f"기존={existing} 실패={total_fail}")
    else:
        print(f"\n[이미지 완료] 저장={total_saved} 중복={total_dedup} "
              f"기존={existing} 실패={total_fail}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Image downloader")
    parser.add_argument("--retry", action="store_true", help="Retry failed list")
    parser.add_argument("--retry-multilang", action="store_true",
                        help="KakaoPF 성공 이미지에 multilang alt 보충")
    parser.add_argument("--retry-kakaopf", action="store_true",
                        help="multilang 성공 이미지에 KakaoPF alt 보충")
    parser.add_argument("--posts", default=str(ROOT_DIR / "all_links.txt"), help="Posts list file")
    parser.add_argument("--backfill-map", action="store_true", help="Backfill image_map.tsv")
    args = parser.parse_args()

    if args.backfill_map:
        backfill_image_map()
    else:
        posts = load_posts(args.posts)
        run_images(posts, retry_mode=args.retry,
                   retry_multilang=args.retry_multilang,
                   retry_kakaopf=args.retry_kakaopf)
