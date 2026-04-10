# -*- coding: utf-8 -*-
"""단일 미디어 다운로드."""

import hashlib
import mimetypes
import urllib.parse
from pathlib import Path

from log_io import csv_line
from utils import ROOT_DIR

from download_images.fetch import _filename_from_cd
from download_images.persistence import save_image  # 파일명 충돌 해소 로직 재사용
from download_images.url_utils import _clean_img_url, _safe_filename

from .constants import (
    ANCHOR_INLINE,
    AUDIO_EXTS,
    MEDIA_EXTS,
    VIDEO_EXTS,
)
from .fetch import _fetch_media, _fetch_wayback_media
from .state import (
    _media_done_buf,
    _media_map_buf,
    _media_save_lock,
    _media_state_lock,
)


def _seen_key(url: str) -> str:
    return f"media:{_clean_img_url(url)}"


def _ext_from_ctype(content_type: str) -> str:
    """Content-Type → 확장자. 모르면 빈 문자열."""
    primary = (content_type or "").split(";")[0].strip().lower()
    if not primary:
        return ""
    guessed = mimetypes.guess_extension(primary)
    if guessed == ".jpe":
        return ".jpg"
    return guessed or ""


def _determine_media_filename(
    media_url: str,
    mtype: str,
    final_url: str,
    content_type: str,
    cd_header: str,
    idx: int,
) -> str:
    cd_name = _filename_from_cd(cd_header)
    if cd_name:
        return cd_name

    # GDrive 는 basename 대신 쿼리 파라미터 기반
    parsed_orig = urllib.parse.urlparse(media_url)
    host = (parsed_orig.hostname or "").lower()
    if "drive.google" in host or "docs.google" in host or "googleusercontent" in host:
        query = urllib.parse.parse_qs(parsed_orig.query)
        for key in ("name", "title"):
            if key in query and query[key]:
                return query[key][0]
        digest = hashlib.md5(media_url.encode()).hexdigest()[:12]
        return f"{digest}{_ext_from_ctype(content_type) or '.bin'}"

    for candidate in (final_url, media_url):
        basename = urllib.parse.unquote(Path(urllib.parse.urlparse(candidate).path).name or "")
        if basename:
            ext = Path(basename).suffix.lower()
            if ext in MEDIA_EXTS or ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                return basename
            if ext:
                return basename

    ext = _ext_from_ctype(content_type)
    prefix = "poster" if mtype == "video_poster" else ("audio" if mtype == "audio_tag" else "video")
    return f"{prefix}_{idx}{ext or '.bin'}"


def _record_map(
    post_url: str,
    media_url: str,
    rel_path: str,
    anchor_type: str,
    anchor_text: str,
) -> None:
    _media_map_buf.add(
        csv_line(post_url, _clean_img_url(media_url), rel_path, anchor_type, anchor_text)
    )


def download_one_media(
    media_url: str,
    mtype: str,
    post_url: str,
    folder: Path,
    idx: int,
    seen_urls: set[str],
    media_map: dict[str, str],
    *,
    anchor_type: str = ANCHOR_INLINE,
    anchor_text: str = "",
) -> str:
    """단일 미디어 다운로드. 성공 방식을 나타내는 문자열 반환.

    Returns:
        "already"   — 동일 URL 이 이미 처리됨 (media_map 재사용)
        "original"  — 디스크에 새 파일 저장
        ""          — 실패
    """
    seen_key = _seen_key(media_url)
    clean = _clean_img_url(media_url)

    # 이미 처리된 URL: media_map 기존 경로로 post 별 엔트리만 기록
    if seen_key in seen_urls:
        existing_rel = media_map.get(clean)
        if existing_rel:
            _record_map(post_url, media_url, existing_rel, anchor_type, anchor_text)
        return "already"

    # ── 다운로드 ────────────────────────────────────────────────────────
    payload = _fetch_media(media_url)
    if payload is None:
        payload = _fetch_wayback_media(media_url)

    if payload is None:
        return ""

    content, final_url, content_type, cd_header = payload
    filename = _determine_media_filename(
        media_url, mtype, final_url, content_type, cd_header, idx
    )
    safe_name = _safe_filename(filename)

    folder.mkdir(parents=True, exist_ok=True)
    with _media_save_lock:
        saved_name = save_image(content, safe_name, folder)
    rel_path = (folder / saved_name).relative_to(ROOT_DIR).as_posix()

    with _media_state_lock:
        seen_urls.add(seen_key)
        if clean not in media_map:
            media_map[clean] = rel_path

    _media_done_buf.add(seen_key)
    _record_map(post_url, media_url, rel_path, anchor_type, anchor_text)
    return "original"
