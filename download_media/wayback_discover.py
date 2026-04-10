# -*- coding: utf-8 -*-
"""Cat C: Wayback 포럼 스냅샷에서 삭제된 미디어 복구.

현재 blog HTML 에 `<video>` 흔적이 없는 포럼 시대 포스트는, Wayback CDX 에
보존된 포럼 스냅샷에서 미디어 태그를 찾아 복구한다. 각 미디어에 대해 인접
텍스트(앵커)를 추출해서 html_local 삽입 위치 힌트로 저장한다.

블로그→포럼 URL 매핑: Ghost 블로그 slug 와 XE3 포럼 slug 는 전혀 다른 체계다
(blog: `miriboneun-5weolyi-gaebalja-noteu`, forum: `개발자-노트-미리보는-5월의-개발자-노트`).
매핑 키는 제목. `_find_forum_url_by_title()` 가 포스트 제목과 published_time 을
사용해 Wayback CDX 에서 해당 기간의 `community-ko.lordofheroes.com/news/` 스냅샷을
조회하고 디코드된 URL 경로에 제목 슬러그가 포함되는 항목을 찾는다.
"""

import datetime
import re
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from download_images.fetch import _fetch_wayback_post_soup, _get_content_tag
from download_images.models import PostSoupCache
from utils import fetch_with_retry
from config import WAYBACK_CDX_API

# blog_url → forum_url 매핑 캐시 (프로세스 수명)
_forum_url_cache: dict[str, str | None] = {}
_forum_url_cache_lock = threading.Lock()

from .constants import (
    ANCHOR_APPEND,
    ANCHOR_MAX_LEN,
    ANCHOR_MIN_LEN,
    ANCHOR_POSITIONED,
    MEDIA_EXTS,
)
from .fetch import strip_wayback_prefix

_WS_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """공백 정규화 + trim."""
    return _WS_RE.sub(" ", (text or "").replace("\xa0", " ").replace("\u3000", " ")).strip()


def _tail(text: str, max_len: int = ANCHOR_MAX_LEN) -> str:
    """텍스트 끝부분(미디어 태그 직전)을 max_len 자로 자른다."""
    t = _normalize_text(text)
    if len(t) <= max_len:
        return t
    return t[-max_len:]


def _extract_anchor_text(media_tag: Tag, content_tag: Tag | BeautifulSoup) -> str:
    """media_tag 직전의 의미 있는 텍스트 블록을 추출한다.

    우선순위:
      1. 동일 부모 내 직전 sibling <p>/<h1-6> 의 텍스트
      2. find_all_previous 로 content_tag 범위 내 <p>/<h1-6>
      3. find_previous(text) 로 텍스트 노드 fallback

    길이 < ANCHOR_MIN_LEN 이면 "" 반환 → 호출측에서 append 로 처리.
    """
    # 미디어 태그가 <figure> 등으로 감싸져 있으면 래퍼를 앵커 기준으로 사용
    anchor_node: Tag = media_tag
    parent = media_tag.parent
    while parent is not None and parent.name in ("figure", "div", "p", "span"):
        # parent 의 직계 children 중 텍스트가 거의 없고 media_tag 만 주로 가지면 래퍼로 간주
        has_other_text = any(
            (child.get_text(strip=True) if hasattr(child, "get_text") else str(child).strip())
            for child in parent.children
            if child is not media_tag
        )
        if has_other_text:
            break
        anchor_node = parent
        parent = parent.parent

    # 1. 직전 sibling 탐색
    for sib in anchor_node.previous_siblings:
        if not hasattr(sib, "name") or sib.name is None:
            text = _normalize_text(str(sib))
            if len(text) >= ANCHOR_MIN_LEN:
                return _tail(text)
            continue
        if sib.name in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
            text = sib.get_text(" ", strip=True)
            text = _normalize_text(text)
            if len(text) >= ANCHOR_MIN_LEN:
                return _tail(text)

    # 2. content_tag 범위 내 find_all_previous
    for prev in anchor_node.find_all_previous(
        ["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"]
    ):
        if content_tag is not None and hasattr(prev, "parents"):
            if content_tag not in prev.parents and prev is not content_tag:
                break
        text = _normalize_text(prev.get_text(" ", strip=True))
        if len(text) >= ANCHOR_MIN_LEN:
            return _tail(text)

    return ""


def _iter_media_tags(content_tag: Tag | BeautifulSoup):
    """content_tag 하위에서 복구 대상 미디어 태그/앵커를 yield 한다."""
    for tag_name in ("video", "audio", "source"):
        for tag in content_tag.find_all(tag_name):
            # <source> 가 <video>/<audio> 내부면 별도로 yield 안 함 (부모가 이미 처리)
            if tag_name == "source" and tag.find_parent(("video", "audio", "picture")):
                continue
            yield tag

    for anchor in content_tag.find_all("a", href=True):
        href = (anchor["href"] or "").strip()
        if not href or href.startswith(("#", "mailto:")):
            continue
        stripped = strip_wayback_prefix(href)
        ext = Path(urllib.parse.urlparse(stripped).path).suffix.lower()
        if ext in MEDIA_EXTS:
            yield anchor


