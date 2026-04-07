# -*- coding: utf-8 -*-
"""EN/JA 다국어 Wayback 폴백."""

import json
import os
import urllib.parse

from bs4 import BeautifulSoup

from build_posts_list import MULTILANG_CONFIGS
from utils import SIZE_W_RE, load_posts

from .constants import (
    BLOG_HOST,
    COMMUNITY_CDN_HOST,
    MULTILANG_BLOG_HOSTS,
    MULTILANG_EARLIEST_DATE,
    MULTILANG_INDEX_CACHE,
    MULTILANG_PUBLISHED_INDEX,
    _KO_SUFFIX_RE,
    _LANG_SUFFIX_MAP,
)
from .fetch import (
    _add_im,
    _fetch_image,
    _fetch_wayback_gdrive_from_post,
    _fetch_wayback_image,
    _fetch_wayback_img_from_post,
    _fetch_wayback_linked_from_post,
    _fetch_wayback_post_soup,
    _get_content_tag,
    _original_url_from_wayback,
)
from .models import PostSoupCache


def _multilang_image_url_candidates(img_url: str) -> list[tuple[str, str]]:
    """이미지 URL의 호스트를 EN/JA로 교체하고 _KO 접미사를 치환한 후보 목록을 반환한다."""
    parsed = urllib.parse.urlparse(img_url)
    hostname = (parsed.hostname or "").lower()

    candidates: list[tuple[str, str]] = []
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        if hostname == BLOG_HOST:
            new_host = lang_host
        elif hostname == COMMUNITY_CDN_HOST:
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


def _load_published_cache() -> dict[str, list[tuple[str, str]]]:
    """published_time 기반 캐시 로드. 없으면 {} 반환."""
    if not MULTILANG_PUBLISHED_INDEX.exists():
        return {}
    try:
        with open(MULTILANG_PUBLISHED_INDEX, "r", encoding="utf-8") as f:
            raw = json.load(f)
        raw.pop("_meta", None)
        return {d: [(u, l) for u, l in entries] for d, entries in raw.items()}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def _save_published_cache(
    date_index: dict[str, list[tuple[str, str]]],
    meta: dict[str, str],
) -> None:
    raw: dict = {d: entries for d, entries in date_index.items()}
    raw["_meta"] = meta
    with open(MULTILANG_PUBLISHED_INDEX, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)


def _build_multilang_date_index() -> dict[str, list[tuple[str, str]]]:
    """EN/JA all_links 파일에서 {published_date: [(url, lang), ...]} 인덱스를 구축한다."""
    from pathlib import Path

    source_files: list[tuple[str, Path]] = []
    for lang, cfg in MULTILANG_CONFIGS.items():
        links_file = cfg["all_links"]
        if links_file.exists():
            source_files.append((lang, links_file))

    if not source_files:
        print("  EN/JA all_links 파일 없음, 빈 인덱스 반환")
        return {}

    # 캐시 유효성: all_links 파일 mtime vs 캐시 mtime
    if MULTILANG_PUBLISHED_INDEX.exists():
        cache_mtime = os.path.getmtime(MULTILANG_PUBLISHED_INDEX)
        if all(os.path.getmtime(pf) <= cache_mtime for _, pf in source_files):
            cached = _load_published_cache()
            if cached:
                total = sum(len(v) for v in cached.values())
                print(f"  published_time 인덱스 캐시 유효 ({len(cached)}일, {total}건)")
                return cached

    # 재구축
    date_index: dict[str, list[tuple[str, str]]] = {}
    meta: dict[str, str] = {}
    for lang, links_file in source_files:
        count = 0
        for url, _lastmod, published in load_posts(links_file):
            if not published:
                continue
            pub_date = published[:10]
            date_index.setdefault(pub_date, []).append((url, lang))
            count += 1
        meta[f"{lang}_count"] = str(count)
        print(f"  [{lang.upper()}] {count}건 published_time 인덱싱")

    _save_published_cache(date_index, meta)

    # 구 sitemap 캐시 삭제
    if MULTILANG_INDEX_CACHE.exists():
        MULTILANG_INDEX_CACHE.unlink()
        print("  구 sitemap 인덱스 캐시 삭제")

    return date_index


