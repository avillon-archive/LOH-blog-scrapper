# -*- coding: utf-8 -*-
"""URL 정규화 및 파일명 유틸 (순수 함수)."""

import mimetypes
import re
import urllib.parse
from pathlib import Path

from utils import SIZE_W_RE, clean_url

from .constants import COMMUNITY_CDN_HOST


def _strip_ref_param(url: str) -> str:
    """URL에서 ref 쿼리 파라미터를 제거한다 (Ghost CMS 참조 추적용)."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.query:
        return url
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "ref" not in params:
        return url
    params.pop("ref")
    new_query = urllib.parse.urlencode(params, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def _clean_img_url(url: str) -> str:
    """이미지 URL 정규화: ref 파라미터 제거 + clean_url."""
    return clean_url(_strip_ref_param(url))


def _seen_scope(utype: str) -> str:
    return "thumb" if utype == "og_image" else "main"


def _seen_key(utype: str, url: str) -> str:
    return f"{_seen_scope(utype)}:{_clean_img_url(url)}"


def _is_community_cdn(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() == COMMUNITY_CDN_HOST


def _normalized_link_key(url: str) -> str:
    """Wayback 재작성을 고려한 URL 정규화 (링크 매칭용)."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = urllib.parse.unquote(parsed.path or "")
    path = SIZE_W_RE.sub("", path)
    if path != "/":
        path = path.rstrip("/")
    return f"{host}{path}"


# ---------------------------------------------------------------------------
# 파일명 유틸
# ---------------------------------------------------------------------------


def _ext_from_mime(mime: str) -> str:
    ext = mimetypes.guess_extension(mime.split(";")[0].strip())
    if ext == ".jpe":
        ext = ".jpg"
    return ext or ".bin"


def _basename(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = Path(path).name or ""
    return urllib.parse.unquote(name) if name else ""


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name[:200] or "image"
