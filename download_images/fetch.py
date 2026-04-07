# -*- coding: utf-8 -*-
"""Wayback CDX, 이미지 fetch, 포스트 soup 파싱."""

import re
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from utils import fetch_with_retry

from .constants import (
    _ARCHIVE_CONTENT_TYPES,
    GDRIVE_HOSTS,
    IMG_EXTS,
    WAYBACK_CDX_API,
)
from .models import PostSoupCache
from .state import _wayback_cache, _wayback_cache_lock, _wayback_events
from .url_utils import _normalized_link_key, _strip_ref_param


# ---------------------------------------------------------------------------
# Wayback CDX
# ---------------------------------------------------------------------------


def _wayback_oldest(url: str) -> str | None:
    """Wayback CDX API에서 가장 오래된 200 스냅샷 URL을 반환한다.

    동일 URL에 대해 여러 스레드가 동시에 CDX 요청을 보내는 것을 방지하기 위해
    이벤트 기반 대기를 사용한다.
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
            except (ValueError, KeyError):
                pass
    finally:
        with _wayback_cache_lock:
            _wayback_cache[url] = result
            _wayback_events.pop(url, None)
        event.set()
    return result


def _add_im(wayback_url: str) -> str:
    return re.sub(r"(/web/\d+)/", r"\1im_/", wayback_url)


_WAYBACK_ORIGINAL_RE = re.compile(
    r"^https?://web\.archive\.org/web/\d+[a-z_]*/(.+)$", re.IGNORECASE
)


def _original_url_from_wayback(url: str) -> str:
    """Wayback 재작성 URL에서 원본 URL을 추출한다. 일반 URL이면 그대로 반환한다."""
    m = _WAYBACK_ORIGINAL_RE.match(url)
    return m.group(1) if m else url


# ---------------------------------------------------------------------------
# HTTP 응답 → 이미지 변환
# ---------------------------------------------------------------------------


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
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return name
    return ""


def _is_image_ct(content_type: str) -> bool:
    return content_type.lower().startswith("image/")


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
    from .constants import ARCHIVE_EXTS
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

    resp = fetch_with_retry(wayback_url, allow_redirects=True)
    if resp is not None:
        direct = _response_to_image(resp, **fetch_kwargs)
        if direct is not None:
            return direct
        original_target = _original_url_from_wayback(resp.url)
        if original_target and _normalized_link_key(original_target) != _normalized_link_key(url):
            result = _fetch_image(original_target, **fetch_kwargs)
            if result is not None:
                return result
            target_wayback = _wayback_oldest(original_target)
            if target_wayback:
                result = _fetch_image(_add_im(target_wayback), **fetch_kwargs)
                if result is not None:
                    return result

    return _fetch_image(_add_im(wayback_url), **fetch_kwargs)


# ---------------------------------------------------------------------------
# Wayback 포스트 soup
# ---------------------------------------------------------------------------


def _fetch_wayback_post_soup(
    post_url: str,
    post_soup_cache: PostSoupCache = None,
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


def _get_content_tag(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """Ghost CMS 본문 컨테이너를 반환한다 (좁은 범위 우선)."""
    return (
        soup.select_one(".gh-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.find("main")
        or soup
    )


# ---------------------------------------------------------------------------
# Wayback 포스트 스냅샷에서 이미지 탐색
# ---------------------------------------------------------------------------


def _fetch_wayback_gdrive_from_post(
    post_url: str,
    original_img_url: str,
    post_soup_cache: PostSoupCache = None,
) -> tuple[bytes, str, str, str] | None:
    soup_with_base = _fetch_wayback_post_soup(post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base

    target_key = _normalized_link_key(original_img_url)

    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src:
            continue
        candidate_url = urllib.parse.urljoin(wayback_post, src)
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


def _fetch_wayback_img_from_post(
    post_url: str,
    original_img_url: str,
    post_soup_cache: PostSoupCache = None,
) -> tuple[bytes, str, str, str] | None:
    """Wayback 포스트 스냅샷에서 original_img_url과 URL이 일치하는 img/og:image를 탐색한다."""
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
    post_soup_cache: PostSoupCache = None,
    *,
    allow_archive: bool = False,
) -> tuple[bytes, str, str, str] | None:
    soup_with_base = _fetch_wayback_post_soup(post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base
    target_key = _normalized_link_key(original_link)
    if not target_key:
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
