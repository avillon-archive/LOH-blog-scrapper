# -*- coding: utf-8 -*-
"""EN/JA 다국어 Wayback 폴백."""

import json
import urllib.parse

from bs4 import BeautifulSoup

from build_posts_list import fetch_newest_single_sitemap_date, parse_sitemap
from utils import SIZE_W_RE, fetch_with_retry

from .constants import (
    BLOG_HOST,
    COMMUNITY_CDN_HOST,
    MULTILANG_BLOG_HOSTS,
    MULTILANG_EARLIEST_DATE,
    MULTILANG_INDEX_CACHE,
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


def _load_multilang_cache() -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """캐시 파일에서 date_index와 meta를 로드. 없으면 ({}, {}) 반환."""
    if not MULTILANG_INDEX_CACHE.exists():
        return {}, {}
    try:
        with open(MULTILANG_INDEX_CACHE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        meta = raw.pop("_meta", {})
        index = {d: [(u, l) for u, l in entries] for d, entries in raw.items()}
        return index, meta
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}, {}


def _save_multilang_cache(
    date_index: dict[str, list[tuple[str, str]]], meta: dict[str, str]
) -> None:
    raw: dict = {d: entries for d, entries in date_index.items()}
    raw["_meta"] = meta
    with open(MULTILANG_INDEX_CACHE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)


def _build_multilang_date_index() -> dict[str, list[tuple[str, str]]]:
    """EN/JA 사이트맵을 직접 가져와 {date: [(post_url, lang), ...]} 인덱스를 구축한다."""
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

    for lang in need_refresh:
        date_index = {
            d: [(u, l) for u, l in entries if l != lang]
            for d, entries in date_index.items()
        }
        date_index = {d: entries for d, entries in date_index.items() if entries}

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

    for lang, lang_host in MULTILANG_BLOG_HOSTS.items():
        if post_date and post_date < MULTILANG_EARLIEST_DATE.get(lang, ""):
            continue
        slug_url = ko_post_url.replace(BLOG_HOST, lang_host)
        if slug_url != ko_post_url and slug_url not in seen:
            seen.add(slug_url)
            candidates.append((slug_url, lang))

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
) -> tuple[bytes, str, str, str, str] | None:
    """다국어 블로그 Wayback 스냅샷에서 이미지를 탐색하는 통합 폴백 함수.

    Returns:
        성공 시 (content, final_url, content_type, cd, fallback_post_url) 5-tuple.
    """
    if post_date and all(
        post_date < earliest for earliest in MULTILANG_EARLIEST_DATE.values()
    ):
        return None

    # Phase A: URL/파일명 기반 매칭
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
