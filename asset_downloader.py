# -*- coding: utf-8 -*-
"""Thread-safe 에셋 다운로더 — per-URL 잠금 + 해시 기반 파일명 캐싱."""

import hashlib
import re
import threading
import urllib.parse
from pathlib import Path

from config import BLOG_HOST as _BLOG_HOST, BLOG_IMAGE_PREFIX as _BLOG_IMAGE_PREFIX
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
    _dedup: bool = False  # True 면 콘텐츠 SHA-256 dedup 활성(SiteImageDownloader)

    def __init__(self, assets_dir: Path) -> None:
        self._assets_dir = assets_dir
        self._lock = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}
        # {sha256: filename}. 최초 download() 시 assets_dir 스캔으로 빌드(_dedup 전용).
        self._hash_index: dict[str, str] | None = None

    def download(self, url: str) -> str | None:
        """에셋을 다운로드하고 로컬 파일명 반환. 이미 있으면 파일명만 반환.

        `_dedup` 활성 시(SiteImageDownloader) fetch 후 콘텐츠 SHA-256 으로 dedup:
        바이트 동일 자산은 먼저 받은 canonical 파일을 재사용해 중복 생성을 막는다
        (storage.ghost.io ↔ 블로그 자기호스트가 같은 바이트를 다른 URL 로 서빙하는 경우 대응).
        """
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
            if self._dedup:
                return self._save_dedup(resp, filename, local_path, url)
            self._save(resp, local_path, url)
            return filename

    def _save_dedup(self, resp, filename: str, local_path: Path, url: str) -> str:
        """콘텐츠 SHA-256 dedup 저장. 바이트 동일 기존 파일이 있으면 그 파일명 반환."""
        index = self._ensure_index()
        digest = hashlib.sha256(resp.content).hexdigest()
        with self._lock:
            existing = index.get(digest)
            if existing is None:
                index[digest] = filename  # canonical 예약(동시 동일바이트 fetch 중복 방지)
        if existing is not None and (self._assets_dir / existing).exists():
            return existing
        self._save(resp, local_path, url)
        return filename

    def _ensure_index(self) -> dict[str, str]:
        """assets_dir 를 1회 스캔해 {sha256: filename} in-memory 인덱스 빌드.

        영속 CSV 대신 매 런 스캔 — 고아 정리(파일 삭제) 후에도 drift 없음.
        같은 해시 충돌 시 정렬상 먼저 오는 파일명을 canonical 로 채택(setdefault).
        """
        if self._hash_index is not None:
            return self._hash_index
        with self._lock:
            if self._hash_index is not None:
                return self._hash_index
            index: dict[str, str] = {}
            if self._assets_dir.exists():
                for p in sorted(self._assets_dir.iterdir()):
                    if p.is_file():
                        digest = hashlib.sha256(p.read_bytes()).hexdigest()
                        index.setdefault(digest, p.name)
            self._hash_index = index
            return index

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
    _dedup = True  # storage.ghost.io ↔ blog 자기호스트 바이트 동일 자산 중복 차단

    def _should_download(self, url: str) -> bool:
        # 블로그 자기 호스트의 /content/images/ (구도메인 등 기존 동작 보존)
        if url.startswith(_BLOG_IMAGE_PREFIX):
            return True
        # 신규 ghost.io 블로그는 프로필 아바타·favicon·icon 을 공유 CDN storage.ghost.io 로
        # 서빙한다. content 수집 필터와 동일 기준 — host + /content/images/ 로 허용.
        # 죽은 구도메인·gdrive·서드파티는 host 불일치로 탈락(아카이브 보존 정책 유지).
        parsed = urllib.parse.urlparse(url)
        return (
            "/content/images/" in parsed.path
            and (parsed.hostname or "").lower() in (_BLOG_HOST, "storage.ghost.io")
        )

    def _save(self, resp, local_path: Path, url: str) -> None:
        local_path.write_bytes(resp.content)
