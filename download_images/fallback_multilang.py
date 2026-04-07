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
    EN_CAT_NORMALIZE,
    JA_CAT_NORMALIZE,
    KO_TO_LANG_CAT,
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


_multilang_cat_index: dict[str, str] = {}
"""EN/JA URL → 정규화된 카테고리. _build_multilang_date_index() 에서 갱신."""

_multilang_lastmod_index: dict[str, str] = {}
"""EN/JA URL → lastmod. _build_multilang_date_index() 에서 갱신."""

_CAT_NORMALIZE = {"en": EN_CAT_NORMALIZE, "ja": JA_CAT_NORMALIZE}


def _load_published_cache() -> tuple[
    dict[str, list[tuple[str, str]]], dict[str, str]
]:
    """published_time 기반 캐시 로드. 없으면 ({}, {}) 반환."""
    if not MULTILANG_PUBLISHED_INDEX.exists():
        return {}, {}
    try:
        with open(MULTILANG_PUBLISHED_INDEX, "r", encoding="utf-8") as f:
            raw = json.load(f)
        raw.pop("_meta", None)
        categories = raw.pop("_categories", {})
        date_index = {d: [(u, l) for u, l in entries] for d, entries in raw.items()}
        return date_index, categories
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}, {}


def _save_published_cache(
    date_index: dict[str, list[tuple[str, str]]],
    meta: dict[str, str],
    categories: dict[str, str],
) -> None:
    raw: dict = {d: entries for d, entries in date_index.items()}
    raw["_meta"] = meta
    raw["_categories"] = categories
    with open(MULTILANG_PUBLISHED_INDEX, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)


def _build_multilang_cat_index() -> dict[str, str]:
    """EN/JA HTML에서 {url: normalized_category} 인덱스를 구축한다."""
    from pathlib import Path

    from utils import build_html_index

    cat_index: dict[str, str] = {}
    for lang, cfg in MULTILANG_CONFIGS.items():
        html_dir = cfg["html_dir"]
        done_file = cfg["done_html"]
        if not Path(html_dir).exists():
            continue
        normalize = _CAT_NORMALIZE.get(lang, {})
        html_index = build_html_index(html_dir, done_file)
        count = 0
        for url, path in html_index.items():
            if not path.exists():
                continue
            try:
                soup = BeautifulSoup(
                    path.read_text(encoding="utf-8"), "lxml"
                )
                meta = soup.find("meta", property="article:tag")
                if meta and meta.get("content"):
                    cat = meta["content"].strip()
                    cat_index[url] = normalize.get(cat, cat)
                    count += 1
            except Exception:
                continue
        print(f"  [{lang.upper()}] {count}건 카테고리 인덱싱")
    return cat_index


def _build_multilang_date_index() -> dict[str, list[tuple[str, str]]]:
    """EN/JA all_links 파일에서 {published_date: [(url, lang), ...]} 인덱스를 구축한다."""
    global _multilang_cat_index, _multilang_lastmod_index
    from pathlib import Path

    source_files: list[tuple[str, Path]] = []
    for lang, cfg in MULTILANG_CONFIGS.items():
        links_file = cfg["all_links"]
        if links_file.exists():
            source_files.append((lang, links_file))

    if not source_files:
        print("  EN/JA all_links 파일 없음, 빈 인덱스 반환")
        return {}

    # 캐시 유효성: all_links 파일 mtime + html_dir mtime vs 캐시 mtime
    html_dirs = [cfg["html_dir"] for _, cfg in MULTILANG_CONFIGS.items()
                 if Path(cfg["html_dir"]).exists()]
    all_sources = [pf for _, pf in source_files] + html_dirs
    if MULTILANG_PUBLISHED_INDEX.exists():
        cache_mtime = os.path.getmtime(MULTILANG_PUBLISHED_INDEX)
        if all(os.path.getmtime(s) <= cache_mtime for s in all_sources):
            cached, categories = _load_published_cache()
            if cached:
                _multilang_cat_index = categories
                _build_lastmod_index(source_files)
                total = sum(len(v) for v in cached.values())
                print(f"  published_time 인덱스 캐시 유효 ({len(cached)}일, {total}건, cat={len(categories)})")
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

    # lastmod 인덱스 (메모리만, 캐시 불필요)
    _build_lastmod_index(source_files)

    # 카테고리 인덱스
    categories = _build_multilang_cat_index()
    _multilang_cat_index = categories

    _save_published_cache(date_index, meta, categories)

    # 구 sitemap 캐시 삭제
    if MULTILANG_INDEX_CACHE.exists():
        MULTILANG_INDEX_CACHE.unlink()
        print("  구 sitemap 인덱스 캐시 삭제")

    return date_index