def _tag_to_source_url(tag: Tag, wayback_post: str) -> str:
    """태그에서 미디어 URL 을 추출하고 Wayback 접두사를 제거한다."""
    if tag.name == "a":
        href = tag.get("href") or ""
        absolute = urllib.parse.urljoin(wayback_post, href)
        return strip_wayback_prefix(absolute)

    # <video>/<audio>: src 우선, 없으면 첫 <source>
    src = tag.get("src") or ""
    if not src:
        child_source = tag.find("source")
        if child_source:
            src = child_source.get("src") or ""
    if not src:
        return ""
    absolute = urllib.parse.urljoin(wayback_post, src)
    return strip_wayback_prefix(absolute)


def _tag_to_mtype(tag: Tag) -> str:
    if tag.name == "video":
        return "video_tag"
    if tag.name == "audio":
        return "audio_tag"
    if tag.name == "source":
        return "video_tag"
    if tag.name == "a":
        return "anchor_direct"
    return "video_tag"


_TITLE_NORM_RE = re.compile(r"[\s\-_]+")


def _title_slug(title: str) -> str:
    """제목 → 정규화된 slug (공백·하이픈·언더스코어 모두 단일 '-')."""
    return _TITLE_NORM_RE.sub("-", (title or "").strip()).strip("-")


def _parse_iso_date(iso: str) -> datetime.date | None:
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cdx_list_forum_urls(from_ts: str, to_ts: str, limit: int = 1000) -> list[str]:
    """특정 기간의 community-ko.lordofheroes.com/news/ prefix URL 목록을 CDX 로 조회."""
    params = {
        "url": "community-ko.lordofheroes.com/news/",
        "matchType": "prefix",
        "output": "json",
        "fl": "original",
        "limit": str(limit),
        "from": from_ts,
        "to": to_ts,
        "filter": "statuscode:200",
    }
    resp = fetch_with_retry(WAYBACK_CDX_API, params=params, timeout=30)
    if resp is None:
        return []
    try:
        rows = resp.json()
    except ValueError:
        return []
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if not isinstance(row, list) or not row:
            continue
        u = str(row[0]).strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _find_forum_url_by_title(
    blog_url: str,
    title: str,
    published_time_iso: str,
) -> str | None:
    """제목 + published_time 으로 Wayback CDX 에서 포럼 URL 을 찾는다."""
    with _forum_url_cache_lock:
        if blog_url in _forum_url_cache:
            return _forum_url_cache[blog_url]

    result: str | None = None
    if not title:
        with _forum_url_cache_lock:
            _forum_url_cache[blog_url] = None
        return None

    pub_date = _parse_iso_date(published_time_iso)
    if pub_date is None:
        # fallback: 넓은 범위 (포럼 수명 전체)
        from_ts = "20190101"
        to_ts = "20211231"
    else:
        # published_time 전후 ±30일
        window = datetime.timedelta(days=30)
        from_ts = (pub_date - window).strftime("%Y%m%d")
        to_ts = (pub_date + window).strftime("%Y%m%d")

    candidates = _cdx_list_forum_urls(from_ts, to_ts, limit=2000)
    target_slug = _title_slug(title)
    if not target_slug:
        with _forum_url_cache_lock:
            _forum_url_cache[blog_url] = None
        return None

    target_lower = target_slug.lower()
    for raw in candidates:
        try:
            decoded = urllib.parse.unquote(raw)
        except Exception:
            continue
        decoded_slug = _title_slug(decoded)
        if target_lower in decoded_slug.lower():
            result = raw
            break

    with _forum_url_cache_lock:
        _forum_url_cache[blog_url] = result
    return result


def discover_forum_media(
    post_url: str,
    post_soup_cache: PostSoupCache = None,
    *,
    blog_soup: BeautifulSoup | None = None,
    published_time: str = "",
) -> list[tuple[str, str, str, str]]:
    """Wayback 포럼 스냅샷에서 미디어 항목을 추출한다.

    blog_soup 이 주어지면 og:title 로 포럼 URL 을 탐색한다. 찾지 못하면
    post_url 직접 조회로 폴백 (포스트 migration 전 blog URL 이 이미 Wayback 에
    있는 드문 경우 커버).

    Returns:
        list[(media_url, mtype, anchor_type, anchor_text)]
    """
    wayback_target_url = post_url
    if blog_soup is not None:
        title = ""
        og_title = blog_soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
        if not title:
            title_tag = blog_soup.find("title")
            if title_tag and title_tag.string:
                title = title_tag.string.strip()
        if title:
            forum_url = _find_forum_url_by_title(post_url, title, published_time)
            if forum_url:
                wayback_target_url = forum_url

    soup_with_base = _fetch_wayback_post_soup(wayback_target_url, post_soup_cache)
    if not soup_with_base:
        return []
    soup, wayback_post = soup_with_base

    content_tag = _get_content_tag(soup)
    if content_tag is None:
        return []

    results: list[tuple[str, str, str, str]] = []
    seen_urls: set[str] = set()

    for tag in _iter_media_tags(content_tag):
        media_url = _tag_to_source_url(tag, wayback_post)
        if not media_url or not media_url.startswith("http"):
            continue
        if media_url in seen_urls:
            continue
        seen_urls.add(media_url)

        anchor_text = _extract_anchor_text(tag, content_tag)
        anchor_type = ANCHOR_POSITIONED if anchor_text else ANCHOR_APPEND
        mtype = _tag_to_mtype(tag)
        results.append((media_url, mtype, anchor_type, anchor_text))
        # Cat C 비디오에 poster 가 있어도 v1 에서는 무시 (html_local 이 poster 없이 주입).

    return results