def _multilang_post_url_candidates(
    ko_post_url: str,
    post_date: str,
    date_index: dict[str, list[tuple[str, str]]],
    published_time: str = "",
) -> list[tuple[str, str]]:
    """KO 포스트 URL에 대응하는 EN/JA 포스트 URL 후보를 반환한다."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    check_date = published_time[:10] if published_time else post_date
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        if check_date and check_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
            continue
        slug_url = ko_post_url.replace(BLOG_HOST, lang_host)
        if slug_url != ko_post_url and slug_url not in seen:
            seen.add(slug_url)
            candidates.append((slug_url, lang))

    lookup_date = published_time[:10] if published_time else post_date
    if lookup_date and lookup_date in date_index:
        for alt_url, lang in date_index[lookup_date]:
            if lookup_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
                continue
            if alt_url not in seen:
                seen.add(alt_url)
                candidates.append((alt_url, lang))

    return candidates


def _fetch_wayback_img_by_position(
    alt_post_url: str,
    idx: int,
    utype: str,
    post_soup_cache: PostSoupCache = None,
) -> tuple[bytes, str, str, str] | None:
    """Wayback 포스트 스냅샷에서 idx(1-based) 위치의 이미지를 다운로드한다."""
    soup_with_base = _fetch_wayback_post_soup(alt_post_url, post_soup_cache)
    if not soup_with_base:
        return None
    soup, wayback_post = soup_with_base

    if utype == "og_image":
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            target_url = SIZE_W_RE.sub("", urllib.parse.urljoin(wayback_post, og["content"]))
            payload = _fetch_image(_add_im(target_url))
            if payload:
                return payload
        return None

    content_tag = _get_content_tag(soup)
    img_urls: list[str] = []
    for img in content_tag.find_all("img"):
        if "author-profile-image" in (img.get("class") or []):
            continue
        if img.find_parent("div", class_="author-card"):
            continue
        src = img.get("src") or img.get("data-src") or ""
        if src:
            img_urls.append(SIZE_W_RE.sub("", urllib.parse.urljoin(wayback_post, src)))

    target_idx = idx - 1
    if target_idx < 0 or target_idx >= len(img_urls):
        return None

    target_url = img_urls[target_idx]
    payload = _fetch_image(_add_im(target_url))
    if payload:
        return payload
    original = _original_url_from_wayback(target_url)
    return _fetch_image(original) if original != target_url else None


def _fetch_multilang_wayback_image(
    post_url: str,
    img_url: str,
    post_date: str,
    utype: str,
    idx: int,
    date_index: dict[str, list[tuple[str, str]]],
    post_soup_cache: PostSoupCache = None,
    published_time: str = "",
) -> tuple[bytes, str, str, str, str] | None:
    """다국어 블로그 Wayback 스냅샷에서 이미지를 탐색하는 통합 폴백 함수.

    Returns:
        성공 시 (content, final_url, content_type, cd, fallback_post_url) 5-tuple.
    """
    check_date = published_time[:10] if published_time else post_date
    if check_date and all(
        check_date < earliest for earliest in MULTILANG_EARLIEST_DATE.values()
    ):
        return None

    # Phase A: URL/파일명 기반 매칭
    img_candidates = _multilang_image_url_candidates(img_url)
    for candidate_img_url, lang in img_candidates:
        if check_date and check_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
            continue
        payload = _fetch_wayback_image(candidate_img_url)
        if payload:
            return (*payload, "")

    # Phase A-2: 포스트 HTML에서 URL 매칭
    post_candidates = _multilang_post_url_candidates(
        post_url, post_date, date_index, published_time=published_time,
    )
    for alt_post_url, lang in post_candidates:
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

    # Phase B: Position 기반 매칭
    for alt_post_url, lang in post_candidates:
        payload = _fetch_wayback_img_by_position(
            alt_post_url, idx, utype, post_soup_cache
        )
        if payload:
            return (*payload, alt_post_url)

    return None