def _build_lastmod_index(source_files) -> None:
    """EN/JA all_links에서 {url: lastmod} 인덱스를 메모리에 구축."""
    global _multilang_lastmod_index
    _multilang_lastmod_index = {}
    for _lang, links_file in source_files:
        for url, lastmod, _pub in load_posts(links_file):
            if lastmod:
                _multilang_lastmod_index[url] = lastmod


def _multilang_post_url_candidates(
    ko_post_url: str,
    post_date: str,
    date_index: dict[str, list[tuple[str, str]]],
    published_time: str = "",
    ko_lastmod: str = "",
    ko_category: str = "",
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """KO 포스트 URL에 대응하는 EN/JA 포스트 URL 후보를 (confirmed, unconfirmed)로 반환한다.

    confirmed: slug 교체 후보 + 카테고리/lastmod 시그널 일치 후보 (score 내림차순).
    unconfirmed: 시그널 없는 나머지 후보.
    """
    confirmed: list[tuple[str, str]] = []
    seen: set[str] = set()

    # 1) slug 교체 후보 — 항상 confirmed
    check_date = published_time[:10] if published_time else post_date
    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        if check_date and check_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
            continue
        slug_url = ko_post_url.replace(BLOG_HOST, lang_host)
        if slug_url != ko_post_url and slug_url not in seen:
            seen.add(slug_url)
            confirmed.append((slug_url, lang))

    # 2) date_index 후보 — 카테고리/lastmod로 스코어링
    lookup_date = published_time[:10] if published_time else post_date
    scored: list[tuple[int, str, str]] = []  # (score, url, lang)
    unconfirmed: list[tuple[str, str]] = []

    if lookup_date and lookup_date in date_index:
        for alt_url, lang in date_index[lookup_date]:
            if lookup_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
                continue
            if alt_url in seen:
                continue
            seen.add(alt_url)

            score = 0
            # 카테고리 시그널
            if ko_category:
                expected = KO_TO_LANG_CAT.get(lang, {}).get(ko_category)
                if expected and _multilang_cat_index.get(alt_url) == expected:
                    score += 1
            # lastmod 시그널
            if ko_lastmod and _multilang_lastmod_index.get(alt_url) == ko_lastmod:
                score += 1

            if score > 0:
                scored.append((score, alt_url, lang))
            else:
                unconfirmed.append((alt_url, lang))

    # score 내림차순 정렬 후 confirmed에 추가
    scored.sort(key=lambda x: -x[0])
    confirmed.extend((url, lang) for _, url, lang in scored)

    return confirmed, unconfirmed


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
    ko_lastmod: str = "",
    ko_category: str = "",
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

    # 후보 분류: confirmed (시그널 일치) vs unconfirmed
    confirmed, unconfirmed = _multilang_post_url_candidates(
        post_url, post_date, date_index,
        published_time=published_time,
        ko_lastmod=ko_lastmod,
        ko_category=ko_category,
    )
    all_candidates = confirmed + unconfirmed

    # Phase A-2: 전체 후보 시도 (URL 매칭 — 안전)
    for alt_post_url, lang in all_candidates:
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

    # Phase B: confirmed만 시도 (위치 기반 — 오매칭 위험)
    for alt_post_url, lang in confirmed:
        payload = _fetch_wayback_img_by_position(
            alt_post_url, idx, utype, post_soup_cache
        )
        if payload:
            return (*payload, alt_post_url)

    return None
