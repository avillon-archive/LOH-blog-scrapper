# -*- coding: utf-8 -*-
"""Thread-safe 에셋 다운로더 — per-URL 잠금 + 해시 기반 파일명 캐싱."""

import hashlib
import re
import threading
import urllib.parse
from pathlib import Path

from config import BLOG_IMAGE_PREFIX as _BLOG_IMAGE_PREFIX
from utils import fetch_with_retry

# CSS url(...) 참조 패턴
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")


# ---------------------------------------------------------------------------
# 베이스 클래스
# ---------------------------------------------------------------------------


class BaseAssetDownloader:
    """Thread-safe 에셋 다운로더. per-URL 잠금 + double-check-exists + fetch.

    서브클래스 구현 필수:
        _default_name  — URL 경로가 빈 경우 기본 파일명
        _default_ext   — 확장자가 없는 경우 기본 확장자
        _save(resp, local_path, url)  — 응답을 디스크에 저장
    """

    _default_name: str
    _default_ext: str

    def __init__(self, assets_dir: Path) -> None:
        self._assets_dir = assets_dir
        self._lock = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}

    def download(self, url: str) -> str | None:
        """에셋을 다운로드하고 로컬 파일명 반환. 이미 있으면 파일명만 반환."""
        if not self._should_download(url):
            return None

        filename = self._filename(url)
        local_path = self._assets_dir / filename
        if local_path.exists():
            return filename

        # per-URL lock 획득 (같은 URL 동시 다운로드 방지)
        with self._lock:
            if url not in self._url_locks:
                self._url_locks[url] = threading.Lock()
            url_lock = self._url_locks[url]

        with url_lock:
            if local_path.exists():
                return filename
            resp = fetch_with_retry(url)
            if resp is None:
                return None
            self._save(resp, local_path, url)
            return filename

    def _filename(self, url: str) -> str:
        """URL → {stem}_{md5[:8]}.{ext} 파일명."""
        path = urllib.parse.urlparse(url).path
        name = path.rsplit("/", 1)[-1] or self._default_name
        stem, _, ext = name.rpartition(".")
        if not ext:
            stem, ext = name, self._default_ext
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        return f"{stem}_{url_hash}.{ext}"

    def _should_download(self, url: str) -> bool:
        """URL 필터링. 기본: 항상 True. 서브클래스에서 오버라이드."""
        return True

    def _save(self, resp, local_path: Path, url: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 구현 클래스
# ---------------------------------------------------------------------------


class CssDownloader(BaseAssetDownloader):
    """CSS 파일을 assets/ 에 다운로드. url() 상대경로를 절대 URL로 변환."""

    _default_name = "style.css"
    _default_ext = "css"

    def _save(self, resp, local_path: Path, url: str) -> None:
        css_text = self._resolve_relative_urls(resp.text, url)
        local_path.write_text(css_text, encoding="utf-8")

    @staticmethod
    def _resolve_relative_urls(css_text: str, css_url: str) -> str:
        """CSS 내 url() 상대경로를 절대 URL로 변환."""
        base = css_url.rsplit("/", 1)[0] + "/"

        def _resolve(m: re.Match) -> str:
            ref = m.group(1)
            if ref.startswith(("data:", "http://", "https://", "//")):
                return m.group(0)
            return f"url({urllib.parse.urljoin(base, ref)})"

        return CSS_URL_RE.sub(_resolve, css_text)


class SiteImageDownloader(BaseAssetDownloader):
    """블로그 사이트 크롬 이미지(favicon, 로고, 프로필)를 assets/ 에 다운로드."""

    _default_name = "image.png"
    _default_ext = "png"

    def _should_download(self, url: str) -> bool:
        return url.startswith(_BLOG_IMAGE_PREFIX)

    def _save(self, resp, local_path: Path, url: str) -> None:
        local_path.write_bytes(resp.content)
