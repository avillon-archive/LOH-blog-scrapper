# -*- coding: utf-8 -*-
"""포스트 단위 처리 (원본 다운로드 / 폴백 보존)."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from utils import (
    ROOT_DIR,
    date_to_folder,
    extract_category,
    fetch_post_html,
)

from .collect import _detect_non_image_urls, collect_image_urls
from .constants import (
    FALLBACK_IMAGES_DIR,
    IMAGES_DIR,
)
from .download import (
    _determine_filename,
    _save_alternative_image,
    _source_tag,
    download_one_image,
)
from .fallback_kakao import KakaoPFPost, _fetch_kakao_pf_image
from .fallback_multilang import _fetch_multilang_wayback_image
from .hashing import _sha256_bytes
from .models import PostProcessResult
from .persistence import (
    record_failed,
    remove_from_failed,
    remove_from_failed_batch,
    save_image,
)
from .state import (
    _done_posts_buf,
    _fb_done_buf,
    _fb_img_hash_buf,
    _fb_kakao_pf_log_buf,
    _fb_map_buf,
    _fb_multilang_log_buf,
    _save_lock,
    _state_lock,
)
from .url_utils import _clean_img_url, _safe_filename, _seen_key


# ---------------------------------------------------------------------------
# 포스트 단위 처리 (원본/KO Wayback)
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
    published_time: str = "",
) -> PostProcessResult:
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

    if post_url in done_post_urls and len(images) == done_post_urls[post_url]:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)

    non_image_urls = _detect_non_image_urls(soup, post_url)
    if retry_mode:
        for skip_url in non_image_urls:
            remove_from_failed(post_url, img_url=skip_url)

    category = extract_category(soup)
    date_folder = date_to_folder(post_date)
    folder = IMAGES_DIR / (category or "etc") / date_folder

    ok = fail = ok_saved = ok_dedup = 0
    succeeded_urls: list[str] = []
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}
    for idx, (img_url, utype) in enumerate(images, start=1):
        if _clean_img_url(img_url) in non_image_urls:
            continue
        how = download_one_image(
            img_url, utype, post_url, folder, idx,
            seen_urls, img_hashes, image_map, thumb_hashes,
            post_soup_cache=post_soup_cache,
            post_date=post_date,
        )
        if how:
            ok += 1
            succeeded_urls.append(img_url)
            if how == "dup":
                ok_dedup += 1
            elif how != "already":
                ok_saved += 1
        else:
            fail += 1

    if fail == 0:
        done_post_urls[post_url] = len(images)
        _done_posts_buf.add(f"{post_url}\t{len(images)}")

    return PostProcessResult(ok=ok, fail=fail, post_fetch_ok=True,
                             ok_saved=ok_saved, ok_original=ok_saved,
                             ok_dedup=ok_dedup,
                             succeeded_urls=succeeded_urls)


# ---------------------------------------------------------------------------
# 폴백 보존용 포스트 처리 (--retry-fallback)
# ---------------------------------------------------------------------------


def process_post_fallback(
    post_url: str,
    post_date: str,
    fb_seen_urls: set[str],
    fb_img_hashes: dict[str, str],
    html_index: "dict[str, Path] | None" = None,
    *,
    multilang_date_index: dict[str, list[tuple[str, str]]] | None = None,
    kakao_pf_index: dict[str, list[KakaoPFPost]] | None = None,
    published_time: str = "",
) -> PostProcessResult:
    """실패 이미지에 대해 multilang/kakao 폴백을 시도해 별도 디렉토리에 보존한다.

    primary 트래킹(image_map, downloaded_urls, failed_images)은 건드리지 않는다.
    """
    html_text = fetch_post_html(post_url, html_index)
    if html_text is None:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=False)

    soup = BeautifulSoup(html_text, "lxml")
    images = collect_image_urls(soup, post_url)

    blog_title = ""
    if kakao_pf_index:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            blog_title = title_tag.string.strip()
        else:
            h1 = soup.find("h1")
            if h1:
                blog_title = h1.get_text(strip=True)

    category = extract_category(soup)
    date_folder = date_to_folder(post_date)
    folder = FALLBACK_IMAGES_DIR / (category or "etc") / date_folder

    ok_saved = 0
    ok_kakao = 0
    ok_multilang = 0
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}

    for idx, (img_url, utype) in enumerate(images, start=1):
        if utype == "linked_direct":
            continue

        seen_key = _seen_key(utype, img_url)
        if seen_key in fb_seen_urls:
            continue

        # ── kakao + multilang 동시 시도 ──────────────────────────────────
        kakao_result = None
        if kakao_pf_index:
            kakao_result = _fetch_kakao_pf_image(
                post_url, img_url, post_date, utype, idx,
                kakao_pf_index, blog_title=blog_title,
                published_time=published_time,
            )

        multilang_result = None
        if multilang_date_index:
            multilang_result = _fetch_multilang_wayback_image(
                post_url, img_url, post_date, utype, idx,
                multilang_date_index, post_soup_cache,
                published_time=published_time,
                ko_lastmod=post_date,
                ko_category=category,
            )

        if kakao_result is None and multilang_result is None:
            continue

        # ── 결과 선택: 큰 쪽 primary, 작은 쪽 alt ────────────────────────
        primary: tuple[bytes, str, str, str, str] | None = None
        alt: tuple[bytes, str, str, str, str] | None = None

        if kakao_result and multilang_result:
            if len(kakao_result[0]) >= len(multilang_result[0]):
                primary, alt = kakao_result, multilang_result
            else:
                primary, alt = multilang_result, kakao_result
        elif kakao_result:
            primary = kakao_result
        else:
            primary = multilang_result

        # ── primary 저장 ─────────────────────────────────────────────────
        p_content, p_final_url, p_ctype, p_cd, p_source = primary  # type: ignore[misc]
        p_hash = _sha256_bytes(p_content)

        with _state_lock:
            if seen_key in fb_seen_urls:
                continue
            existing_rel = fb_img_hashes.get(p_hash)
            if existing_rel is not None:
                fb_seen_urls.add(seen_key)
                _fb_map_buf.add(f"{_clean_img_url(img_url)}\t{existing_rel}")
                _fb_done_buf.add(seen_key)
                continue
            fb_seen_urls.add(seen_key)

        p_filename = _determine_filename(utype, img_url, p_final_url, p_ctype, p_cd, idx)
        p_tag = _source_tag(p_source)
        if p_tag:
            stem = Path(p_filename).stem
            suffix = Path(p_filename).suffix
            p_filename = f"{p_tag} {stem}{suffix}"

        folder.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(p_filename)
        with _save_lock:
            saved_name = save_image(p_content, safe_name, folder)
        p_rel = (folder / saved_name).relative_to(ROOT_DIR).as_posix()

        with _state_lock:
            fb_img_hashes[p_hash] = p_rel

        img_key = _clean_img_url(img_url)
        _fb_map_buf.add(f"{img_key}\t{p_rel}")
        _fb_done_buf.add(seen_key)
        _fb_img_hash_buf.add(f"{p_hash}\t{p_rel}\t")

        if p_source.startswith("http://pf.kakao.com/"):
            _fb_kakao_pf_log_buf.add(f"{p_rel}\t{post_url}\t{p_source}\t{img_url}")
            ok_kakao += 1
        else:
            _fb_multilang_log_buf.add(f"{p_rel}\t{post_url}\t{p_source}\t{img_url}")
            ok_multilang += 1
        ok_saved += 1

        # ── alt 저장 (있으면) ────────────────────────────────────────────
        if alt is not None:
            a_content = alt[0]
            a_hash = _sha256_bytes(a_content)
            if a_hash != p_hash:
                a_filename = _determine_filename(
                    utype, img_url, alt[1], alt[2], alt[3], idx
                )
                a_tag = _source_tag(alt[4])
                a_rel = _save_alternative_image(
                    a_content, a_filename, folder,
                    source_tag=a_tag, root_dir=ROOT_DIR,
                )
                if a_rel:
                    if alt[4].startswith("http://pf.kakao.com/"):
                        _fb_kakao_pf_log_buf.add(
                            f"{a_rel}\t{post_url}\t{alt[4]}\t{img_url}")
                    else:
                        _fb_multilang_log_buf.add(
                            f"{a_rel}\t{post_url}\t{alt[4]}\t{img_url}")

    return PostProcessResult(
        ok=ok_saved, fail=0, post_fetch_ok=True,
        ok_saved=ok_saved, ok_multilang=ok_multilang, ok_kakao=ok_kakao,
    )
