# -*- coding: utf-8 -*-
"""download_media HTTP fetch — Content-Type 무관하게 바이트만 반환.

이미지 전용 필터가 있는 download_images.fetch._fetch_image 와 달리,
mp4/webm/mp3 등 어떤 MIME 이어도 허용한다. Wayback CDX / 포럼 재조회는
download_images.fetch._wayback_oldest / _cdn_to_forum_url 를 재사용한다.
"""

import urllib.parse
from pathlib import Path

from utils import fetch_with_retry

from download_images.fetch import _add_im, _cdn_to_forum_url, _wayback_oldest


def _response_to_payload(
    resp,
    *,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    """HTTP 응답을 (content, final_url, content_type, content_disposition) 로 변환."""
    if not resp or not getattr(resp, "content", None):
        return None
    content = resp.content
    if len(content) < min_bytes:
        return None
    return (
        content,
        resp.url,
        resp.headers.get("Content-Type", ""),
        resp.headers.get("Content-Disposition", ""),
    )


def _fetch_media(
    url: str,
    *,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    """직접 다운로드 — HTML 응답은 거부."""
    resp = fetch_with_retry(url, allow_redirects=True)
    payload = _response_to_payload(resp, min_bytes=min_bytes)
    if payload is None:
        return None
    # HTML 폴백 거부 (죽은 호스트가 index 페이지로 리다이렉트하는 경우)
    ctype = payload[2].lower().split(";")[0].strip()
    if ctype.startswith("text/html"):
        return None
    return payload


def _fetch_wayback_media(
    url: str,
    *,
    min_bytes: int = 1,
) -> tuple[bytes, str, str, str] | None:
    """Wayback CDX 에서 가장 오래된 스냅샷을 받아 바이트를 반환."""
    wayback_url = _wayback_oldest(url)
    if not wayback_url:
        forum_url = _cdn_to_forum_url(url)
        if forum_url:
            wayback_url = _wayback_oldest(forum_url)
    if not wayback_url:
        return None

    resp = fetch_with_retry(_add_im(wayback_url), allow_redirects=True)
    payload = _response_to_payload(resp, min_bytes=min_bytes)
    if payload is None:
        return None
    ctype = payload[2].lower().split(";")[0].strip()
    if ctype.startswith("text/html"):
        return None
    return payload


def strip_wayback_prefix(url: str) -> str:
    """`https://web.archive.org/web/TIMESTAMPim_/https://original/...` → 원본 URL."""
    import re
    m = re.match(r"^https?://web\.archive\.org/web/\d+[a-z_]*/(.+)$", url, re.IGNORECASE)
    return m.group(1) if m else url
