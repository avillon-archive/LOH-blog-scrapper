# -*- coding: utf-8 -*-
"""Kakao PF 폴백."""

import difflib
import json
import time
from datetime import datetime, timedelta, timezone

from utils import fetch_with_retry

from .constants import (
    KAKAO_PF_API,
    KAKAO_PF_INDEX_FILE,
    KAKAO_PF_PROFILE,
)
from .fetch import _fetch_image
from .models import KakaoPFPost

_KST = timezone(timedelta(hours=9))


def _ms_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=_KST).strftime("%Y-%m-%d")


def _extract_media_urls(media_list: list[dict]) -> list[str]:
    urls: list[str] = []
    for m in media_list:
        if m.get("type") == "image" and m.get("url"):
            urls.append(m["url"])
        elif m.get("type") == "link":
            for img in m.get("images") or []:
                if img.get("url"):
                    urls.append(img["url"])
                    break
    return urls


def _build_kakao_pf_index() -> dict[str, list[KakaoPFPost]]:
    """Kakao PF 게시글을 페이지네이션하며 {date: [KakaoPFPost, ...]} 인덱스를 구축한다."""
    # 캐시 로드
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

    # 새 포스트 fetch
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
        except (ValueError, KeyError):
            print(f"  Kakao PF JSON 파싱 실패 (page {page}), 중단")
            break

        items = body.get("items", [])
        if not items:
            break

        for item in items:
            sort_val = str(item.get("sort", ""))
            if last_sort and sort_val <= last_sort:
                items = []
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
        time.sleep(0.2)

    if new_posts:
        print(f"  Kakao PF 새 포스트: {len(new_posts)}개 수집")

    # 캐시 병합 및 저장
    all_raw = new_posts + cached_posts
    all_raw.sort(key=lambda p: p.get("sort", ""), reverse=True)
    seen_ids: set[int] = set()
    deduped: list[dict] = []
    for p in all_raw:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            deduped.append(p)

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

    # date → [KakaoPFPost] 인덱스 구축
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

    if not kp.media_urls:
        return None

    if utype == "og_image":
        target_url = kp.media_urls[0]
    elif utype == "img":
        media_idx = idx - 1
        if media_idx < 0 or media_idx >= len(kp.media_urls):
            return None
        target_url = kp.media_urls[media_idx]
    else:
        media_idx = idx - 1
        if media_idx < 0 or media_idx >= len(kp.media_urls):
            return None
        target_url = kp.media_urls[media_idx]

    payload = _fetch_image(target_url)
    if payload is None:
        return None

    permalink = f"http://pf.kakao.com/{KAKAO_PF_PROFILE}/{kp.id}"
    return (*payload, permalink)
