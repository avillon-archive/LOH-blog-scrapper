# -*- coding: utf-8 -*-
"""이미지 URL 수집."""

import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from .constants import (
    BLOG_HOST,
    COMMUNITY_CDN_HOST,
    COMMUNITY_SITE_HOST,
    DL_KEYWORDS,
    DOWNLOADABLE_EXTS,
    GAME_CDN_HOST,
    GDRIVE_HOSTS,
    IMG_EXTS,
    is_gdrive_host,
    RESOLUTION_RE,
    _NON_IMAGE_CONTEXT_KEYWORDS,
    _SKIP_LINK_HOSTS,
)
from .fetch import _get_content_tag
from .url_utils import _clean_img_url


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
        if "kg-bookmark-icon" in (img.get("class") or []):
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
        if is_gdrive_host(hostname):
            _add(abs_src, "gdrive")
        elif "/content/images/" in parsed_src.path and hostname == BLOG_HOST:
            _add(abs_src, "img")
        elif hostname in (COMMUNITY_CDN_HOST, COMMUNITY_SITE_HOST, GAME_CDN_HOST) and path_ext in IMG_EXTS:
            _add(abs_src, "img")

    for anchor in content_tag.find_all("a", href=True):
        if anchor.find_parent("div", class_="author-card"):
            continue
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:")):
            continue
        abs_href = urllib.parse.urljoin(post_url, href)
        parsed = urllib.parse.urlparse(abs_href)
        if is_gdrive_host(parsed.hostname):
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
        if not is_gdrive_host(parsed.hostname):
            continue
        anchor_text = anchor.get_text(strip=True).lower()
        if any(kw in anchor_text for kw in _NON_IMAGE_CONTEXT_KEYWORDS):
            skip_urls.add(_clean_img_url(abs_href))
            continue
        # 같은 부모 블록 내 이전 텍스트에서 키워드 탐색
        _found = False
        for sib in anchor.previous_siblings:
            sib_text = (
                sib.get_text(strip=True) if hasattr(sib, "get_text") else str(sib)
            ).lower()
            if any(kw in sib_text for kw in _NON_IMAGE_CONTEXT_KEYWORDS):
                skip_urls.add(_clean_img_url(abs_href))
                _found = True
                break
            if len(sib_text) > 200:
                break
        if _found:
            continue
        # content_tag 내 이전 heading에서 키워드 탐색
        for prev in anchor.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6"]):
            if content_tag not in (prev.parents if hasattr(prev, "parents") else []):
                break
            text = prev.get_text(strip=True).lower()
            if any(kw in text for kw in _NON_IMAGE_CONTEXT_KEYWORDS):
                skip_urls.add(_clean_img_url(abs_href))
            break
    return skip_urls
