# -*- coding: utf-8 -*-
"""단일 이미지 다운로드 (download_one_image) 및 파일명 결정."""

import hashlib
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from log_io import csv_line
from utils import ROOT_DIR

from .constants import DOWNLOADABLE_EXTS, IMAGE_OVERRIDES, KAKAO_PF_PROFILE
from .fetch import (
    _fetch_image,
    _fetch_wayback_gdrive_from_post,
    _fetch_wayback_image,
    _fetch_wayback_img_from_post,
    _fetch_wayback_linked_from_post,
    _filename_from_cd,
)
from .hashing import _sha256_bytes
from .models import PostSoupCache
from .persistence import record_failed, remove_from_failed, save_image
from .state import (
    _done_buf,
    _img_hash_buf,
    _map_buf,
    _save_lock,
    _state_lock,
)
from .url_utils import (
    _basename,
    _clean_img_url,
    _ext_from_mime,
    _is_community_cdn,
    _safe_filename,
    _seen_key,
)


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
# 출처 태그 / alternative 저장
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
    root_dir: Path = ROOT_DIR,
) -> str | None:
    """alternative 이미지를 저장하고 root_dir 기준 상대 경로를 반환한다."""
    folder.mkdir(parents=True, exist_ok=True)
    if source_tag:
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        filename = f"{source_tag} {stem}{suffix}"
    safe_name = _safe_filename(filename)
    with _save_lock:
        saved = save_image(content, safe_name, folder)
    return (folder / saved).relative_to(root_dir).as_posix()


# ---------------------------------------------------------------------------
# download_one_image
# ---------------------------------------------------------------------------


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
    post_soup_cache: PostSoupCache = None,
    *,
    post_date: str = "",
) -> str:
    """이미지 1건 다운로드. 성공 방식을 나타내는 문자열을 반환한다.

    Returns:
        "already"  — 이미 다운로드됨
        "original" — 원본/KO Wayback으로 성공
        "dup"      — 해시 중복 (저장 생략)
        ""         — 실패
    """
    seen_key = _seen_key(utype, img_url)

    if seen_key in seen_urls:
        return "already"

    # ── 수동 오버라이드 ──────────────────────────────────────────────────
    clean_url = _clean_img_url(img_url)
    override_target = IMAGE_OVERRIDES.get(clean_url)
    if override_target:
        target_clean = _clean_img_url(override_target)
        local_path = image_map.get(target_clean)
        if local_path:
            with _state_lock:
                seen_urls.add(seen_key)
                image_map[clean_url] = local_path
            _map_buf.add(csv_line(clean_url, local_path))
            _done_buf.add(seen_key)
            remove_from_failed(post_url, img_url=img_url)
            return "override"
        else:
            print(f"  [override] target 미발견: {override_target[:80]}")

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

    if payload is None:
        record_failed(post_url, img_url, "download_failed")
        return ""

    content, final_url, content_type, content_disposition = payload
    filename = _determine_filename(
        utype, img_url, final_url, content_type, content_disposition, idx
    )
    safe_name = _safe_filename(filename)

    # ── Phase 1: in-memory 상태 예약 (_state_lock, 극히 빠름) ────────────
    should_save = False
    img_key: str | None = None
    content_hash = _sha256_bytes(content)

    with _state_lock:
        if seen_key in seen_urls:
            return "already"

        existing_rel = img_hashes.get(content_hash)
        if existing_rel is not None:
            seen_urls.add(seen_key)
            img_key = _clean_img_url(img_url)
            if img_key not in image_map:
                image_map[img_key] = existing_rel
                _map_buf.add(csv_line(img_key, existing_rel))
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
        with _state_lock:
            img_hashes[content_hash] = rel
            if img_key not in image_map:  # type: ignore[operator]
                image_map[img_key] = rel   # type: ignore[index]
                _map_buf.add(csv_line(img_key, rel))
        _img_hash_buf.add(csv_line(content_hash, rel, "T" if is_thumb else ""))

    _done_buf.add(seen_key)

    if not should_save:
        return "dup"
    return "original"
